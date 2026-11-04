"""Loopback-only UI. No packages, no elevated service, no telemetry."""
from __future__ import annotations
import ctypes, http.server, json, os, secrets, subprocess, threading, time, urllib.parse, webbrowser, sys
import core, tasks, taxonomy

TOKEN = secrets.token_urlsafe(32)
CLIENTS = {}
CLIENT_LOCK = threading.Lock()
SHUTDOWN = threading.Event()
PORT = 0
SERVER = None
STARTED = time.monotonic()
INSTANCE = None

def singleton():
    global INSTANCE
    kernel = ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p,ctypes.c_bool,ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    INSTANCE = kernel.CreateMutexW(None,False,"Local\\"+core.APP_ID+"_UI")
    if not INSTANCE:
        raise RuntimeError("无法创建本地程序锁")
    if ctypes.get_last_error()==183:
        path = core.safe_path("data","ui.json")
        try:
            info = json.loads(path.read_text("utf-8"))
            if 1024<=int(info["port"])<=65535 and isinstance(info["token"],str):
                webbrowser.open(f"http://127.0.0.1:{info['port']}/?token={info['token']}")
        except (OSError,ValueError,KeyError):
            pass
        return False
    return True

def end_app():
    tasks.stop()
    SHUTDOWN.set()
    if SERVER:
        SERVER.shutdown()

class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8
    permits = threading.BoundedSemaphore(12)
    def process_request(self,request,address):
        if not self.permits.acquire(blocking=False):
            self.close_request(request)
            return
        try:
            super().process_request(request,address)
        except BaseException:
            self.permits.release()
            raise
    def process_request_thread(self,request,address):
        try:
            super().process_request_thread(request,address)
        finally:
            self.permits.release()
    def verify_request(self,request,address):
        return address[0]=="127.0.0.1"
    def get_request(self):
        request,address = super().get_request()
        request.settimeout(10)
        return request,address

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "LocalPaperReader"
    sys_version = ""
    def log_message(self,*args):
        pass
    def reply(self,data,code=200,content_type="application/json; charset=utf-8"):
        if isinstance(data,(dict,list)):
            data = json.dumps(data,ensure_ascii=False).encode()
        elif isinstance(data,str):
            data = data.encode()
        self.send_response(code)
        self.send_header("Content-Type",content_type)
        self.send_header("Content-Length",str(len(data)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Referrer-Policy","no-referrer")
        self.send_header("Cross-Origin-Resource-Policy","same-origin")
        self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; img-src 'none'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError):
            pass
    def valid_host(self):
        return self.headers.get("Host","")==f"127.0.0.1:{PORT}"
    def authenticated(self):
        return self.valid_host() and secrets.compare_digest(self.headers.get("X-Session-Token",""),TOKEN)
    def do_GET(self):
        try:
            if not self.valid_host():
                return self.reply({"error":"访问地址无效"},403)
            parsed = urllib.parse.urlsplit(self.path)
            query = {k:v[-1] for k,v in urllib.parse.parse_qs(parsed.query).items()}
            if parsed.path=="/":
                if not secrets.compare_digest(query.get("token",""),TOKEN):
                    return self.reply("请通过 D:\\classproject\\game\\paper-reader 中的启动入口打开。",403,"text/plain; charset=utf-8")
                return self.reply(core.safe_path("static","index.html").read_bytes(),content_type="text/html; charset=utf-8")
            if parsed.path in ["/static/app.js","/static/style.css"]:
                name = parsed.path.rsplit("/",1)[1]
                mime = "text/javascript" if name.endswith(".js") else "text/css"
                return self.reply(core.safe_path("static",name).read_bytes(),content_type=mime+"; charset=utf-8")
            if not self.authenticated():
                return self.reply({"error":"会话无效，请重新打开程序"},403)
            if parsed.path=="/api/data":
                return self.reply(tasks.list_data(query))
            if parsed.path=="/api/appearance":
                return self.reply(core.appearance())
            if parsed.path=="/api/settings":
                return self.reply({"settings":core.settings(),"has_key":core.safe_path("config","key.dat").exists(),
                    "task_installed":tasks.task_exists(),"root":str(core.ROOT),
                    "latest":core.current_periods(),"status":tasks.status()})
            if parsed.path=="/api/status":
                return self.reply(tasks.status())
            if parsed.path=="/api/history":
                with core.db() as con:
                    rows = [dict(r) for r in con.execute("SELECT * FROM attempts ORDER BY run_date DESC,report_id LIMIT 60")]
                return self.reply({"attempts":rows})
            return self.reply({"error":"页面不存在"},404)
        except (ValueError,KeyError,TypeError) as exc:
            self.reply({"error":str(exc)},400)
        except Exception:
            self.reply({"error":"本地读取失败，请查看项目状态"},500)
    def do_POST(self):
        try:
            if not self.valid_host():
                return self.reply({"error":"访问地址无效"},403)
            origin = self.headers.get("Origin")
            if origin and origin!=f"http://127.0.0.1:{PORT}":
                return self.reply({"error":"拒绝跨站请求"},403)
            length = int(self.headers.get("Content-Length","0"))
            if not 0<=length<=64000:
                return self.reply({"error":"请求过大"},413)
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value,dict):
                raise ValueError("请求格式无效")
            path = urllib.parse.urlsplit(self.path).path
            beacon = path=="/api/closing" and secrets.compare_digest(str(value.get("token","")),TOKEN)
            if not self.authenticated() and not beacon:
                return self.reply({"error":"会话无效，请重新打开程序"},403)
            if path in ["/api/heartbeat","/api/closing"]:
                client = str(value.get("client",""))[:100]
                with CLIENT_LOCK:
                    if path.endswith("closing"):
                        CLIENTS.pop(client,None)
                    else:
                        CLIENTS[client] = time.monotonic()
                return self.reply({"ok":True})
            if core.safe_path("config","uninstalling").exists():
                raise ValueError("正在卸载，已停止接受新操作")
            if path=="/api/appearance":
                return self.reply(core.save_appearance(value))
            if path=="/api/settings":
                cfg = tasks.save_settings(value)
                return self.reply({"ok":True,"settings":cfg})
            if path=="/api/key":
                if tasks.status()["ai"]["running"]:
                    raise ValueError("请先停止 AI 调用再更换密钥")
                if value.get("delete"):
                    core.safe_path("config","key.dat").unlink(missing_ok=True)
                else:
                    core.save_key(value.get("key",""))
                return self.reply({"ok":True})
            if path=="/api/models":
                if tasks.status()["ai"]["running"]:
                    raise ValueError("AI 正在运行，停止后再测试连接")
                core.CANCEL.clear()
                key = core.get_key()
                if not key:
                    raise ValueError("请先保存 API Key")
                result = json.loads(core.request("https://api.deepseek.com/models",key=key,max_bytes=256000))
                models = [m["id"] for m in result.get("data",[]) if isinstance(m,dict) and isinstance(m.get("id"),str)]
                return self.reply({"ok":True,"models":models})
            if path=="/api/run":
                kind,period = value.get("kind"),value.get("period")
                spec = core.period(kind,period) if kind and period else None
                tasks.start("manual",spec=spec)
                return self.reply({"ok":True})
            if path=="/api/read":
                ids = value.get("ids",[])
                if not isinstance(ids,list) or not 1<=len(ids)<=20 or any(not isinstance(x,str) for x in ids):
                    raise ValueError("请选择 1 至 20 篇论文")
                tasks.start("ai",pids=ids)
                return self.reply({"ok":True})
            if path=="/api/tags":
                tags=taxonomy.validate_manual(value.get("tags"))
                pid=value.get("id")
                with core.db() as con:
                    con.execute("UPDATE papers SET tags=?,tag_version=1 WHERE id=?",
                                (json.dumps(tags,ensure_ascii=False),pid))
                return self.reply({"ok":True})
            if path=="/api/stop":
                tasks.stop(value.get("kind"))
                return self.reply({"ok":True})
            if path=="/api/disable":
                cfg = tasks.save_settings({"auto":False})
                tasks.stop("fetch")
                return self.reply({"ok":True,"settings":cfg})
            if path=="/api/export":
                # Export only within the managed project directory and retention range.
                return self.reply({"ok":True,"path":core.export_data()})
            if path=="/api/uninstall-preview":
                preview = tasks.powershell("uninstall.ps1","-Preview")
                return self.reply(json.loads(preview))
            if path=="/api/uninstall":
                if value.get("confirm")!="删除整个程序":
                    raise ValueError("请完整输入：删除整个程序")
                # Remove task before starting cleanup. If this fails, keep application files.
                tasks.save_settings({"auto":False})
                tasks.stop()
                core.safe_path("config","uninstalling").write_text("stop","ascii")
                subprocess.Popen(["powershell.exe","-NoLogo","-NoProfile","-NonInteractive",
                    "-ExecutionPolicy","Bypass","-File",str(core.safe_path("uninstall.ps1")),
                    "-WaitPid",str(os.getpid()),"-ConfirmId",core.APP_ID],
                    cwd=core.ROOT,creationflags=0x08000000)
                self.reply({"ok":True})
                threading.Thread(target=end_app,daemon=True).start()
                return
            if path=="/api/exit":
                self.reply({"ok":True})
                threading.Thread(target=end_app,daemon=True).start()
                return
            self.reply({"error":"操作不存在"},404)
        except (ValueError,RuntimeError,KeyError,TypeError) as exc:
            self.reply({"error":str(exc)},400)
        except Exception:
            self.reply({"error":"操作失败，资料已保留；未自动重试"},500)

def watchdog():
    while not SHUTDOWN.wait(2):
        with CLIENT_LOCK:
            alive = any(time.monotonic()-last<120 for last in CLIENTS.values())
        if not alive and time.monotonic()-STARTED>120:
            end_app()
            return

def main():
    global PORT,SERVER
    core.initialize()
    if not singleton():
        return
    core.cleanup()
    SERVER = Server(("127.0.0.1",0),Handler)
    PORT = SERVER.server_port
    core.atomic_json(core.safe_path("data","ui.json"),{"port":PORT,"token":TOKEN,"pid":os.getpid()})
    threading.Thread(target=watchdog,daemon=True).start()
    if core.settings()["auto"] and "--no-auto" not in sys.argv:
        tasks.start("auto")
    webbrowser.open(f"http://127.0.0.1:{PORT}/?token={TOKEN}")
    try:
        SERVER.serve_forever(poll_interval=.3)
    finally:
        SHUTDOWN.set()
        tasks.stop()
        # Let bounded network requests finish before the process releases its files.
        until = time.monotonic()+25
        while tasks.STATE.get("running") and time.monotonic()<until:
            time.sleep(.1)
        SERVER.server_close()
        core.safe_path("data","ui.json").unlink(missing_ok=True)

if __name__=="__main__":
    try:
        main()
    except Exception as exc:
        # Do not place secrets or request bodies in a crash log.
        try:
            import logging
            logging.getLogger("papers").error("Startup failure: %s",type(exc).__name__)
            ctypes.windll.user32.MessageBoxW(None,"启动失败。请确认目录完整，并查看 D:\\classproject\\game\\paper-reader\\logs 中的运行记录。","论文助手",0x10)
        except Exception:
            pass
