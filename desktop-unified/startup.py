"""Opt-in Windows logon startup and reconnect of a verified lamp endpoint."""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from serial.tools import list_ports
from core import DeviceError, validate_host
from serial_notifications import WindowsSerialNotifications


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "HLKM30AIIndicator"
CH340 = (0x1a86,0x7523)
CH340_LOCATION = re.compile(r"\d+(?:-\d+)+(?:\.\d+)*(?::x\.0)?")


def validate_target(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DeviceError("设备连接配置不正确")
    if value.get("transport") == "tcp":
        host = validate_host(value.get("host"))
        port = value.get("tcp_port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise DeviceError("TCP 端口应为 1–65535")
        return dict(transport="tcp", host=host, tcp_port=port)
    if value.get("transport") != "serial" or not isinstance(value.get("port"), str) or not re.fullmatch(r"COM[1-9][0-9]{0,4}", value["port"]):
        raise DeviceError("设备串口配置不正确")
    result = dict(transport="serial", port=value["port"])
    for key in ("vid", "pid"):
        item = value.get(key)
        if item is not None and (type(item) is not int or not 0 <= item <= 65535):
            raise DeviceError("USB 标识不正确")
        result[key] = item
    for key in ("serial_number", "location"):
        item = value.get(key) or ""
        if not isinstance(item, str) or len(item) > 256 or any(ord(c) < 32 for c in item):
            raise DeviceError("USB 标识不正确")
        result[key] = item
    return result


def capture_target(data, ports):
    if data.get("transport") == "tcp":
        return validate_target(data)
    found = next((p for p in ports if p.device == data.get("port")), None)
    if found is None:
        raise DeviceError("Nano 已拔出，请重新扫描串口")
    return validate_target(dict(transport="serial", port=found.device, vid=found.vid, pid=found.pid,
                                serial_number=found.serial_number, location=found.location))


def same_usb_location(expected,actual):
    if not expected or not actual:return False
    if expected==actual:return True
    # Older Windows pyserial used "1-5"; newer builds report "1-5:x.0".
    # A stored explicit interface remains exact; never broaden to other ports.
    return bool(re.fullmatch(r"\d+(?:-\d+)+(?:\.\d+)*",expected) and
                re.fullmatch(re.escape(expected)+r":x\.\d+",actual))


def resolve_target(target, ports, roam_usb=False):
    """Resolve the last confirmed binding without opening or probing other ports."""
    if target["transport"] == "tcp":
        return dict(target)
    candidates = [p for p in ports if p.vid == target["vid"] and p.pid == target["pid"]]
    same_chip = candidates
    if target["serial_number"]:
        candidates = [p for p in candidates if p.serial_number == target["serial_number"]]
        if len(candidates) == 1:
            return dict(transport="serial", port=candidates[0].device)
    if not candidates:
        raise DeviceError("等待已记住的 Nano 接入 USB")
    if target["location"]:
        # CH340 boards commonly omit unique serial numbers. Windows' USB
        # topology differentiates two such adapters even when COM names change.
        ch340_plain = re.fullmatch(r"(\d+(?:-\d+)+(?:\.\d+)*):x\.0", target["location"]) if (
            target["vid"],target["pid"]) == CH340 else None
        candidates = [p for p in candidates if same_usb_location(target["location"],p.location) or (
            ch340_plain and p.device == target["port"] and p.location == ch340_plain[1])]
        if not candidates:
            if (roam_usb and not target["serial_number"] and (target["vid"],target["pid"])==CH340
                    and CH340_LOCATION.fullmatch(target["location"])):
                if len(same_chip)>1:
                    raise DeviceError("换 USB 口后发现多个 CH340，请手动选择 Nano；不会自动尝试。")
                if len(same_chip)==1 and not same_chip[0].serial_number and CH340_LOCATION.fullmatch(same_chip[0].location or ""):
                    return dict(transport="serial",port=same_chip[0].device)
            raise DeviceError("等待灯板原 USB 接口 " + target["location"] + "；换接口后请手动连接一次")
    else:
        # Older profiles without a topology binding may use only the explicitly
        # confirmed COM name. Never pick the sole remaining, unrelated adapter.
        candidates = [p for p in candidates if p.device == target["port"]]
    if len(candidates) != 1:
        raise DeviceError("灯板标识仍有歧义或原串口未就绪，请手动选择一次灯控串口")
    return dict(transport="serial", port=candidates[0].device)


class ConnectionPreferences:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "HLK-M30/connection-settings.json"
        self.value = dict(auto_connect=False, auto_follow=True, roam_usb=False, target=None)
        self.warning = ""
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(value,dict) or any(type(value.get(k)) is not bool for k in ("auto_connect","auto_follow"))
                    or type(value.get("roam_usb",False)) is not bool):
                raise ValueError("Invalid preferences")
            self.value = dict(auto_connect=value["auto_connect"], auto_follow=value["auto_follow"],
                              roam_usb=value.get("roam_usb",False), target=validate_target(value.get("target")))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, DeviceError):
            self.warning = "自动连接配置无法读取，已安全停用；保存设置可修复。"

    def snapshot(self):
        return copy.deepcopy(self.value)

    def save(self, **changes):
        value = {**self.value, **changes}
        for key in ("auto_connect", "auto_follow", "roam_usb"):
            if type(value[key]) is not bool:
                raise DeviceError("自动连接开关必须为布尔值")
        value["target"] = validate_target(value["target"])
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            raise DeviceError("无法保存自动连接配置，请检查目录写入权限") from exc
        self.value, self.warning = value, ""


class WindowsStartup:
    """Only our current user's single Run value; no admin, shell or system policy."""
    def __init__(self, allowed=True, local_only=False):
        self.supported = os.name == "nt" and allowed
        runtime=Path(sys.executable).resolve()
        if os.name=='nt' and not getattr(sys,'frozen',False) and runtime.with_name('pythonw.exe').is_file():
            runtime=runtime.with_name('pythonw.exe')
        args = [str(runtime)]
        if not getattr(sys, "frozen", False):
            args.append(str(Path(__file__).resolve().with_name("nano_local.py" if local_only else "server.py")))
        args.extend(["--autostart", "--no-browser"])
        self.command = subprocess.list2cmdline(args)

    def snapshot(self):
        if not self.supported:
            return dict(supported=False, enabled=False, warning="演示模式不修改开机启动" if os.name == "nt" else "此功能仅支持 Windows")
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, RUN_NAME)
        except FileNotFoundError:
            value = ""
        except OSError as exc:
            return dict(supported=True, enabled=False, warning="无法读取当前用户的启动项：" + str(exc))
        return dict(supported=True, enabled=value == self.command,
                    warning="启动项指向旧目录，请重新保存开机自启设置" if value and value != self.command else "")

    def set_enabled(self, enabled):
        if type(enabled) is not bool or not self.supported:
            raise DeviceError("当前环境不能修改开机启动")
        if enabled and len(self.command) > 260:
            raise DeviceError("程序路径过长，请移到较短的固定目录后再启用开机启动")
        import winreg
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, self.command)
                else:
                    try:
                        winreg.DeleteValue(key, RUN_NAME)
                    except FileNotFoundError:
                        pass
        except OSError as exc:
            raise DeviceError("保存开机启动失败：" + str(exc)) from exc


class AutoConnector:
    def __init__(self, server, preferences=None, registration=None, ports=None, clock=time.monotonic):
        self.server = server
        self.preferences = preferences or ConnectionPreferences()
        self.registration = registration or WindowsStartup(allowed=not server.controller.demo, local_only=getattr(server, 'local_only', False))
        self.ports = ports or list_ports.comports
        self.clock = clock
        self.stop_event = threading.Event()
        self.worker = None
        self.suspended = False
        self.pending = None
        self.attempts = 0
        self.next_try = 0
        self.serial_requested = True  # One initial check, not a periodic scan.
        self.serial_was_connected = False
        self.serial_due = None
        self.serial_removed = False
        self.serial_events = set()
        self.serial_events_lock = threading.Lock()
        self.serial_notifications = None
        self.serial_notice_warning = ""
        self.serial_checks = 0
        self.skip_first_auto_follow = False
        self.message = "尚未启用自动连接"

    def snapshot(self):
        with self.server.connection_lock:
            return {**self.preferences.snapshot(), "autostart": self.registration.snapshot(),
                    "suspended": self.suspended, "attempts": self.attempts,
                    "message": self.message, "warning": self.preferences.warning or self.serial_notice_warning,
                    "serial_notifications": self.serial_notifications is not None,
                    "serial_checks": self.serial_checks,
                    "retry_in": max(0, int(self.next_try - self.clock()))}

    def configure(self, data):
        if self.server.controller.demo:
            raise DeviceError("演示模式不修改真实启动和连接设置")
        required={"autostart","auto_connect","auto_follow"}
        if not required<=set(data)<=required|{"roam_usb"} or any(type(v) is not bool for v in data.values()):
            raise DeviceError("启动设置开关必须为布尔值")
        previous = self.preferences.snapshot()
        self.preferences.save(auto_connect=data["auto_connect"], auto_follow=data["auto_follow"],
                              roam_usb=data.get("roam_usb",previous["roam_usb"]))
        try:
            self.registration.set_enabled(data["autostart"])
        except DeviceError:
            self.preferences.save(**previous)
            raise
        self.suspended, self.next_try, self.attempts = False, 0, 0
        self.serial_requested = True
        self.message = "设置已保存；等待连接上次确认的灯板" if data["auto_connect"] else "已关闭自动连接"

    def manual_started(self, action, data):
        if action == "light_connect":
            try:
                target = capture_target(data, self.ports() if data.get("transport") == "serial" else ())
            except DeviceError as exc:
                self.suspend(str(exc))
                return
            self.pending = (target, False)
            self.suspended = False
        elif action in ("light_disconnect", "connect"):
            self.suspend("已暂停自动重连；重新启动程序或点击恢复重连后继续")

    def manual_serial_switch(self, port):
        # Called with connection_lock held. Capture the target before opening it;
        # step() saves it only after a valid lamp reply, like a normal manual connect.
        data = {"transport": "serial", "port": port}
        target = capture_target(data, self.ports())
        self.server.lights.start("light_switch_serial", data)
        self.pending = (target, False)
        self.suspended = False
        self.message = "正在切换到 " + port

    def suspend(self, message):
        self.suspended, self.pending, self.message = True, None, message
        self.serial_was_connected = False

    def resume(self):
        if self.server.controller.demo:
            raise DeviceError("演示模式不连接真实设备")
        if not self.preferences.value["auto_connect"] or not self.preferences.value["target"]:
            raise DeviceError("请先手动连接一次灯板，并保存开启自动连接")
        self.suspended, self.next_try, self.attempts = False, 0, 0
        self.serial_requested = True
        self.message = "正在恢复自动连接"

    def notify_serial_change(self, identity, removed):
        with self.serial_events_lock:
            self.serial_events.add((identity, removed))

    def _serial_ports(self, target, light):
        """Consume a startup/manual request, a connection loss, or a PnP event."""
        identity = target["vid"], target["pid"]
        with self.serial_events_lock:
            events = self.serial_events or ()
            if events:
                self.serial_events = set()
        for observed, removed in events:
            if identity == observed or identity == (None, None):
                self.serial_due = self.clock() + .35  # Coalesce removals/arrivals and let PnP settle.
                self.serial_removed |= removed
        active = (light["connected"] and light.get("transport") == "serial"
                  and light.get("endpoint", "").startswith(target["port"] + " "))
        if active:
            self.serial_requested = False
            self.serial_was_connected = True
        elif light["connected"]:
            return None  # Never replace a different, live connection.
        elif self.serial_was_connected:
            self.serial_was_connected = False
            self.serial_requested = True  # One check after a real link/heartbeat failure.
        due = self.serial_due
        if (due is not None and self.clock() < due) or (due is None and not self.serial_requested):
            return None
        # A live recording can hold a shared USB connection while it finishes.
        if active and hasattr(self.server, "audio") and self.server.audio.snapshot()["busy"]:
            return None
        self.serial_due, self.serial_requested = None, False
        removed, self.serial_removed = self.serial_removed, False
        self.serial_checks += 1
        ports = self.ports()
        if not active:
            return ports
        try:
            # Roaming is allowed after a loss, not when identifying an open handle.
            present = resolve_target(target, ports)["port"] == target["port"]
        except DeviceError:
            present = False
        if present and (not removed or self.server.lights.serial_handle_alive()):
            return None
        self.server.lights._close()
        self.serial_was_connected = False
        self.server.ai.reset_link_state()
        self.message = "灯板 USB 已断开，等待重新连接"
        return ports

    def step(self):
        with self.server.connection_lock:
            if self.server.controller.demo or self.stop_event.is_set():
                return
            light = self.server.lights.snapshot()
            if light["busy"]:
                return
            if self.pending:
                target, automatic = self.pending
                self.pending = None
                if light["connected"]:
                    try:
                        self.preferences.save(target=target)
                    except DeviceError as exc:
                        self.message = str(exc)
                        self.suspended = True
                        return
                    self.attempts = 0
                    self.message = "已连接并记住灯板；下次启动可自动连接"
                    if automatic:
                        skip_follow, self.skip_first_auto_follow = self.skip_first_auto_follow, False
                        if not skip_follow and self.preferences.value["auto_connect"] and self.preferences.value["auto_follow"]:
                            self.server.ai.set_enabled(True)
                            self.message = "已自动连接灯板并开启 AI 跟随"
                else:
                    self.message = light.get("message", "连接失败，等待重试")
                    if not automatic:
                        self.suspended = True  # Never reconnect an old target after a failed manual switch.
            settings = self.preferences.value
            if self.suspended or not settings["auto_connect"]:
                return
            target = settings["target"]
            if not target:
                self.message = "请先在灯控调试中手动成功连接一次，自动记住设备"
                return
            if not light["connected"]:
                module = self.server.controller.snapshot()
                if module["busy"] or module["connected"]:
                    return
            try:
                if target["transport"] == "serial":
                    ports = self._serial_ports(target, light)
                    if ports is None:
                        return
                    self.attempts += 1
                    data = resolve_target(target, ports, settings["roam_usb"])
                    target = capture_target(data, ports)
                else:
                    if light["connected"] or self.clock() < self.next_try:
                        return
                    if getattr(self.server, 'local_only', False):
                        self.suspend("Nano 本地版仅使用 USB；请先手动连接一次 Nano 更新设备记忆")
                        return
                    self.attempts += 1
                    delay = (2, 5, 10, 30)[min(self.attempts - 1, 3)]
                    self.next_try = self.clock() + delay
                    data = dict(target)
                self.server.lights.start("light_connect", data)
                self.pending = (target, True)
                self.message = "正在自动连接灯板（第 " + str(self.attempts) + " 次）"
            except (DeviceError, OSError) as exc:
                self.message = str(exc)

    def start(self):
        if self.server.controller.demo:
            return
        if os.name == "nt":
            try:
                self.serial_notifications = WindowsSerialNotifications(self.notify_serial_change)
            except (OSError, AttributeError) as exc:
                self.serial_notice_warning = "未能注册 Windows 串口通知；插拔后请点恢复自动重连或从托盘选择。"
                self.server.controller.log(self.serial_notice_warning + " " + str(exc), "warn")
        self.worker = threading.Thread(target=self._loop, name="lamp-auto-connect", daemon=True)
        self.worker.start()

    def _loop(self):
        while not self.stop_event.wait(.5):
            try:
                self.step()
            except Exception as exc:
                # Do not silently kill reconnect on an unexpected OS failure.
                with self.server.connection_lock:
                    self.suspend("自动连接已暂停：" + str(exc))
                self.server.controller.log(self.message, "error")

    def shutdown(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(4)
        if self.serial_notifications:
            try:
                self.serial_notifications.close()
            except OSError as exc:
                self.server.controller.log("注销串口通知失败：" + str(exc), "warn")
            else:
                self.serial_notifications = None
