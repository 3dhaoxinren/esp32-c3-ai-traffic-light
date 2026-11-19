"""Windows notification-area icon and one workbench per signed-in user."""
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import sys
import threading
import time
import webbrowser
from ai_status import visible_errors

STATE_NAMES = {"idle": "空闲 / 完成", "running": "执行中", "waiting": "等待确认", "error": "异常 / 报错", "offline": "状态失效"}
COLOR_NAMES = {"RED": "红灯", "YELLOW": "黄灯", "GREEN": "绿灯", "OFF": "熄灯"}
EFFECT_NAMES = {"SOLID": "常亮", "BREATHE": "呼吸", "SLOW": "慢闪", "FAST": "快闪"}
def timer_api():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    user32.KillTimer.restype = wintypes.BOOL
    return user32


OPEN_MESSAGE = 0x8000 + 20
CALLBACK_MESSAGE = 0x8000 + 21


def tray_status(light, ai):
    connection = "Wi-Fi TCP" if light.get("transport") == "tcp" else "USB 串口"
    if not light.get("connected"):
        error = light.get("result") == "error"
        return ("red" if error else "gray", f"AI指示灯 · {connection} · " + ("连接异常" if error else "未连接灯板") + "\n" + light.get("endpoint", ""))
    endpoint = light.get("endpoint", "")
    if ai.get("enabled") or ai.get("preview"):
        target = ai.get("target", "offline")
        pattern = ai.get("patterns", {}).get(target, {"color": "OFF", "effect": "SOLID"})
        color = pattern["color"]
        effect = COLOR_NAMES[color] + (EFFECT_NAMES[pattern["effect"]] if color != "OFF" else "")
        mode = "试亮中" if ai.get("preview") else "自动跟随"
        detail = mode + " · " + STATE_NAMES.get(target, target) + " · " + effect
        icon = color.lower() if color != "OFF" else "gray"
    else:
        detail, icon = "自动跟随已暂停", "blue"
    return icon, f"AI指示灯 · {connection} 已连接\n{detail}\n{endpoint}"[:127]


def window_class():
    # Windows session plus current user, matching the local mutex scope.
    import win32api
    name = hashlib.sha256(win32api.GetUserName().encode("utf-8")).hexdigest()[:16]
    return "AILamp.Unified." + name


class SingleInstance:
    def __init__(self):
        self.handle = None

    def acquire_or_open(self, open_existing=True):
        if os.name != "nt":
            return True
        import win32api
        import win32event
        import win32gui
        self.handle = win32event.CreateMutex(None, False, "Local\\" + window_class())
        if win32api.GetLastError() != 183:
            return True
        if not open_existing:
            self.close()
            return False
        for _ in range(60):
            hwnd = win32gui.FindWindow(window_class(), None)
            if hwnd:
                win32gui.PostMessage(hwnd, OPEN_MESSAGE, 0, 0)
                self.close()
                return False
            time.sleep(.1)
        self.close()
        raise RuntimeError("AI指示灯正在启动或退出，请稍后再打开。")

    def close(self):
        if self.handle:
            self.handle.Close()
            self.handle = None


class DesktopTray:
    def __init__(self, server):
        self.server = server
        self.hwnd = None
        self.ready = threading.Event()
        self.worker = None
        self.error = None
        self.last_status = None
        self.icons = {}

    def start(self):
        self.worker = threading.Thread(target=self._run, name="workbench-tray", daemon=True)
        self.worker.start()
        if not self.ready.wait(5) or self.error:
            raise RuntimeError("无法创建系统托盘图标") from self.error

    def open(self, page="ai"):
        webbrowser.open(self.server.origin + "/#" + page)

    def _update(self, add=False):
        import win32gui
        icon, tooltip = tray_status(self.server.lights.snapshot(), self.server.ai.snapshot())
        if add or (icon, tooltip) != self.last_status:
            flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
            data = (self.hwnd, 1, flags, CALLBACK_MESSAGE, self.icons[icon], tooltip)
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD if add else win32gui.NIM_MODIFY, data)
            self.last_status = (icon, tooltip)

    def _menu(self):
        import win32con as c
        import win32gui as g
        light, ai = self.server.lights.snapshot(), self.server.ai.snapshot()
        errors = visible_errors(ai)
        menu = g.CreatePopupMenu()
        try:
            g.AppendMenu(menu, c.MF_STRING | c.MF_GRAYED, 0, tray_status(light, ai)[1].replace("\n", " · "))
            g.AppendMenu(menu, c.MF_SEPARATOR, 0, "")
            g.AppendMenu(menu, c.MF_STRING, 1, "打开AI指示灯")
            g.AppendMenu(menu, c.MF_STRING, 2, "灯控调试")
            g.AppendMenu(menu, c.MF_STRING, 6, "C3 音频剪辑与播报")
            flags = c.MF_STRING | (0 if light["connected"] and not light["busy"] else c.MF_GRAYED)
            g.AppendMenu(menu, flags, 3, "暂停并熄灯" if ai["enabled"] else "开启自动跟随")
            flags = c.MF_STRING | (0 if errors else c.MF_GRAYED)
            g.AppendMenu(menu, flags, 5, "已查看，恢复正常")
            g.AppendMenu(menu, c.MF_SEPARATOR, 0, "")
            g.AppendMenu(menu, c.MF_STRING, 4, "退出AI指示灯")
            x, y = g.GetCursorPos()
            g.SetForegroundWindow(self.hwnd)
            selected = g.TrackPopupMenu(menu, c.TPM_RETURNCMD | c.TPM_RIGHTBUTTON, x, y, 0, self.hwnd, None)
            g.PostMessage(self.hwnd, c.WM_NULL, 0, 0)
            if selected in (1, 2, 6):
                self.open({1:"ai",2:"lights",6:"audio"}[selected])
            elif selected == 3:
                with self.server.connection_lock:
                    self.server.ai.set_enabled(not self.server.ai.enabled)
                self._update()
            elif selected == 4:
                if self.server.controller.snapshot()["busy"] or self.server.lights.snapshot()["busy"] or (hasattr(self.server,"audio") and self.server.audio.snapshot()["busy"]):
                    self.open()
                    self.server.controller.log("设备操作进行中，请先停止操作再退出", "warn")
                else:
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif selected == 5:
                # Use identities captured when this menu opened; a new failure
                # arriving while it is open must remain unacknowledged.
                self.server.ai.acknowledge_errors(errors)
                self._update()
        finally:
            g.DestroyMenu(menu)

    def _message(self, hwnd, message, wparam, lparam):
        import win32con as c
        import win32gui as g
        try:
            if message == OPEN_MESSAGE:
                self.open()
            elif message == CALLBACK_MESSAGE and lparam == c.WM_LBUTTONDBLCLK:
                self.open()
            elif message == CALLBACK_MESSAGE and lparam in (c.WM_RBUTTONUP, c.WM_CONTEXTMENU):
                self._menu()
            elif message == c.WM_TIMER:
                self._update()
            elif message == self.taskbar_created:
                self._update(add=True)
            elif message == c.WM_CLOSE:
                g.DestroyWindow(hwnd)
            elif message == c.WM_DESTROY:
                timer_api().KillTimer(hwnd, 1)
                g.Shell_NotifyIcon(g.NIM_DELETE, (hwnd, 1))
                g.PostQuitMessage(0)
            else:
                return g.DefWindowProc(hwnd, message, wparam, lparam)
        except Exception as error:
            self.server.controller.log("托盘操作失败：" + str(error), "warn")
        return 0

    def _run(self):
        import win32api
        import win32con as c
        import win32gui as g
        registered = False
        try:
            self.taskbar_created = g.RegisterWindowMessage("TaskbarCreated")
            instance = win32api.GetModuleHandle(None)
            cls = g.WNDCLASS()
            cls.hInstance, cls.lpszClassName, cls.lpfnWndProc = instance, window_class(), self._message
            g.RegisterClass(cls)
            registered = True
            assets = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "icons"
            for color in ("red", "yellow", "green", "gray", "blue"):
                self.icons[color] = g.LoadImage(0, str(assets / (color + ".ico")), c.IMAGE_ICON, 32, 32, c.LR_LOADFROMFILE)
            self.hwnd = g.CreateWindow(cls.lpszClassName, "AI指示灯后台", 0, 0, 0, 0, 0, 0, 0, instance, None)
            self._update(add=True)
            if not timer_api().SetTimer(self.hwnd, 1, 1000, None):
                raise ctypes.WinError(ctypes.get_last_error())
            self.ready.set()
            g.PumpMessages()
        except Exception as error:
            self.error = error
            self.ready.set()
        finally:
            if self.hwnd and g.IsWindow(self.hwnd):
                g.DestroyWindow(self.hwnd)
            self.hwnd = None
            for icon in self.icons.values():
                g.DestroyIcon(icon)
            if registered:
                g.UnregisterClass(window_class(), instance)

    def stop(self):
        if self.hwnd:
            import win32con
            import win32gui
            win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
        if self.worker:
            self.worker.join(4)
