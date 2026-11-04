"""Meaningful behavior and deletion-boundary checks; no external network or real tasks."""
import datetime as dt, html, json, os, shutil, subprocess, sys, threading, unittest, urllib.error, urllib.request, uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core, tasks, app

REAL_ROOT = core.ROOT
FIXED = dt.datetime(2026,10,31,12,0,tzinfo=core.TZ)

class BehaviorTests(unittest.TestCase):
    def setUp(self):
        self.root = REAL_ROOT/"tests"/("sandbox_"+uuid.uuid4().hex)
        self.root.mkdir()
        self.original_stop = tasks.STOP_FILE
        core.ROOT = self.root
        tasks.STOP_FILE = self.root/"data"/"stop.signal"
        self.clock = patch.object(core,"now",return_value=FIXED)
        self.clock.start()
        core.CANCEL.clear()
        for name in tasks.EVENTS:
            tasks.EVENTS[name].clear()
            tasks.LANES[name].update(tasks.fresh(name))
        tasks.STATE.update(running=False,message="test",done=0,total=0,errors=[],finished_at="")
        core.initialize()
    def tearDown(self):
        import logging
        logger=logging.getLogger("papers")
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)
        core.CANCEL.clear()
        self.clock.stop()
        core.ROOT = REAL_ROOT
        tasks.STOP_FILE = self.original_stop
        if self.root.resolve().is_relative_to((REAL_ROOT/"tests").resolve()) and self.root.name.startswith("sandbox_"):
            shutil.rmtree(self.root)
    def row(self,pid="2610.12345",**kw):
        return {"id":pid,"title":"Test paper","abstract":"Author abstract","votes":8}|kw
    def latest_from_mock(self,deadline=None):
        spec=core.period("daily",core.now().date().isoformat())
        rows,total=core.fetch_report(spec,deadline)
        return spec,rows,total
    def test_ai_limit_persists_without_touching_scheduler_or_usage(self):
        cfg=core.settings();cfg["auto"]=True
        core.atomic_json(core.safe_path("config","settings.json"),cfg)
        with core.db() as con:
            con.execute("INSERT INTO usage(day,calls) VALUES (?,?)",(core.now().date().isoformat(),23))
        with patch.object(tasks,"powershell") as scheduler,patch.object(tasks,"task_exists") as query:
            saved=tasks.save_settings({"daily_ai_limit":50})
            scheduler.assert_not_called();query.assert_not_called()
        self.assertEqual(core.settings()["daily_ai_limit"],50)
        self.assertTrue(saved["auto"])
        self.assertEqual(core.settings()["model"],cfg["model"])
        with core.db() as con:
            self.assertEqual(con.execute("SELECT calls FROM usage").fetchone()[0],23)
    def test_invalid_daily_limit_does_not_overwrite_settings(self):
        original=core.safe_path("config","settings.json").read_bytes()
        for value in [0,101,1.5,True,"50"]:
            with self.assertRaisesRegex(ValueError,"每日 AI 请求上限"):
                tasks.save_settings({"daily_ai_limit":value})
            self.assertEqual(core.safe_path("config","settings.json").read_bytes(),original)
    def test_running_ai_uses_updated_limit_on_next_paper(self):
        self.save("daily","2026-10-30",[self.row(),self.row("2610.54321")])
        limits=[]
        def read(pid,cfg,deadline):
            limits.append(cfg["daily_ai_limit"])
            if len(limits)==1:
                tasks.save_settings({"daily_ai_limit":50})
        with patch.object(core,"ai_read",side_effect=read):
            self.assertTrue(tasks.run("ai",pids=["2610.12345","2610.54321"]))
        self.assertEqual(limits,[30,50])
    def save(self,kind,value,rows=None):
        spec=core.period(kind,value)
        core.save_report(spec,rows or [self.row()],30)
        return spec
    def test_native_hf_periods_and_calendar_retention(self):
        self.assertEqual(core.period("weekly","2026-W43")["start"],"2026-10-18")
        self.assertEqual(core.latest_periods()[1]["period"],"2026-W43")
        self.assertEqual(core.latest_periods()[2]["period"],"2026-09")
        self.assertEqual(core.cutoff_date(dt.date(2026,5,31)),dt.date(2026,2,28))
        self.assertEqual(core.cutoff_date(dt.date(2026,1,31)),dt.date(2025,10,31))
    def test_hf_date_validation_native_order_and_top_ten(self):
        props={"dateString":"2026-10-30","periodType":"day","dailyPapers":[
            {"paper":{"id":f"2610.{i:05}","title":f"Paper {i}","summary":"Original","upvotes":i}}
            for i in range(25)]}
        text='<div data-target="DailyPapers" data-props="'+html.escape(json.dumps(props),quote=True)+'"></div>'
        with patch.object(core,"request",return_value=text):
            rows,total=core.fetch_report(core.period("daily","2026-10-30"))
            self.assertEqual((len(rows),total),(10,25))
            self.assertEqual(rows[0]["id"],"2610.00000")
            with self.assertRaisesRegex(ValueError,"日期"):
                core.fetch_report(core.period("daily","2026-10-29"))
    def test_deduplication_and_reading_reuse_across_reports(self):
        self.save("daily","2026-10-30")
        with core.db() as con:
            con.execute("UPDATE papers SET intro='已有简介',ai_state='done'")
        created=core.save_report(core.period("monthly","2026-09"),[self.row()],1)
        self.assertEqual(created,[])
        self.assertEqual(core.stats()["papers"],1)
        self.assertEqual(core.stats()["reports"],2)
        with core.db() as con:
            self.assertEqual(con.execute("SELECT intro FROM papers").fetchone()[0],"已有简介")
    def test_retention_preserves_recent_reference_and_refreshes_export(self):
        self.save("daily","2026-07-30",[self.row("2607.00001"),self.row("2607.00002")])
        self.save("daily","2026-10-30",[self.row("2607.00002")])
        core.export_data()
        core.cleanup()
        self.assertEqual(core.stats()["papers"],1)
        self.assertEqual(core.stats()["reports"],1)
        exported=json.loads((self.root/"data"/"export.json").read_text("utf-8"))
        self.assertEqual([p["id"] for p in exported["papers"]],["2607.00002"])
    def test_combined_tag_date_state_and_report_filters(self):
        self.save("monthly","2026-09",[self.row(),self.row("2610.54321",title="Other")])
        with core.db() as con:
            con.execute("UPDATE papers SET tags=?,ai_state='done' WHERE id=?",
                        (json.dumps(["强化学习","推理"]),"2610.12345"))
        result=tasks.list_data({"kind":"monthly","start":"2026-09-15","end":"2026-10-01","tags":"强化学习,推理","tag_mode":"all","state":"done"})
        self.assertEqual(len(result["papers"]),1)
        self.assertEqual(result["reports"][0]["matched"],1)
        self.assertEqual(result["reports"][0]["count"],2)
        self.assertEqual(len(tasks.list_data({"tags":"未分类"})["papers"]),1)
    def test_no_key_does_not_call_ai(self):
        spec=core.period("daily","2026-10-30")
        with patch.object(core,"fetch_report",return_value=([self.row()],1)),patch.object(core,"ai_read") as ai:
            self.assertTrue(tasks.run("manual",spec))
            ai.assert_not_called()
        self.assertEqual(core.stats()["papers"],1)
    def test_fetch_all_periods_never_invokes_ai(self):
        cfg=core.settings();cfg["daily"]=cfg["weekly"]=cfg["monthly"]=True
        core.atomic_json(core.safe_path("config","settings.json"),cfg)
        def fetch(spec,deadline):
            ids={"daily":"2610.00001","weekly":"2610.00002","monthly":"2610.00003"}
            return [self.row(ids[spec["kind"]])],1
        with patch.object(core,"get_key",return_value="fake-key"),patch.object(core,"fetch_latest_daily",side_effect=self.latest_from_mock),patch.object(core,"fetch_report",side_effect=fetch) as hf,patch.object(core,"ai_read",side_effect=RuntimeError("HTTP 401")) as ai:
            tasks.run("manual")
            self.assertEqual(hf.call_count,5)
            ai.assert_not_called()
        self.assertEqual(core.stats()["papers"],3)
    def test_unreadable_key_does_not_block_collection(self):
        spec=core.period("daily","2026-10-30")
        with patch.object(core,"get_key",side_effect=RuntimeError("decrypt failed")),patch.object(core,"fetch_report",return_value=([self.row()],1)),patch.object(core,"ai_read") as ai:
            tasks.run("manual",spec)
            ai.assert_not_called()
        self.assertEqual(core.stats()["papers"],1)
    def test_network_failure_is_not_retried(self):
        with patch.object(core,"fetch_latest_daily",side_effect=self.latest_from_mock),patch.object(core,"fetch_report",side_effect=RuntimeError("network unavailable")) as fetch:
            tasks.run("manual")
            self.assertEqual(fetch.call_count,1)
        self.assertEqual(core.stats()["papers"],0)
    def test_failed_auto_period_not_retried_same_day(self):
        self.save("daily","2026-10-30")
        cfg=core.settings();cfg.update(auto=True,time="09:00",weekly=False,monthly=False,backfill_days=1)
        core.atomic_json(core.safe_path("config","settings.json"),cfg)
        with patch.object(core,"fetch_latest_daily",side_effect=self.latest_from_mock),patch.object(core,"fetch_report",side_effect=RuntimeError("offline")) as fetch:
            tasks.run("auto"); tasks.run("auto")
            self.assertEqual(fetch.call_count,1)
    def test_latest_daily_uses_actual_date_and_never_saves_redirect_as_requested_date(self):
        props={"dateString":"2026-10-30","periodType":"day","dailyPapers":[{"paper":{"id":"2610.12345","title":"Test","summary":"Abstract"}}]}
        text='<div data-target="DailyPapers" data-props="'+html.escape(json.dumps(props),quote=True)+'"></div>'
        with patch.object(core,"request",return_value=text):
            spec,rows,total=core.fetch_latest_daily()
            self.assertEqual(spec["period"],"2026-10-30")
            self.assertEqual((len(rows),total),(1,1))
            with self.assertRaises(core.ReportUnavailable):
                core.fetch_report(core.period("daily","2026-10-31"))
    def test_latest_daily_rejects_future_date(self):
        props={"dateString":"2026-11-01","periodType":"day","dailyPapers":[]}
        text='<div data-target="DailyPapers" data-props="'+html.escape(json.dumps(props),quote=True)+'"></div>'
        with patch.object(core,"request",return_value=text),self.assertRaisesRegex(ValueError,"日期"):
            core.fetch_latest_daily()
    def test_absent_daily_is_skipped_without_error_or_wrong_date_storage(self):
        with patch.object(core,"fetch_report",side_effect=core.ReportUnavailable("无对应日期")):
            tasks.run("manual",core.period("daily","2026-10-30"))
        self.assertEqual(core.stats()["papers"],0)
        self.assertEqual(tasks.lane_status("fetch")["errors"],[])
        with core.db() as con:
            self.assertEqual(con.execute("SELECT state FROM attempts").fetchone()[0],"unavailable")
    def enable_after_fetch(self,**extra):
        cfg=core.settings();cfg.update(ai_after_fetch=True,**extra)
        core.atomic_json(core.safe_path("config","settings.json"),cfg)
    def test_fetch_then_auto_ai_only_reads_unprocessed_candidates_once(self):
        self.enable_after_fetch()
        rows=[self.row(f"2610.{i:05}") for i in range(25)]
        self.save("weekly","2026-W44",rows)
        with core.db() as con:
            con.execute("UPDATE papers SET ai_state='done',translation='已有翻译' WHERE id='2610.00000'")
            con.execute("UPDATE papers SET ai_state='failed' WHERE id='2610.00001'")
        seen=[]
        def reading(pid,cfg,deadline):
            self.assertFalse(tasks.lane_status("fetch")["running"])
            self.assertTrue(tasks.lane_status("ai")["running"])
            with core.FileLock("fetch.lock"):
                pass
            seen.append(pid)
            with core.db() as con:con.execute("UPDATE papers SET ai_state='done' WHERE id=?",(pid,))
        with patch.object(core,"get_key",return_value="fake"),patch.object(core,"fetch_report",return_value=(rows,25)),patch.object(core,"ai_read",side_effect=reading):
            tasks.run("manual",core.period("weekly","2026-W44"))
        self.assertEqual(seen,[f"2610.{i:05}" for i in range(2,22)])
        with core.db() as con:
            self.assertEqual(con.execute("SELECT translation FROM papers WHERE id='2610.00000'").fetchone()[0],"已有翻译")
    def test_auto_fetch_chains_ai_and_repeated_start_does_not_read_twice(self):
        self.enable_after_fetch(auto=True,time="09:00",weekly=False,monthly=False,backfill_days=1)
        def fetching(spec,deadline):
            if spec["period"]==core.now().date().isoformat():
                return [self.row()],1
            raise core.ReportUnavailable("no day")
        def reading(pid,cfg,deadline):
            with core.db() as con:con.execute("UPDATE papers SET ai_state='done' WHERE id=?",(pid,))
        with patch.object(core,"get_key",return_value="fake"),patch.object(core,"fetch_latest_daily") as latest,patch.object(core,"fetch_report",side_effect=fetching) as fetch,patch.object(core,"ai_read",side_effect=reading) as ai:
            tasks.run("auto");tasks.run("auto")
            latest.assert_not_called();ai.assert_called_once()
            self.assertEqual(fetch.call_count,3)
    def test_auto_ai_without_key_or_with_failed_key_keeps_abstract(self):
        self.enable_after_fetch()
        for failure in [None,RuntimeError("cannot decrypt")]:
            with self.subTest(key=failure),patch.object(core,"get_key",return_value=None,side_effect=failure),patch.object(core,"fetch_report",return_value=([self.row()],1)),patch.object(core,"ai_read") as ai:
                tasks.run("manual",core.period("daily","2026-10-30"))
                ai.assert_not_called()
                self.assertEqual(core.stats()["papers"],1)
    def test_auto_ai_failure_stops_batch_without_retry_and_keeps_remaining_raw(self):
        self.enable_after_fetch()
        rows=[self.row(),self.row("2610.54321")]
        with patch.object(core,"get_key",return_value="fake"),patch.object(core,"fetch_report",return_value=(rows,2)),patch.object(core,"ai_read",side_effect=RuntimeError("offline")) as ai:
            tasks.run("manual",core.period("daily","2026-10-30"))
            ai.assert_called_once()
        with core.db() as con:
            self.assertEqual([r[0] for r in con.execute("SELECT ai_state FROM papers ORDER BY id")],["failed","raw"])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM reports").fetchone()[0],1)
    def test_auto_ai_respects_daily_budget_without_marking_unattempted_paper_failed(self):
        self.enable_after_fetch(daily_ai_limit=1)
        with core.db() as con:con.execute("INSERT INTO usage(day,calls) VALUES (?,1)",(core.now().date().isoformat(),))
        with patch.object(core,"get_key",return_value="fake"),patch.object(core,"fetch_report",return_value=([self.row()],1)),patch.object(core,"ai_read") as ai:
            tasks.run("manual",core.period("daily","2026-10-30"))
            ai.assert_not_called()
        with core.db() as con:self.assertEqual(con.execute("SELECT ai_state FROM papers").fetchone()[0],"raw")
    def test_stop_ai_during_fetch_suppresses_automatic_handoff(self):
        self.enable_after_fetch()
        def fetching(*args):
            tasks.stop("ai")
            return [self.row()],1
        with patch.object(core,"get_key",return_value="fake"),patch.object(core,"fetch_report",side_effect=fetching),patch.object(core,"ai_read") as ai:
            tasks.run("manual",core.period("daily","2026-10-30"))
            ai.assert_not_called()
        self.assertEqual(core.stats()["papers"],1)
    def test_disabling_automatic_ai_during_batch_stops_next_request(self):
        self.enable_after_fetch()
        rows=[self.row(),self.row("2610.54321")]
        def reading(*args):
            tasks.save_settings({"ai_after_fetch":False})
        with patch.object(core,"get_key",return_value="fake"),patch.object(core,"fetch_report",return_value=(rows,2)),patch.object(core,"ai_read",side_effect=reading) as ai:
            tasks.run("manual",core.period("daily","2026-10-30"))
            ai.assert_called_once()
        self.assertIn("已停止",tasks.lane_status("ai")["message"])
    def test_automatic_handoff_does_not_interfere_with_existing_ai_lock(self):
        self.enable_after_fetch()
        with core.FileLock("ai.lock"),patch.object(core,"fetch_report",return_value=([self.row()],1)),patch.object(core,"ai_read") as ai:
            tasks.run("manual",core.period("daily","2026-10-30"))
            ai.assert_not_called()
        self.assertEqual(core.stats()["papers"],1)
    def test_intro_limit_and_json_validation(self):
        translation,tags,intro=core.parse_reading({"translation":"摘要","tags":["语言模型","推理","训练"],"intro":"一句介绍。"*100},100)
        self.assertLessEqual(len(intro),100)
        self.assertTrue(intro.endswith("…"))
        for malformed in [{"translation":"","tags":[],"intro":"简介"}, {"translation":"摘要","tags":["一个"],"intro":"简介"}]:
            with self.assertRaises(ValueError): core.parse_reading(malformed,300)
    def test_key_is_encrypted_and_roundtrips(self):
        key="sk-fake-unit-test-key-only"
        core.save_key(key)
        self.assertEqual(core.get_key(),key)
        self.assertNotIn(key.encode(),(self.root/"config"/"key.dat").read_bytes())
    def test_ai_budget_counts_attempt_and_does_not_retry(self):
        self.save("daily","2026-10-30")
        cfg=core.settings();cfg["daily_ai_limit"]=1
        with patch.object(core,"get_key",return_value="test-key"),patch.object(core,"request",side_effect=RuntimeError("offline")) as request:
            with self.assertRaises(RuntimeError): core.ai_read("2610.12345",cfg)
            with self.assertRaisesRegex(ValueError,"上限"): core.ai_read("2610.12345",cfg)
            self.assertEqual(request.call_count,1)
        self.assertEqual(core.stats()["usage"]["calls"],1)
    def test_exclusive_task_lock(self):
        with core.FileLock():
            with self.assertRaises(RuntimeError):
                with core.FileLock(): pass
    def test_no_project_path_escape(self):
        with self.assertRaises(ValueError): core.safe_path("..","outside.txt")
        with self.assertRaises(ValueError): core.safe_path(r"C:\Windows\test.txt")
    def test_uninstall_preserves_unregistered_file(self):
        case=self.root/"uninstall_case";case.mkdir()
        (case/"config").mkdir()
        shutil.copy2(REAL_ROOT/"uninstall.ps1",case/"uninstall.ps1")
        (case/"schedule.ps1").write_text("param([string]$Action)\nexit 0","utf-8-sig")
        (case/"owned.txt").write_text("owned","utf-8")
        (case/"中文文件.txt").write_text("中文资料","utf-8")
        (case/"keep.txt").write_text("unrelated","utf-8")
        (case/"data").mkdir()
        (case/"config"/"project.json").write_text(json.dumps({"app_id":core.APP_ID,"root":str(case)}),"utf-8")
        (case/"config"/"manifest.json").write_text(json.dumps({"files":["owned.txt","中文文件.txt","schedule.ps1","uninstall.ps1","config/manifest.json"],"dirs":["data","config"]}),"utf-8")
        done=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-File",str(case/"uninstall.ps1"),"-ConfirmId",core.APP_ID],capture_output=True,timeout=25)
        self.assertEqual(done.returncode,0,done.stderr.decode(errors="replace"))
        self.assertFalse((case/"owned.txt").exists())
        self.assertFalse((case/"中文文件.txt").exists())
        self.assertEqual((case/"keep.txt").read_text(),"unrelated")
    def test_uninstall_rejects_manifest_traversal_before_deleting(self):
        case=self.root/"unsafe_case";case.mkdir();(case/"config").mkdir()
        shutil.copy2(REAL_ROOT/"uninstall.ps1",case/"uninstall.ps1")
        (case/"config"/"project.json").write_text(json.dumps({"app_id":core.APP_ID,"root":str(case)}),"utf-8")
        (case/"config"/"manifest.json").write_text(json.dumps({"files":["../outside.txt"],"dirs":[]}),"utf-8")
        done=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-File",str(case/"uninstall.ps1"),"-ConfirmId",core.APP_ID],capture_output=True,timeout=25)
        self.assertNotEqual(done.returncode,0)
        self.assertTrue((case/"uninstall.ps1").exists())
    def test_ui_rejects_missing_token_bad_host_and_cross_origin(self):
        server=app.Server(("127.0.0.1",0),app.Handler)
        app.PORT=server.server_port
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f"http://127.0.0.1:{app.PORT}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base+"/api/settings")
            self.assertEqual(error.exception.code,403)
            req=urllib.request.Request(base+"/api/settings",headers={"X-Session-Token":app.TOKEN})
            with urllib.request.urlopen(req) as response:
                self.assertEqual(json.loads(response.read())["settings"]["auto"],False)
            req=urllib.request.Request(base+"/api/settings",headers={"Host":"attacker.example","X-Session-Token":app.TOKEN})
            with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(req)
            self.assertEqual(error.exception.code,403)
            req=urllib.request.Request(base+"/api/stop",data=b"{}",headers={"X-Session-Token":app.TOKEN,"Origin":"https://attacker.example"})
            with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(req)
            self.assertEqual(error.exception.code,403)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_acquisition_never_loads_key_even_when_ai_is_configured(self):
        cfg=core.settings();cfg.update(auto=True,time="09:00",daily=True,weekly=False,monthly=False,backfill_days=1)
        core.atomic_json(core.safe_path("config","settings.json"),cfg)
        with patch.object(core,"get_key",side_effect=AssertionError("acquisition must not read key")) as key,patch.object(core,"fetch_latest_daily",side_effect=self.latest_from_mock),patch.object(core,"fetch_report",return_value=([self.row()],1)) as fetch,patch.object(core,"ai_read") as ai:
            tasks.run("auto")
            self.assertEqual(fetch.call_count,3)
            key.assert_not_called();ai.assert_not_called()
    def test_ai_only_processes_requested_papers(self):
        self.save("daily","2026-10-30")
        with patch.object(core,"ai_read") as ai,patch.object(core,"fetch_report") as fetch:
            tasks.run("ai",pids=["2610.12345"])
            ai.assert_called_once()
            fetch.assert_not_called()
    def test_ai_batch_is_bounded(self):
        with patch.object(core,"ai_read") as ai:
            tasks.run("ai",pids=[f"2610.{i:05}" for i in range(30)])
            self.assertEqual(ai.call_count,20)
    def test_redirect_supports_request_without_explicit_method(self):
        req=urllib.request.Request("https://huggingface.co/papers/date/2026-10-31")
        redirect=core.SafeRedirect().redirect_request(req,None,302,"Found",{},"https://huggingface.co/papers/date/2026-10-30")
        self.assertEqual(redirect.get_method(),"GET")
        with self.assertRaises(ValueError):
            core.SafeRedirect().redirect_request(req,None,302,"Found",{},"https://attacker.example/")
        post=urllib.request.Request("https://api.deepseek.com/chat/completions",data=b"{}")
        with self.assertRaises(ValueError):
            core.SafeRedirect().redirect_request(post,None,302,"Found",{},"https://api.deepseek.com/other")
    def test_old_tags_are_normalized_without_losing_content_or_keywords(self):
        self.save("daily","2026-10-30")
        old=["LLM","多模态大模型","基准评测","Harness演化"]
        with core.db() as con:
            con.execute("UPDATE papers SET tags=?,tag_version=0,translation='已有翻译',intro='已有简介',ai_state='done'",
                        (json.dumps(old),))
        with patch.object(core,"request") as request:
            core.initialize();core.initialize()
            request.assert_not_called()
        with core.db() as con:
            row=con.execute("SELECT * FROM papers").fetchone()
        self.assertEqual(set(json.loads(row["keywords"])),set(old))
        self.assertTrue(set(json.loads(row["tags"])).issubset(set(core.taxonomy.CATALOG)))
        self.assertLessEqual(len(json.loads(row["tags"])),4)
        self.assertEqual((row["translation"],row["intro"]),("已有翻译","已有简介"))
        self.assertEqual(len(tasks.list_data({"q":"Harness演化"})["papers"]),1)
    def test_manual_categories_cannot_expand_the_vocabulary(self):
        with self.assertRaises(ValueError): core.taxonomy.validate_manual(["随便造一个词"])
        self.assertEqual(core.taxonomy.validate_manual(["智能体","智能体"]),["智能体"])
        with self.assertRaises(ValueError): core.taxonomy.validate_manual(core.taxonomy.CATALOG[:5])
    def test_cancelled_ai_does_not_start_request_or_spend_budget(self):
        self.save("daily","2026-10-30")
        event=threading.Event();event.set()
        with core.cancellation_scope(event),patch.object(core,"request") as request:
            with self.assertRaises(InterruptedError):
                core.ai_read("2610.12345",core.settings())
            request.assert_not_called()
        self.assertEqual(core.stats()["usage"]["calls"],0)
    def test_ai_does_not_overwrite_result_for_changed_abstract(self):
        self.save("daily","2026-10-30")
        with core.db() as con: con.execute("UPDATE papers SET translation='原来的翻译'")
        def response(*args,**kwargs):
            with core.db() as con: con.execute("UPDATE papers SET abstract='Changed source'")
            return json.dumps({"choices":[{"finish_reason":"stop","message":{"content":json.dumps(
                {"translation":"过期的翻译","tags":["大语言模型","推理与规划"],"intro":"简介"})}}]})
        with patch.object(core,"get_key",return_value="fake-test"),patch.object(core,"request",side_effect=response):
            with self.assertRaisesRegex(ValueError,"摘要已更新"):
                core.ai_read("2610.12345",core.settings())
        with core.db() as con:
            self.assertEqual(con.execute("SELECT translation FROM papers").fetchone()[0],"原来的翻译")
    def test_stop_buttons_are_independent_while_both_tasks_run(self):
        import time
        self.save("daily","2026-10-30")
        def wait_idle(kind,seconds=3):
            until=time.monotonic()+seconds
            while tasks.lane_status(kind)["running"] and time.monotonic()<until:
                time.sleep(.01)
            self.assertFalse(tasks.lane_status(kind)["running"])
        for first in ["fetch","ai"]:
            with self.subTest(stop=first):
                began={"fetch":threading.Event(),"ai":threading.Event()}
                def fetching(*args,**kwargs):
                    began["fetch"].set()
                    until=time.monotonic()+5
                    while not core.is_cancelled() and time.monotonic()<until: time.sleep(.01)
                    raise InterruptedError("cancelled")
                def reading(*args,**kwargs):
                    began["ai"].set()
                    until=time.monotonic()+5
                    while not core.is_cancelled() and time.monotonic()<until: time.sleep(.01)
                    raise InterruptedError("cancelled")
                with patch.object(core,"get_key",return_value="fake"),patch.object(core,"fetch_report",side_effect=fetching),patch.object(core,"ai_read",side_effect=reading):
                    try:
                        tasks.start("manual",spec=core.period("daily","2026-10-30"))
                        tasks.start("ai",pids=["2610.12345"])
                        self.assertTrue(began["fetch"].wait(2));self.assertTrue(began["ai"].wait(2))
                        tasks.stop(first);wait_idle(first)
                        other="ai" if first=="fetch" else "fetch"
                        self.assertTrue(tasks.lane_status(other)["running"])
                        self.assertFalse(tasks.EVENTS[other].is_set())
                        tasks.stop(other);wait_idle(other)
                    finally:
                        tasks.stop()
                        wait_idle("fetch");wait_idle("ai")

    def test_stopping_external_worker_does_not_leave_local_lane_running(self):
        state=tasks.fresh("fetch")|{"running":True,"message":"external worker"}
        core.atomic_json(tasks.progress_path("fetch"),state)
        with core.FileLock("fetch.lock"):
            tasks.stop("fetch")
            self.assertFalse(tasks.LANES["fetch"]["running"])
            self.assertTrue(tasks.lane_status("fetch")["running"])
            core.atomic_json(tasks.progress_path("fetch"),tasks.fresh("fetch")|{"message":"已停止"})
        self.assertFalse(tasks.lane_status("fetch")["running"])

if __name__=="__main__":
    unittest.main(verbosity=2)
