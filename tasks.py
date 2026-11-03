"""Independent, bounded paper acquisition and AI reading tasks."""
from __future__ import annotations
import datetime as dt, json, os, subprocess, threading, time
import core, taxonomy

STATE_LOCK = threading.RLock()
STOP_FILE = core.safe_path("data","stop.signal")  # Compatibility with the standalone stop/uninstall tools.
EVENTS = {"fetch":threading.Event(),"ai":threading.Event()}
def fresh(kind):
    return {"kind":kind,"running":False,"cancelling":False,"message":"尚未获取论文" if kind=="fetch" else "尚未调用 AI",
            "done":0,"total":0,"finished_at":"","errors":[]}
LANES = {kind:fresh(kind) for kind in EVENTS}
STATE = {"running":False}
def signal_path(kind):
    return core.safe_path("data",kind+".stop.signal")
def progress_path(kind):
    return core.safe_path("data",kind+"-progress.json")
def sync_state():
    STATE["running"] = any(value["running"] for value in LANES.values())
def message(kind,text,**values):
    with STATE_LOCK:
        LANES[kind].update(message=text,**values)
        core.atomic_json(progress_path(kind),LANES[kind])
        sync_state()
def lane_status(kind):
    with STATE_LOCK:
        if LANES[kind]["running"]:
            return dict(LANES[kind])
    try:
        value = fresh(kind)|json.loads(progress_path(kind).read_text("utf-8"))
    except (OSError,ValueError):
        value = fresh(kind)
    if value["running"]:
        try:
            with core.FileLock(kind+".lock"):
                value.update(running=False,cancelling=False,message="上次任务已结束或中断，可手动继续")
        except RuntimeError:
            pass
    return value
def status():
    lanes={kind:lane_status(kind) for kind in EVENTS}
    latest=max(lanes.values(),key=lambda v:v.get("finished_at",""))
    running=[value for value in lanes.values() if value["running"]]
    return {**lanes,"running":bool(running),"message":"；".join(v["message"] for v in running) if running else latest["message"],
            "done":sum(v["done"] for v in lanes.values()),"total":sum(v["total"] for v in lanes.values()),
            "errors":[e for v in lanes.values() for e in v["errors"]],"finished_at":latest["finished_at"]}
def cancelled(kind):
    if STOP_FILE.exists() or signal_path(kind).exists():
        EVENTS[kind].set()
    return EVENTS[kind].is_set()
def attempt(spec,state,text=""):
    with core.db() as con:
        con.execute("INSERT OR REPLACE INTO attempts VALUES(?,?,?,?)",
                    (spec["id"],core.now().date().isoformat(),state,text[:300]))
def due_specs(cfg):
    today=core.now().date()
    specs=[]
    latest=core.latest_periods(today)
    if cfg["daily"]:
        if core.now().strftime("%H:%M")>=cfg["time"]:
            specs.append(latest[0])
        for delta in range(1,cfg["backfill_days"]+1):
            specs.append(core.period("daily",(today-dt.timedelta(days=delta)).isoformat()))
    for name,spec in zip(["daily","weekly","monthly"],latest):
        if name=="daily" or not cfg[name]:
            continue
        released=dt.date.fromisoformat(spec["end"])+dt.timedelta(days=2 if name=="weekly" else 1)
        if today>released or (today==released and core.now().strftime("%H:%M")>=cfg["time"]):
            specs.append(spec)
    with core.db() as con:
        complete={r[0] for r in con.execute("SELECT id FROM reports")}
        attempted={r[0] for r in con.execute("SELECT report_id FROM attempts WHERE run_date=?",(today.isoformat(),))}
    return [s for s in specs if s["id"] not in complete and s["id"] not in attempted]
def prepare(kind):
    EVENTS[kind].clear()
    signal_path(kind).unlink(missing_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    LANES[kind].update(fresh(kind))
    message(kind,"准备获取论文" if kind=="fetch" else "准备 AI 翻译与简介",running=True)
def stop_stamp():
    path=signal_path("ai")
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None

def follow_with_ai(pids,deadline,stamp):
    """Run synchronously so a scheduled worker stays alive until bounded AI work ends."""
    if not pids or not core.settings()["ai_after_fetch"] or stop_stamp()!=stamp:
        return
    with STATE_LOCK:
        if lane_status("ai")["running"]:
            message("fetch",LANES["fetch"]["message"]+"；AI 已有任务，本批可稍后手动解读")
            return
        try:
            lock=core.FileLock("ai.lock")
            lock.__enter__()
        except RuntimeError:
            return
        try:
            if stop_stamp()!=stamp:
                lock.__exit__(None,None,None)
                return
            if not core.get_key():
                message("ai","未设置密钥，本次只保存摘要",running=False)
                lock.__exit__(None,None,None)
                return
            with core.db() as con:
                raw={r[0] for r in con.execute("SELECT id FROM papers WHERE ai_state='raw' AND abstract!=''")}
            selected=[pid for pid in dict.fromkeys(pids) if pid in raw][:20]
            if not selected:
                lock.__exit__(None,None,None)
                return
            prepare("ai")
        except Exception:
            lock.__exit__(None,None,None)
            message("ai","密钥不可用或解读准备失败，本次只保存摘要；未重试",running=False)
            return
    run("ai",pids=selected,_lock=lock,_prepared=True,_automatic=True,_deadline=deadline)

def run(mode="auto",spec=None,pids=None,_lock=None,_prepared=False,_automatic=False,_deadline=None):
    kind="ai" if mode=="ai" else "fetch"
    lock=_lock
    watcher_stop=threading.Event()
    auto_candidates=[]
    ai_stamp=stop_stamp()
    try:
        core.initialize()
        cfg=core.settings()
        if mode=="auto" and not cfg["auto"]:
            return False
        if core.safe_path("config","uninstalling").exists():
            return False
        if lock is None:
            lock=core.FileLock(kind+".lock")
            try:
                lock.__enter__()
            except RuntimeError:
                lock=None
                return False
        if not _prepared:
            prepare(kind)
        if core.safe_path("data","papers.db").stat().st_size>128*1024*1024:
            raise ValueError("数据库超过安全容量上限，请先导出或清理")
        if __import__("shutil").disk_usage(core.ROOT).free<150*1024*1024:
            raise ValueError("磁盘剩余空间不足，已停止新增")
        def watch():
            while not watcher_stop.wait(.2):
                if cancelled(kind):
                    return
        threading.Thread(target=watch,daemon=True).start()
        deadline=_deadline if _deadline is not None else time.monotonic()+core.RUN_SECONDS
        errors=[]
        notes=[]
        done=0
        with core.cancellation_scope(EVENTS[kind]):
            if kind=="ai":
                selected=list(dict.fromkeys(pids or []))[:20]
                message(kind,"正在翻译摘要并生成简介",total=len(selected))
                for pid in selected:
                    if cancelled(kind):
                        break
                    if _automatic and not core.settings()["ai_after_fetch"]:
                        EVENTS[kind].set()
                        break
                    if time.monotonic()>=deadline:
                        errors.append("已达到本轮时限，剩余论文尚未处理")
                        break
                    try:
                        if _automatic:
                            with core.db() as con:
                                row=con.execute("SELECT ai_state FROM papers WHERE id=?",(pid,)).fetchone()
                            if not row or row[0]!="raw":
                                continue
                        current=core.settings()
                        with core.db() as con:
                            usage=con.execute("SELECT calls FROM usage WHERE day=?",(core.now().date().isoformat(),)).fetchone()
                        if usage and usage[0]>=current["daily_ai_limit"]:
                            errors.append("已达到每日 AI 调用上限，剩余摘要保留，可稍后手动继续")
                            break
                        core.ai_read(pid,current,deadline)
                        done+=1
                        message(kind,"已完成 "+str(done)+" 篇 AI 翻译与简介",done=done)
                    except Exception as exc:
                        if isinstance(exc,InterruptedError) or cancelled(kind):
                            break
                        errors.append(str(exc))
                        with core.db() as con:
                            con.execute("UPDATE papers SET ai_state='failed' WHERE id=? AND ai_state!='done'",(pid,))
                        # An explicit AI action makes one attempt per paper, no retries.
                        break
            else:
                specs=due_specs(cfg) if mode=="auto" else ([spec] if spec else [
                    item for item in core.latest_periods() if cfg[item["kind"]]])
                if len(specs)>10:
                    raise ValueError("本轮任务数量超过上限")
                message(kind,"正在获取论文摘要" if specs else "没有需要获取的新榜单",total=len(specs))
                seen_reports=set()
                for requested in specs:
                    item=requested
                    if cancelled(kind):
                        break
                    if time.monotonic()>=deadline:
                        errors.append("已达到本轮时限，剩余榜单尚未获取")
                        break
                    if mode=="auto" and not core.settings()["auto"]:
                        EVENTS[kind].set()
                        break
                    if item["end"]<core.cutoff_date().isoformat():
                        errors.append("所选榜单超出最近三个月保留范围")
                        continue
                    if dt.date.fromisoformat(item["end"])>core.now().date() or (item["kind"]!="daily" and item["end"]==core.now().date().isoformat()):
                        errors.append("不能获取未来日期或尚未结束的周月榜")
                        continue
                    if item["id"] in seen_reports:
                        continue
                    message(kind,"正在检查 HF 最新日榜" if item.get("latest") else "正在获取 "+item["period"],done=done)
                    attempt(requested,"running")
                    try:
                        if item.get("latest"):
                            item,rows,total=core.fetch_latest_daily(deadline)
                            notes.append("HF 最新日榜为 "+item["period"])
                            with core.db() as con:
                                exists=con.execute("SELECT 1 FROM reports WHERE id=?",(item["id"],)).fetchone()
                            if mode=="auto" and exists:
                                attempt(requested,"checked","最新日榜 "+item["period"]+" 已保存")
                                seen_reports.add(item["id"])
                                done+=1
                                continue
                        else:
                            rows,total=core.fetch_report(item,deadline)
                        if cancelled(kind):
                            attempt(requested,"cancelled","用户停止获取")
                            break
                        core.save_report(item,rows,total)
                        seen_reports.add(item["id"])
                        auto_candidates.extend(row["id"] for row in rows)
                        if requested["id"]!=item["id"]:
                            attempt(requested,"checked","实际日榜 "+item["period"])
                        attempt(item,"done")
                        done+=1
                        message(kind,"已获取 "+item["period"]+"，保存 "+str(len(rows))+" 篇摘要",done=done)
                    except core.ReportUnavailable as exc:
                        attempt(requested,"unavailable",str(exc))
                        notes.append(requested["period"]+" 无对应榜单，已跳过")
                    except Exception as exc:
                        if isinstance(exc,InterruptedError) or cancelled(kind):
                            attempt(requested,"cancelled","用户停止获取")
                            break
                        errors.append(item["period"]+"："+str(exc))
                        attempt(requested,"failed",str(exc))
                        if isinstance(exc,RuntimeError):
                            break
                # Save abstracts first. AI handoff happens after releasing the fetch lane.
            if not cancelled(kind):
                core.cleanup()
        stopped=cancelled(kind)
        label="获取论文" if kind=="fetch" else "AI 翻译与简介"
        ended=(label+"已停止") if stopped else (label+"结束，部分未完成；未自动重试" if errors else label+"完成")
        if notes:
            ended+="；"+"；".join(notes[:3])
        message(kind,ended,running=False,cancelling=False,errors=errors[:10],finished_at=core.now().isoformat(timespec="seconds"))
    except Exception as exc:
        message(kind,("获取论文" if kind=="fetch" else "AI")+"停止："+str(exc),
                running=False,cancelling=False,errors=[str(exc)],finished_at=core.now().isoformat(timespec="seconds"))
        return False
    finally:
        watcher_stop.set()
        if lock is not None:
            lock.__exit__(None,None,None)
        with STATE_LOCK:
            LANES[kind]["running"]=False
            sync_state()
    if kind=="fetch" and not stopped and not cancelled("fetch") and time.monotonic()<deadline:
        follow_with_ai(auto_candidates,deadline,ai_stamp)
    return True
def start(mode="auto",spec=None,pids=None):
    kind="ai" if mode=="ai" else "fetch"
    with STATE_LOCK:
        if lane_status(kind)["running"]:
            raise ValueError("已有获取任务运行中" if kind=="fetch" else "AI 正在处理，请先停止或等待完成")
        if kind=="ai":
            if not core.get_key():
                raise ValueError("请先在设置中保存 DeepSeek API Key")
            if not pids:
                raise ValueError("当前筛选没有待解读的论文")
            with core.db() as con:
                used=con.execute("SELECT calls FROM usage WHERE day=?",(core.now().date().isoformat(),)).fetchone()
            if used and used[0]>=core.settings()["daily_ai_limit"]:
                raise ValueError("已达到每日 AI 调用上限，可在设置中调整")
        lock=core.FileLock(kind+".lock")
        lock.__enter__()
        try:
            prepare(kind)
            def target():
                run(mode,spec,pids,_lock=lock,_prepared=True)
            threading.Thread(target=target,daemon=True).start()
        except BaseException:
            LANES[kind]["running"]=False
            lock.__exit__(None,None,None)
            sync_state()
            raise
def stop(kind=None):
    if kind is not None and kind not in EVENTS:
        raise ValueError("停止操作类型无效")
    for name in EVENTS if kind is None else [kind]:
        signal_path(name).write_text("stop","ascii")
        EVENTS[name].set()
        current=lane_status(name)
        if current["running"]:
            text="正在停止获取论文…" if name=="fetch" else "正在停止 AI 调用…"
            with STATE_LOCK:
                local=LANES[name]["running"]
            if local:
                message(name,text,cancelling=True)
            else:
                # A scheduled worker owns this lane in another process.
                # Never claim it as a local thread, or its completion would remain hidden.
                current.update(message=text,cancelling=True)
                core.atomic_json(progress_path(name),current)
    if kind is None:
        STOP_FILE.write_text("stop","ascii")

def powershell(script,*arguments):
    command = ["powershell.exe","-NoLogo","-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass",
               "-File",str(core.safe_path(script)),*arguments]
    done = subprocess.run(command,capture_output=True,timeout=25,creationflags=0x08000000)
    if done.returncode:
        raise RuntimeError("计划任务操作失败；未提升权限。请查看任务状态或使用手动更新。")
    return done.stdout.decode("utf-8-sig",errors="replace").strip()

def save_settings(value):
    current = core.settings()
    cfg = core.validated_settings(value)
    if any(key in value for key in ["auto","daily","weekly","monthly","time","backfill_days"]):
        if cfg["auto"]:
            powershell("schedule.ps1","-Action","Install","-At",cfg["time"])
        elif current["auto"] or task_exists():
            # Do not claim auto is disabled until task removal has succeeded.
            powershell("schedule.ps1","-Action","Remove")
    core.atomic_json(core.safe_path("config","settings.json"),cfg)
    if cfg["intro_limit"]<current["intro_limit"]:
        with core.db() as con:
            for row in con.execute("SELECT id,intro FROM papers WHERE intro!=''").fetchall():
                con.execute("UPDATE papers SET intro=? WHERE id=?",
                            (core.trim_intro(row["intro"],cfg["intro_limit"]),row["id"]))
    if current["auto"] and not cfg["auto"]:
        stop("fetch")
    return cfg

def task_exists():
    try:
        result = subprocess.run(["schtasks.exe","/Query","/TN",core.TASK_NAME],
                  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5,creationflags=0x08000000)
        return result.returncode==0
    except (OSError,subprocess.TimeoutExpired):
        return False


def list_data(filters):
    start,end=filters.get("start",""),filters.get("end","")
    for value in [start,end]:
        if value:
            dt.date.fromisoformat(value)
    tags=[x.strip() for x in filters.get("tags","").split(",") if x.strip()][:6]
    # Old bookmarked names remain useful after category normalization.
    tags=[t if t=="未分类" or t in taxonomy.CATALOG else taxonomy.normalize_tags([t])[-1] for t in tags]
    tag_mode=filters.get("tag_mode","any")
    kind=filters.get("kind","")
    if kind and kind not in ["daily","weekly","monthly"]:
        raise ValueError("类型无效")
    with core.db() as con:
        reports=[dict(r) for r in con.execute("SELECT * FROM reports ORDER BY end DESC,kind")]
        papers={r["id"]:dict(r) for r in con.execute("SELECT * FROM papers")}
        entries=[dict(r) for r in con.execute("SELECT * FROM entries ORDER BY rank")]
    counts={tag:0 for tag in taxonomy.CATALOG}
    uncategorized=0
    for paper in papers.values():
        raw=json.loads(paper["tags"])
        paper["tags"]=(raw if all(t in taxonomy.CATALOG for t in raw) else taxonomy.normalize_tags(raw,paper["title"],paper["abstract"])) if raw else []
        paper["keywords"]=json.loads(paper["keywords"])
        paper["url"]="https://huggingface.co/papers/"+paper["id"]
        paper["paper_url"]="https://arxiv.org/abs/"+paper["id"]
        if not paper["tags"]:
            uncategorized+=1
        for tag in paper["tags"]:
            counts[tag]+=1
    def matches(paper):
        query=filters.get("q","").strip().casefold()
        haystack=paper["title"]+" "+" ".join(paper["keywords"]+paper["tags"])
        if query and query not in haystack.casefold():
            return False
        state=filters.get("state","")
        if state=="done" and paper["ai_state"]!="done":
            return False
        if state=="raw" and paper["ai_state"]=="done":
            return False
        if tags:
            values=[t in paper["tags"] if t!="未分类" else not paper["tags"] for t in tags]
            return all(values) if tag_mode=="all" else any(values)
        return True
    paper_out,report_out={},[]
    by_report={}
    for entry in entries:
        by_report.setdefault(entry["report_id"],[]).append(entry)
    for report in reports:
        if kind and report["kind"]!=kind:
            continue
        if start and report["end"]<start:
            continue
        if end and report["start"]>end:
            continue
        members,all_members=[],[]
        for entry in by_report.get(report["id"],[]):
            paper=papers.get(entry["paper_id"])
            if not paper:
                continue
            member=paper|{"rank":entry["rank"],"votes":entry["votes"]}
            all_members.append(member)
            if matches(member):
                members.append(member)
                if member["id"] not in paper_out:
                    paper_out[member["id"]]=member|{"selected_date":report["end"]}
        if not members and (tags or filters.get("q") or filters.get("state")):
            continue
        report.update(papers=members,all_papers=all_members,count=len(all_members),matched=len(members))
        tally={}
        for member in all_members:
            for tag in member["tags"]:
                tally[tag]=tally.get(tag,0)+1
        report["top_tags"]=sorted(tally.items(),key=lambda x:(-x[1],x[0]))[:5]
        report_out.append(report)
    counts["未分类"]=uncategorized
    return {"papers":list(paper_out.values()),"reports":report_out,
            "tags":[t for t in taxonomy.CATALOG if counts[t]],"tag_groups":taxonomy.GROUPS,"tag_counts":counts,
            "stats":core.stats(),"status":status()}

if __name__=="__main__":
    import sys
    if "--worker" in sys.argv:
        run("auto")
