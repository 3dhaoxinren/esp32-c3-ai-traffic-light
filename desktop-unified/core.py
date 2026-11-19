"""HLK-M30 serial operations. All hardware writes run in one worker."""
from __future__ import annotations

import re
import ipaddress
import select
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass

import serial
from serial.tools import list_ports


class DeviceError(Exception):
    pass


class Cancelled(DeviceError):
    pass


class ReplyTimeout(DeviceError):
    pass


@dataclass(frozen=True)
class Timing:
    reply: float = 2.5
    quiet: float = 0.18
    pmk: float = 10.0
    reboot: float = 20.0


def ascii_field(value, label, minimum, maximum):
    if not isinstance(value, str):
        raise DeviceError(f"{label}格式不正确")
    if not minimum <= len(value) <= maximum or any(not 32 <= ord(c) <= 126 for c in value):
        raise DeviceError(f"{label}需要 {minimum}–{maximum} 位英文、数字或半角符号，不接受中文和换行")
    return value


def validate_wifi(data):
    ssid = ascii_field(data.get("ssid"), "Wi-Fi 名称", 1, 32)
    password = ascii_field(data.get("password"), "Wi-Fi 密码", 8, 63)
    if data.get("auth") not in (7, 9):
        raise DeviceError("请选择 WPA2-PSK 或 WPA/WPA2-PSK")
    return ssid, password, data["auth"]


def response_lines(raw, command):
    text = raw.decode("utf-8", errors="replace")
    return [line.strip() for line in re.split(r"[\r\n]+", text)
            if line.strip() and line.strip().lower() != command.lower()]


def failure_line(lines):
    return next((line for line in lines
                 if re.match(r"^(?:error|err|fail|failed)(?:\b|[:=])", line, re.I)), None)


class Controller:
    def __init__(self, demo=False, factory=None, timing=None):
        self.demo = demo
        self.factory = factory
        self.timing = timing or (Timing(0.3, 0.02, 0.6, 0.8) if demo else Timing())
        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.disconnect_event = threading.Event()
        self.worker = None
        self.port = None
        self.secrets = set()
        self.events = deque(maxlen=400)
        self.seq = 0
        self.silent = False
        self.app_baud = 115200
        self.state = dict(connected=False, at_ready=False, busy=False, port="", baud=115200,
                          stage="未连接", message="选择 CH340 串口，连接后检测模块。", version="",
                          config={}, progress=0, total=0, result="idle", job="", demo=demo, transport="serial",
                          mode="unknown", upgrade_supported=None, device={}, warnings=[],
                          last_seen=0, status_updated=0, app_version="2.3.0")

    def _set(self, **values):
        with self.lock:
            self.state.update(values)

    def log(self, message, kind="info"):
        if self.silent and kind in ("tx", "rx", "info", "success"):
            return
        with self.lock:
            for secret in sorted(self.secrets, key=len, reverse=True):
                message = message.replace(secret, "[密码已隐藏]")
            self.seq += 1
            self.events.append(dict(id=self.seq, time=time.strftime("%H:%M:%S"),
                                    kind=kind, text=message[:1200]))

    def snapshot(self):
        with self.lock:
            return {**self.state, "config": dict(self.state["config"]),
                    "device": dict(self.state["device"]), "warnings": list(self.state["warnings"]),
                    "logs": list(self.events)}

    def ports(self):
        if self.demo:
            return [dict(device="DEMO", description="模拟模块 · 不访问硬件")]
        return [dict(device=p.device, description=p.description, kind='esp32c3' if (getattr(p,'vid',None),getattr(p,'pid',None))==(0x303a,0x1001) else 'legacy') for p in list_ports.comports()]

    def start(self, action, data=None):
        data = data or {}
        with self.lock:
            if self.state["busy"]:
                raise DeviceError("已有任务正在执行，请等待完成或先停止")
            if action not in ("connect", "detect", "read", "status", "audit", "configure", "transparent", "disconnect", "upgrade", "return_at", "nano_baud"):
                raise DeviceError("不支持的操作")
            if action in ("upgrade", "return_at", "nano_baud") and self.state["transport"] != "serial":
                raise DeviceError("此操作需要 USB 串口连接")
            if action in ("upgrade", "nano_baud") and not self.state["at_ready"]:
                raise DeviceError("请先检测到普通 AT 回复")
            if action == "return_at" and self.state["mode"] != "recovery":
                raise DeviceError("当前未确认处于升级模式")
            if self.state["mode"] in ("recovery", "transparent") and action in ("configure", "read", "audit", "nano_baud", "transparent"):
                raise DeviceError("请先回到普通 AT 模式")
            if action == "configure":
                validate_wifi(data)
                if not self.state["at_ready"]:
                    raise DeviceError("请先检测到 HLK-M30 的 AT 回复")
            if action == "transparent" and not self.state["at_ready"]:
                raise DeviceError("请先检测到 AT 模式，再切换透传")
            if action == "connect":
                if self.port:
                    raise DeviceError("请先断开当前串口")
                if data.get("transport", "serial") == "udp":
                    validate_host(data.get("host", ""))
                elif data.get("transport", "serial") == "serial":
                    if data.get("baud") not in (9600, 19200, 38400, 57600, 115200):
                        raise DeviceError("波特率不支持")
                    if data.get("port") not in {p["device"] for p in self.ports()}:
                        raise DeviceError("串口不存在，请刷新串口列表")
                else:
                    raise DeviceError("连接方式不支持")
            elif not self.port:
                raise DeviceError("请先连接模块")
            self.cancel_event.clear()
            self.disconnect_event.clear()
            self._set(busy=True, job=action, result="running", progress=0, total=0)
            self.worker = threading.Thread(target=self._run, args=(action, dict(data)), daemon=True)
            self.worker.start()

    def cancel(self, disconnect=False):
        with self.lock:
            if disconnect:
                self.disconnect_event.set()
            self.cancel_event.set()
            if not self.state["busy"] and disconnect:
                self._close()
            elif self.state["busy"]:
                self._set(message="正在停止；已发送的设置不会自动撤销。")

    def shutdown(self):
        self.cancel(disconnect=True)
        if self.worker:
            self.worker.join(timeout=4)

    def _run(self, action, data):
        try:
            getattr(self, "_" + action)(data)
            if action != "disconnect":
                self._set(result="success")
        except Cancelled:
            self._set(at_ready=False, result="cancelled", stage="已停止",
                      message="操作已停止；设置可能已部分生效。重新检测 AT 并读取配置后再继续。")
            self.log("操作已停止，未继续发送后续命令。", "warn")
        except DeviceError as exc:
            self._set(result="error", stage="需要处理", message=str(exc))
            self.log(str(exc), "error")
        except (serial.SerialException, OSError):
            self._close()
            self._set(result="error", stage="连接不可用",
                      message="连接或读写失败。串口请检查占用和 USB；无线请检查 IP 地址及电脑是否在同一局域网。")
            self.log("连接打开或读写失败，已关闭连接。", "error")
        except Exception:
            # Never publish exception repr: third-party drivers can echo sensitive buffers.
            self._set(at_ready=False, result="error", stage="操作未完成", message="操作异常，已停止。请断开串口后重新连接。")
            self.log("内部操作异常，后续命令已停止。", "error")
        finally:
            self.silent = False
            data.clear()
            with self.lock:
                if self.disconnect_event.is_set():
                    self._close()
                self._set(busy=False, job="")

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise Cancelled()

    def _close(self):
        if self.port:
            try:
                self.port.close()
            except Exception:
                pass
        self.port = None
        self._set(connected=False, at_ready=False, port="", version="", config={},
                  stage="已断开", message="连接已关闭。", result="idle", mode="unknown",
                  device={}, warnings=[], upgrade_supported=None, last_seen=0, status_updated=0, progress=0, total=0)

    def _disconnect(self, data):
        self._close()
        self.log("连接已关闭。")

    def _connect(self, data):
        transport = data.get("transport", "serial")
        self._set(stage="连接模块", message="正在建立连接…", config={}, version="", at_ready=False, transport=transport)
        if self.factory:
            port = self.factory()
        elif self.demo:
            port = DemoSerial()
        elif transport == "udp":
            port = UDPTransport(validate_host(data["host"]))
        else:
            port = serial.Serial(port=None, baudrate=data["baud"], timeout=0.05, write_timeout=1,
                                 bytesize=8, parity="N", stopbits=1, xonxoff=False, rtscts=False, dsrdtr=False)
            port.dtr = False
            port.rts = False
            port.port = data["port"]
            try:
                port.open()
            except Exception:
                port.close()
                raise
        self.port = port
        if transport == "udp":
            self._set(connected=True, port=data["host"] + ":988", baud=115200)
            self.log("连接局域网 UDP AT 接口；使用默认前缀 hlkAT。此操作不改变串口模式。")
        else:
            self._set(connected=True, port=data["port"], baud=data["baud"])
            self.log(f"打开 {data['port']} · {data['baud']} / 8N1；命令自动添加 CR (0D)。")
        self.app_baud = self.state["baud"]
        try:
            self._read({})
        except ReplyTimeout as original_error:
            if transport != "serial":
                raise
            # A released Recovery port can be re-opened without cycling power.
            self.port.baudrate = 115200
            self._set(baud=115200)
            try:
                self._check_recovery()
            except ReplyTimeout:
                self.port.baudrate = self.app_baud
                self._set(baud=self.app_baud)
                raise original_error
            self._set(mode="recovery", at_ready=False, stage="已连接升级模式",
                      message="已确认 Recovery；可返回普通 AT，或释放串口供升级器接管。")

    def _send(self, command, sensitive=False):
        self._check_cancel()
        if not self.port or not self.port.is_open:
            raise DeviceError("串口未打开")
        if "\r" in command or "\n" in command:
            raise DeviceError("命令内容包含换行，已阻止发送")
        self.port.reset_input_buffer()
        payload = (command + "\r").encode("ascii")
        sent = self.port.write(payload)
        if sent != len(payload):
            raise DeviceError("串口未完整发送命令，后续操作已停止")
        self.log("发送 at+Spw=[密码已隐藏] + CR" if sensitive else "发送 " + command + " + CR", "tx")
        if hasattr(self.port, "flush"):
            self.port.flush()

    def _receive(self, command, expect="query", sensitive=False, timeout=None):
        deadline = time.monotonic() + (self.timing.reply if timeout is None else timeout)
        raw = bytearray()
        last_data = time.monotonic()
        lines = []
        while time.monotonic() < deadline:
            self._check_cancel()
            chunk = self.port.read(min(max(self.port.in_waiting, 1), 4096))
            if chunk:
                raw.extend(chunk)
                last_data = time.monotonic()
                if len(raw) > 32768:
                    raise DeviceError("收到过多非 AT 数据，请重新切换 AT 模式")
            lines = response_lines(raw, command)
            error = failure_line(lines)
            # Only consider completed lines or a quiet interval; handles split UART chunks.
            settled = bool(raw) and (raw.endswith((b"\r", b"\n")) or
                                    time.monotonic() - last_data >= self.timing.quiet)
            if settled and error:
                if not sensitive:
                    self.log("设备返回错误", "rx")
                raise DeviceError("模块返回错误，已停止；请查看当前步骤并重新读取配置")
            if expect == "ok" and settled and any(s.lower() == "ok" for s in lines):
                self.log("ok", "rx")
                return "ok"
            if expect == "query" and lines and time.monotonic() - last_data >= self.timing.quiet:
                values = [s for s in lines if s.lower() != "ok"]
                if values:
                    result = "\n".join(values)
                    if not sensitive:
                        self.log(result, "rx")
                    return result
        if self.state["transport"] == "udp":
            raise ReplyTimeout("等待 UDP 回复超时。请检查 IP、同一局域网及远程 AT 是否开启；写入超时时设置可能已生效，不会自动重试。")
        raise ReplyTimeout(f"{command.split('=')[0]} 未返回有效数据。请检查供电、接线和波特率；单条查询失败不代表需要短接 ES/DF。")

    def _query(self, key):
        command = "at+" + key + "=?"
        self._send(command)
        return self._receive(command)

    def _setting(self, key, value, label, step, total):
        self._set(stage=label, message="正在" + label + "…", progress=step - 1, total=total)
        command = f"at+{key}={value}"
        self._send(command, sensitive=(key == "Spw"))
        self._receive(command, expect="ok", sensitive=(key == "Spw"))
        self._set(progress=step)

    def _detect(self, data):
        self._set(stage="检测 AT", message="等待 HLK-M30 版本回复…")
        try:
            version = self._query("ver")
        except DeviceError:
            self._set(at_ready=False, mode="unknown")
            raise
        if not re.search(r"\bHLK-M30\s*\(", version, re.I):
            self._set(at_ready=False, mode="unknown")
            raise DeviceError("收到的数据不是预期的 HLK-M30 版本回复；未允许写入配置")
        network = self.state["transport"] == "udp"
        self._set(at_ready=True, mode="remote" if network else "at", last_seen=time.time(), version=version[:200], stage="远程 AT 已就绪" if network else "AT 已就绪",
                  message="已确认局域网 AT 接口响应；串口模式未改变。可以读取配置。" if network else "已确认 HLK-M30 接收 AT 命令。可以读取配置或设置 Wi-Fi。")
        self.log("AT 检测通过。", "success")

    def _read(self, data):
        self._detect({})
        result = {}
        warnings = []
        self._set(warnings=[])
        keys = [("mac", "MAC 地址"), ("WA", "网络模式"), ("WM", "STA 配置方式"),
                ("Sssidl", "名称长度"), ("Sssid", "Wi-Fi 名称"), ("Sam", "加密方式"),
                ("dhcp", "DHCP"), ("ip", "模块 IP 参数"), ("Ub", "模块波特率"),
                ("UType", "透传协议"), ("UIp", "透传目标"), ("URPort", "远端端口"), ("ULPort", "本地端口")]
        if self.state["transport"] == "serial":
            keys.append(("UP", "固件升级能力"))
        for i, (key, label) in enumerate(keys):
            self._set(stage="读取" + label, progress=i, total=len(keys))
            try:
                # An empty SSID is a valid reply. Its length query disambiguates it
                # from a missing response without accepting echo-only replies.
                result[key] = "" if key == "Sssid" and result.get("Sssidl") == "0" else self._query(key)
            except Cancelled:
                raise
            except DeviceError:
                warnings.append(label + "未读取到")
            self._set(config=dict(result), progress=i + 1)
        supported = result.get("UP") == "UP=1:UART-RECOVERY-V1" if self.state["transport"] == "serial" else None
        try:
            parts = [int(x) for x in result.get("mac", "").split(",")]
            mac = ":".join(f"{x:02X}" for x in parts) if len(parts) == 6 and all(0 <= x <= 255 for x in parts) else result.get("mac", "")
        except ValueError:
            mac = result.get("mac", "")
        ip = result.get("ip", "").replace(",", ".")
        try:
            ip = str(ipaddress.IPv4Address(ip))
        except ValueError:
            ip = ""
        wifi = "reachable" if self.state["transport"] == "udp" else "unconfigured" if result.get("Sssidl") == "0" and result.get("WA") == "0" else "unverified"
        self._set(upgrade_supported=supported, warnings=warnings, status_updated=time.time(),
                  device=dict(mac=mac, ip=ip, ssid=result.get("Sssid"), wifi=wifi,
                              network_mode=result.get("WA"), sta_mode=result.get("WM"),
                              module_baud=result.get("Ub"), protocol=result.get("UType"),
                              remote_ip=result.get("UIp"), remote_port=result.get("URPort"), local_port=result.get("ULPort")),
                  stage="状态已更新" if not warnings else "状态已更新 · 部分未读取",
                  message="小板响应正常，Wi-Fi 名称为空，请填写网络名称和密码。" if wifi == "unconfigured" else
                          "小板的局域网 AT 接口可达。" if wifi == "reachable" else
                          "已读取实时回复。IP 和 Wi-Fi 参数本身不能证明已连接路由器。")

    def _status(self, data):
        if self.state["mode"] == "transparent":
            raise DeviceError("透传期间暂停 AT 查询，避免把查询指令发送给网络端")
        self.silent = bool(data.get("quiet"))
        if self.state["mode"] == "recovery":
            self._check_recovery()
            self._set(stage="升级模式就绪", message="已确认 Recovery 响应；可由匹配的升级器接管串口。")
        else:
            self._read({})

    def _audit(self, data):
        self._detect({})
        values = dict(self.state["config"])
        for i, key in enumerate(("DE", "DP"), 1):
            self._set(stage="检查远程 AT 设置", progress=i - 1, total=2)
            values[key] = self._query(key)
            self._set(config=dict(values), progress=i)
        self._set(stage="远程 AT 已检查", message="DE=1 表示远程 AT 开启；DP 是指令前缀，并非加密认证。此检查没有修改设置。")

    def _transparent(self, data):
        self._detect({})
        self._setting("TS", 1, "进入透传模式", 1, 1)
        self._set(at_ready=False, mode="transparent", stage="已请求透传",
                  message="模块已确认 TS=1。串口数据将用于透传；再次配置前需切回 AT 模式。")

    def _raw_read(self, seconds, length=None):
        raw = bytearray()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and (length is None or len(raw) < length):
            self._check_cancel()
            raw.extend(self.port.read(min(max(self.port.in_waiting, 1), 4096) if length is None else length-len(raw)))
            if len(raw) > 65536:
                raise DeviceError("串口回复过长，已停止")
        return bytes(raw)

    def _check_recovery(self):
        self.port.reset_input_buffer()
        self.port.write(b'AT#Ver\r\n')
        self.port.flush()
        raw = self._raw_read(.4)
        if b'AT#Ver=IoT_V1.30_2' not in raw:
            raise ReplyTimeout("未收到 Recovery 版本回复，请检查供电和 115200 波特率")
        self._set(last_seen=time.time())
        self.log("AT#Ver=IoT_V1.30_2", "rx")

    def _upgrade(self, data):
        self._detect({})
        if self._query("UP") != "UP=1:UART-RECOVERY-V1":
            raise DeviceError("未检测到经过验证的 UP 升级入口，未发送切换指令")
        self.app_baud = self.state["baud"]
        self._set(at_ready=False, mode="unknown", stage="进入升级模式", message="正在等待 Recovery 版本回复…")
        self._send("at+UP=1")
        self.port.baudrate = 115200
        self._set(baud=115200)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                self._check_recovery()
                self._set(mode="recovery", stage="升级模式就绪",
                          message="已进入 Recovery。需要其他升级器接管时，先点“释放串口”。")
                self.log("UP 指令进入升级模式成功；尚未写入固件。", "success")
                return
            except ReplyTimeout:
                continue
        raise DeviceError("已发送 UP=1，但尚未确认 Recovery 回复；可断电重启回到 AT")

    def _return_at(self, data):
        import functools
        import operator
        self._check_recovery()
        self.port.reset_input_buffer()
        self.port.write(b'AT#Up2\r\n')
        self.port.flush()
        self._raw_read(.04)
        self.port.write(b'\xaa')
        self.port.flush()
        if self._raw_read(.15) != b'a':
            raise DeviceError("升级握手未通过，未发送重启；可断电重启回到 AT")
        # Stock command 0x21 restarts only. Fixed-length frame, not a flash write.
        packet = b'\x21\xfe' + bytes(4102)
        packet += bytes((functools.reduce(operator.xor, packet, 0),))
        for offset in range(0, len(packet), 128):
            # Finish this one frame even if Stop is pressed, then honour cancellation.
            chunk = packet[offset:offset+128]
            if self.port.write(chunk) != len(chunk):
                raise DeviceError("重启帧未完整发送，请断电重启回到 AT")
            self.port.flush()
            time.sleep(.002)
        if self._raw_read(2, 4) != bytes.fromhex('91fe006f'):
            raise DeviceError("未收到重启确认，可断电重启回到 AT")
        self._set(mode="unknown", stage="等待返回 AT", message="重启已确认，等待模块启动…")
        self.port.baudrate = self.app_baud
        self._set(baud=self.app_baud)
        if self.cancel_event.wait(self.timing.reboot):
            raise Cancelled()
        # Recovery always uses 115200; the application may use a saved different
        # speed, especially after reconnecting the workbench while in Recovery.
        for baud in dict.fromkeys((self.app_baud, 115200, 9600, 57600, 38400, 19200)):
            self.port.baudrate = baud
            self._set(baud=baud)
            try:
                self._detect({})
                self.app_baud = baud
                break
            except ReplyTimeout:
                continue
        else:
            raise DeviceError("已发送重启，但未确认普通 AT 回复，请检查供电和模块波特率")
        self._read({})
        self.log("已返回普通 AT，未执行固件擦写。", "success")

    def _nano_baud(self, data):
        self._detect({})
        previous = self.state["baud"]
        self._set(stage="准备 Nano 串口", message="将模块通信速度设为 9600，随后验证回复…")
        self._send("at+Ub=9600")
        early = self._raw_read(.25)
        if failure_line(response_lines(early, 'at+Ub=9600')):
            raise DeviceError("模块拒绝修改波特率")
        # Some revisions apply Ub immediately, others after reboot. Verify both
        # before deciding to reboot; never resend the setting blindly.
        for baud in dict.fromkeys((9600, previous)):
            self.port.baudrate = baud
            self._set(baud=baud)
            try:
                self._send('at+ver=?')
                version = self._receive('at+ver=?', timeout=1)
                if 'HLK-M30' not in version:
                    continue
                if baud != 9600:
                    if self._query('Ub') != '9600':
                        raise DeviceError("模块未保存 9600，请重新读取状态")
                    self._wait_command('at+Rb=1', '重启以应用 9600', self.timing.reboot)
                    self.port.baudrate = 9600
                    self._set(baud=9600)
                self.app_baud = 9600
                self._read({})
                self._set(stage="Nano 串口已准备", message="已验证模块在 9600 响应。断开 CH340 的 TX/RX 后，按接线图连接 Nano。")
                return
            except ReplyTimeout:
                continue
        self._set(at_ready=False, mode="unknown")
        raise DeviceError("波特率切换后未确认回复。请先尝试以 9600 重新连接，再尝试原波特率。")

    def _wait_command(self, command, label, seconds):
        self._set(stage=label)
        self._send(command)
        deadline = time.monotonic() + seconds
        raw = bytearray()
        while time.monotonic() < deadline:
            self._check_cancel()
            remaining = max(1, int(deadline - time.monotonic() + 0.99))
            self._set(message=f"{label}，约剩 {remaining} 秒；此步骤可能不返回 ok。")
            # Reboot need not reply. Avoid treating an expected UDP socket reset as a write failure.
            chunk = b"" if command == "at+Rb=1" else self.port.read(min(max(self.port.in_waiting, 1), 4096))
            raw.extend(chunk)
            if len(raw) > 32768:
                raw = raw[-8192:]
            lines = response_lines(raw, command)
            if failure_line(lines):
                raise DeviceError(label + "时模块报告错误，后续操作已停止")
            self.cancel_event.wait(min(0.1, max(0, deadline - time.monotonic())))
        self.log(label + "等待结束；未以此判定联网成功。")

    def _configure(self, data):
        ssid, password, auth = validate_wifi(data)
        with self.lock:
            self.secrets.add(password)
        self._detect({})
        modes = {}
        for key in ("WA", "WM"):
            val = self._query(key)
            if not val.isdigit():
                raise DeviceError("模式查询返回格式不明确，已停止写入")
            modes[key] = int(val)
        settings = []
        if modes["WA"] != 0:
            settings.append(("WA", 0, "设置 STA 网络模式"))
        if modes["WM"] != 2:
            settings.append(("WM", 2, "设置手动配置方式"))
        settings.extend([
            ("Sssid", ssid, "设置 Wi-Fi 名称"), ("Sssidl", len(ssid.encode("ascii")), "设置名称长度"),
            ("Sam", auth, "设置加密方式"), ("Spw", password, "设置 Wi-Fi 密码"),
            ("Spwl", len(password.encode("ascii")), "设置密码长度"), ("dhcp", 1, "启用 DHCP")])
        total = len(settings) + 2
        for i, (key, value, label) in enumerate(settings, 1):
            self._setting(key, value, label, i, total)
        self._wait_command("at+WC=1", "计算 Wi-Fi 密钥", self.timing.pmk)
        self._set(progress=total - 1, at_ready=False, config={})
        self._wait_command("at+Rb=1", "等待模块重启", self.timing.reboot)
        if self.state["transport"] == "serial" and self.state["upgrade_supported"]:
            self._read({})
        self._set(progress=total, total=total, stage="设置已发送 · 联网待确认",
                  message="设置已发送并重启。请在路由器确认联网；无线改网后 IP 可能改变。")
        self.log("配置流程结束；尚未验证路由器连接或 IP 地址。", "success")


def validate_host(host):
    try:
        address = ipaddress.IPv4Address(host)
    except (ValueError, TypeError):
        raise DeviceError("请输入小板的局域网 IPv4 地址") from None
    if not any(address in ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise DeviceError("此工具只连接私有局域网地址")
    return str(address)


class UDPTransport:
    """One peer, no broadcast/discovery, no retry of configuration writes."""
    def __init__(self, host, port=988):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.connect((host, port))
        self.socket.setblocking(False)
        self.is_open = True
        self.buffer = bytearray()

    @property
    def in_waiting(self):
        return len(self.buffer)

    def reset_input_buffer(self):
        self.buffer.clear()
        while select.select([self.socket], [], [], 0)[0]:
            try:
                self.socket.recv(32768)
            except (BlockingIOError, ConnectionResetError):
                break

    def write(self, payload):
        framed = b"hlkAT" + payload
        if self.socket.send(framed) != len(framed):
            raise DeviceError("UDP 命令未完整发出")
        return len(payload)

    def read(self, size=1):
        if not self.buffer and select.select([self.socket], [], [], 0.05)[0]:
            try:
                self.buffer.extend(self.socket.recv(32768))
            except BlockingIOError:
                return b""
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def close(self):
        self.is_open = False
        self.socket.close()


class DemoSerial:
    """Explicit offline simulator; never touches a physical COM port."""
    def __init__(self):
        self.is_open = True
        self.buffer = bytearray()
        self.baudrate = 115200
        self.mode = "at"
        self.frame = bytearray()
        self.values = dict(WA="0", WM="2", Sssid="AILamp_Demo", Sssidl="11", Sam="9", dhcp="1", DE="1", DP="hlkAT",
                           mac="60,51,0,12,34,128", ip="192,168,1,100", Ub="115200", UType="1", UIp="192.168.0.1", URPort="0", ULPort="8080", UP="UP=1:UART-RECOVERY-V1")

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return len(self.buffer)

    def reset_input_buffer(self):
        self.buffer.clear()

    def write(self, payload):
        if self.mode == "binary":
            self.frame.extend(payload)
            if len(self.frame) == 4105:
                import functools
                import operator
                if self.frame[:2] == b'\x21\xfe' and functools.reduce(operator.xor, self.frame, 0) == 0 and not any(self.frame[2:-1]):
                    self.buffer.extend(bytes.fromhex('91fe006f'))
                    self.mode = "at"
                self.frame.clear()
            return len(payload)
        if self.mode in ("recovery", "handshake"):
            if payload == b'AT#Ver\r\n':
                self.buffer.extend(b'AT#Ver=IoT_V1.30_2\r\n')
            elif payload == b'AT#Up2\r\n':
                self.mode = "handshake"
            elif payload == b'\xaa' and self.mode == "handshake":
                self.buffer.extend(b'a')
                self.mode = "binary"
            return len(payload)
        command = payload.decode("ascii").strip()
        key, value = command[3:].split("=", 1)
        if value == "?":
            answer = "HLK-M30(V3.18(Dec 9 2015))" if key == "ver" else self.values.get(key, "error")
        else:
            self.values[key] = value
            answer = "" if key in ("WC", "Rb") else "ok"
            if key == "UP" and value == "1":
                self.values[key] = "UP=1:UART-RECOVERY-V1"
                self.mode = "recovery"
        self.buffer.extend((command + "\r\n" + answer + "\r\n").encode("ascii"))
        return len(payload)

    def read(self, size=1):
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        if not data:
            time.sleep(0.01)
        return data

    def close(self):
        self.is_open = False
