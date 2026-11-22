"""Local-only desktop web UI, packaged with PyInstaller on Windows."""
import argparse
import hmac
import json
import os
import secrets
import sys
import tempfile
import time
import threading
from urllib.parse import unquote
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import Controller, DeviceError
from light_control import LightController
from ai_status import AiCoordinator, publish_endpoint
from desktop_tray import DesktopTray, SingleInstance
from screen_preferences import screen_target
from startup import AutoConnector
from audio_desk import AudioDesk
from discovery import Discovery


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0, demo=False, preferences=None, local_only=False):
        super().__init__(("127.0.0.1", port), Handler)
        self.controller = Controller(demo=demo)
        self.local_only = local_only
        if local_only:
            self.controller._set(app_version='2.6.0', message='Nano 本地版：选择 USB 灯板')
        else:
            self.controller._set(app_version='3.1.3')
        self.lights = LightController(self.controller.log, demo=demo, local_only=local_only)
        self.connection_lock = threading.RLock()
        self.ai = AiCoordinator(self.lights, self.connection_lock, self.controller.log, demo=demo, preferences=preferences, local_only=local_only)
        self.desktop = {"tray": False, "single_instance": False}
        self.ai_token = secrets.token_urlsafe(32)
        self.token = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.assets = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / ("web-local" if local_only else "web")
        self.startup = AutoConnector(self)
        self.audio = AudioDesk(self)
        self.discovery = Discovery(self)
        self.last_ui=0.0
        self.ai.ui_recent=lambda:time.monotonic()-self.last_ui < 8


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, data, mime="application/json; charset=utf-8",extra_headers=()):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if isinstance(data, (dict, list)) else data
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for header,value in extra_headers:self.send_header(header,value)
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def allowed(self, api=False):
        expected = self.server.origin.removeprefix("http://")
        if self.headers.get("Host") != expected:
            self.reply(403, {"error": "主机不匹配"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            self.reply(403, {"error": "只接受本机界面的请求"})
            return False
        event_auth = self.path == "/api/ai_event" and hmac.compare_digest(self.headers.get("X-AI-Token", ""), self.server.ai_token)
        if api and not event_auth and not hmac.compare_digest(self.headers.get("X-Session-Token", ""), self.server.token):
            self.reply(403, {"error": "会话不匹配，请重新打开程序界面"})
            return False
        return True

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if not self.allowed(api=path.startswith("/api/")):
            return
        if path == "/api/state":
            self.last_ui=time.monotonic()
            self.reply(200, {**self.server.controller.snapshot(), 'lights': self.server.lights.snapshot(), 'ai': self.server.ai.snapshot(), 'desktop': self.server.desktop, 'startup': self.server.startup.snapshot(), 'local_only': self.server.local_only})
        elif path == "/api/ports":
            self.reply(200, self.server.controller.ports())
        elif path == "/api/discover" and not self.server.local_only:
            self.reply(200,self.server.discovery.snapshot())
        elif path == "/api/audio" and not self.server.local_only:
            self.reply(200,self.server.audio.snapshot())
        elif path in ('/audio.js','/discovery.js','/unified.css') and not self.server.local_only:
            mime='text/javascript' if path.endswith('js') else 'text/css'
            self.reply(200,(self.server.assets/path[1:]).read_bytes(),mime+'; charset=utf-8')
        elif path == "/session.js":
            self.reply(200, ("window.HLK_SESSION=" + json.dumps(self.server.token) + ";").encode(), "text/javascript; charset=utf-8")
        elif self.server.local_only and path not in ('/', '/app.js', '/style.css', '/favicon.ico'):
            self.reply(404, {"error": "Nano 本地版没有此功能"})
        elif path in ("/", "/app.js", "/style.css", "/nano-wiring.svg", "/Nano_HLK_M30.ino"):
            filename = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/nano-wiring.svg": "nano-wiring.svg", "/Nano_HLK_M30.ino": "Nano_HLK_M30.ino"}[path]
            mime = {"/": "text/html", "/app.js": "text/javascript", "/style.css": "text/css", "/nano-wiring.svg": "image/svg+xml", "/Nano_HLK_M30.ino": "text/plain"}[path]
            self.reply(200, (self.server.assets / filename).read_bytes(), mime + "; charset=utf-8")
        elif path in ("/voice-wiring.png", "/voice-guide.txt"):
            mime = "image/png" if path.endswith(".png") else "text/plain; charset=utf-8"
            self.reply(200, (self.server.assets / path[1:]).read_bytes(), mime)
        elif path == "/favicon.ico":
            self.reply(204, b"", "image/x-icon")
        else:
            self.reply(404, {"error": "未找到"})

    def audio_import(self):
        if self.server.local_only:
            self.reply(404,{"error":"此模式不含 C3 音频"});return
        if self.headers.get('Content-Type','').split(';',1)[0]!='application/octet-stream':
            self.reply(415,{"error":"音频需要二进制请求"});return
        p=None
        try:
            n=int(self.headers.get('Content-Length','0'))
            if not 0<n<=32*1024*1024:raise ValueError('导入文件不得超过 32 MB。')
            if self.server.audio.snapshot()['busy']:raise DeviceError('音频工作区正在忙。')
            name=unquote(self.headers.get('X-Audio-Name','本地音频'))[:128]
            with tempfile.NamedTemporaryFile(prefix='ailamp-audio-',delete=False) as t:
                p=t.name
                self.connection.settimeout(12)
                while n:
                    chunk=self.rfile.read(min(n,65536))
                    if not chunk:raise ValueError('上传未完成。')
                    t.write(chunk);n-=len(chunk)
            self.server.audio.start('import',{'name':name},file=p)
            p=None
            self.reply(202,{"ok":True})
        except (DeviceError,ValueError,OSError) as e:
            self.reply(409,{"error":str(e)})
        finally:
            if p:Path(p).unlink(missing_ok=True)

    def do_POST(self):
        if not self.allowed(api=True):
            return
        if self.path=='/api/audio_import':
            self.audio_import();return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self.reply(415, {"error": "需要 JSON 请求"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= (16384 if self.path in ('/api/ai_ack_errors','/api/audio_job','/api/audio_voice_preview') else 4096):
                raise ValueError()
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            self.reply(400, {"error": "请求格式不正确"})
            return
        action = self.path.removeprefix("/api/")
        try:
            if self.server.local_only and action not in {
                    'ai_event', 'ai_settings', 'ai_ack_errors', 'ai_control', 'ai_preview',
                    'ai_codex', 'startup_settings', 'startup_resume', 'light_connect',
                    'light_send', 'light_disconnect', 'exit'}:
                raise DeviceError("Nano 本地版仅支持 USB 灯控与本机 AI 状态")
            if self.path == "/api/discover":
                if self.server.local_only:raise DeviceError("此模式不扫描网络")
                self.server.discovery.start(data.get('kind'))
            elif self.path == "/api/audio_job":
                if self.server.local_only:raise DeviceError("此模式不含 C3 音频")
                self.server.audio.start(data.get('op'),data)
            elif self.path == "/api/audio_cancel":
                self.server.audio.cancel_live()
            elif self.path == "/api/audio_preview":
                if self.server.local_only:raise DeviceError("此模式不含 C3 音频")
                self.reply(200,self.server.audio.preview(data),'audio/wav')
                return
            elif self.path == "/api/audio_voice_preview":
                self.reply(200,self.server.audio.preview_voice(data),'audio/wav')
                return
            elif self.path == "/api/audio_recording_preview":
                audio,mode=self.server.audio.preview_recording(data)
                self.reply(200,audio,'audio/wav',(('X-C3-Readback',mode),))
                return
            elif self.path == "/api/ai_event":
                if data.get("source") == "Codex-local":
                    raise DeviceError("Codex-local 名称由本地监听器保留，请使用其他 source")
                self.server.ai.registry.event(data)
            elif self.path == '/api/startup_settings':
                with self.server.connection_lock:
                    self.server.startup.configure(data)
            elif self.path == '/api/startup_resume':
                with self.server.connection_lock:
                    self.server.startup.resume()
            elif self.path == '/api/light_sound':
                with self.server.connection_lock:
                    self.server.lights.start('light_sound',data)
            elif self.path == '/api/screen_notice':
                with self.server.connection_lock:
                    settings=self.server.ai.screen_preferences.snapshot()
                    self.server.lights.start('light_notice',{**settings,'state':screen_target(self.server.ai.snapshot()),'text':data.get('text'),'seconds':data.get('seconds')})
            elif self.path == '/api/screen_settings':
                with self.server.connection_lock:
                    self.server.ai.screen_preferences.save(data)
                    self.server.ai.screen_applied = None
            elif self.path == "/api/ai_settings":
                with self.server.connection_lock:
                    self.server.ai.configure(data.get("patterns"))
            elif self.path == "/api/ai_ack_errors":
                self.server.ai.acknowledge_errors(data.get('events'))
            elif self.path == "/api/ai_control":
                with self.server.connection_lock:
                    self.server.ai.set_enabled(data.get("enabled"))
            elif self.path == "/api/ai_preview":
                with self.server.connection_lock:
                    self.server.ai.demonstrate(data.get("state"))
            elif self.path == "/api/ai_codex":
                if type(data.get("enabled")) is not bool:
                    raise DeviceError("enabled 必须为布尔值")
                self.server.ai.observer.enabled = data["enabled"]
            elif self.path == "/api/stop":
                self.server.controller.cancel()
            elif self.path == "/api/disconnect":
                self.server.controller.cancel(disconnect=True)
            elif self.path == "/api/exit":
                if self.server.lights.snapshot()['busy'] or self.server.audio.snapshot()['busy'] or self.server.controller.snapshot()['busy']:
                    raise DeviceError('设备或音频正在操作，请结束后再退出。')
                self.reply(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            elif self.path in ("/api/light_connect", "/api/light_send", "/api/light_disconnect"):
                with self.server.connection_lock:
                    module = self.server.controller.snapshot()
                    if module["busy"] or module["connected"]:
                        raise DeviceError("请先释放左侧 AT 连接，再使用灯控调试")
                    if action == "light_disconnect" or (action == "light_send" and data.get("command") not in ("STATUS", "HELP")):
                        self.server.ai.detach()
                    self.server.lights.start(action, data)
                    self.server.startup.manual_started(action, data)
            elif self.path in ("/api/connect", "/api/detect", "/api/read", "/api/status", "/api/audit", "/api/configure", "/api/transparent", "/api/upgrade", "/api/return_at", "/api/nano_baud"):
                with self.server.connection_lock:
                    lights = self.server.lights.snapshot()
                    if lights["busy"] or lights["connected"]:
                        raise DeviceError("请先断开灯控连接，再配置模块 AT")
                    if action=='connect' and data.get('transport','serial')=='serial':
                        if any(p.get('device')==data.get('port') and p.get('kind')=='esp32c3' for p in self.server.controller.ports()):
                            raise DeviceError('C3 原生 USB 在灯控调试连接，不使用 M30 的 AT 配置。')
                    self.server.controller.start(action, data)
                    self.server.startup.manual_started(action, data)
            else:
                self.reply(404, {"error": "不支持的操作"})
                return
            self.reply(202, {"ok": True})
        except (DeviceError,ValueError,TypeError) as exc:
            self.reply(409, {"error": str(exc)})


def main(local_only=False):
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="Offline simulator; no hardware access")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--self-test", metavar="REPORT", help="Offline package diagnostics")
    parser.add_argument("--autostart", action="store_true", help="Start quietly after Windows sign-in")
    parser.add_argument("--keep-follow-paused", action="store_true", help="Do not start AI follow on the first automatic connection")
    parser.add_argument("--signal", choices=["idle","running","waiting","error","offline","heartbeat","remove"])
    parser.add_argument("--source", default="custom-ai")
    parser.add_argument("--session", default="main")
    parser.add_argument("--ttl", type=int, default=120)
    args = parser.parse_args()
    if args.self_test:
        from package_selftest import run
        run(AppServer,args.self_test)
        return
    if args.signal:
        from ai_signal import send_event
        send_event(args.source, args.session, args.signal, args.ttl)
        return
    if not args.demo:
        from host_process import running, default_path
        if running(default_path()):
            text="旧的 AI指示灯 2.7.0 正在后台。请从旧版托盘退出，再运行统一版；本次没有打开旧版。"
            if sys.stderr:print(text,file=sys.stderr)
            if os.name=='nt' and not args.autostart:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None,text,'AI 指示灯统一版',0x30)
            return 2
    instance = SingleInstance()
    tray = server = None
    try:
        if not args.demo and not instance.acquire_or_open(open_existing=not args.autostart):
            return
        server = AppServer(port=args.port, demo=args.demo, local_only=local_only)
        server.startup.skip_first_auto_follow = args.keep_follow_paused
        server.ai.start()
        server.startup.start()
        if not args.demo:
            publish_endpoint(server.origin, server.ai_token)
        if os.name == "nt" and not args.demo:
            tray = DesktopTray(server)
            tray.start()
            server.desktop = {"tray": True, "single_instance": True}
        if not args.no_browser and not args.autostart:
            webbrowser.open(server.origin + "/#ai")
        if sys.stdout:
            print(server.origin, flush=True)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if tray:
            tray.stop()
        if server:
            server.startup.shutdown()
            server.ai.shutdown()
            server.lights.shutdown()
            server.controller.shutdown()
            server.server_close()
        instance.close()


if __name__ == "__main__":
    sys.exit(main())
