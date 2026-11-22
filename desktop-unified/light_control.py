"""Persistent, manually operated Nano lamp connection (USB serial or TCP)."""
import os
import re
import socket
import threading
import time

import serial
from serial.tools import list_ports

from core import DeviceError, validate_host
from ch340_serial import CachedCH340Serial
from lamp_preferences import validate_pattern, DEFAULT_PATTERNS
from screen_preferences import validate_screen, SCREEN_STATES, render_notice


STATE_REPLY = re.compile(r"OK LIGHT mode=(MANUAL|AUTO) color=(OFF|RED|YELLOW|GREEN) R=([01]) Y=([01]) G=([01])")
AI_REPLY = re.compile(r"OK AI state=(IDLE|RUNNING|WAITING|ERROR|OFFLINE|DONE) R=([01]) Y=([01]) G=([01]) level=(\d{1,3}) lease=(\d+)(?: color=(RED|YELLOW|GREEN|OFF) effect=(SOLID|BREATHE|SLOW|FAST))?")
VOICE_REPLY = re.compile(r"OK VOICE hold=1 ai=(IDLE|RUNNING|WAITING|ERROR|OFFLINE) color=(OFF|RED|YELLOW|GREEN) R=([01]) Y=([01]) G=([01])")
COMMANDS = {"RED", "YELLOW", "GREEN", "OFF", "AUTO", "STATUS", "HELP"}
BITS = {"OFF": (0, 0, 0), "RED": (1, 0, 0), "YELLOW": (0, 1, 0), "GREEN": (0, 0, 1)}
SCREEN_REPLY = re.compile(r'OK SCREEN rotation=(0|90|180|270) mode=(AUTO|RAIN|DONE|STATUS|OFF) state=(IDLE|RUNNING|WAITING|ERROR|OFFLINE|DONE) online=([01])')


class LightController:
    def __init__(self, log, demo=False, local_only=False):
        self.log = log
        self.demo = demo
        self.local_only = local_only
        self.lock = threading.RLock()
        self.worker = None
        self.link = None
        self.cancelled = threading.Event()
        self.completion_key = None
        self.notice_until = 0
        self.last_ai_command=None
        self.state = dict(connected=False, busy=False, transport="serial", endpoint="",
                          message="连接 Nano USB 或 Wi-Fi TCP 后点击按钮。", result="idle",
                          reply="", mode="", color="", pins=None, updated=0, ai_state="", level=0, ai_color="", ai_effect="", voice_hold=False,
                          screen_supported=None, screen_error='')
        if local_only:
            self.state['message'] = '请选择 Nano USB 串口后连接灯板。'

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def _set(self, **values):
        with self.lock:
            self.state.update(values)

    def start(self, action, data):
        with self.lock:
            if self.state["busy"]:
                raise DeviceError("正在等待灯控回执，请稍候")
            if self.demo:
                raise DeviceError("演示模式不连接真实灯板，请打开正常工作台")
            if self.local_only and (
                    (action == 'light_connect' and data.get('transport') != 'serial') or
                    action in ('light_screen', 'light_notice') or
                    (action == 'light_send' and data.get('command') == 'NET ON')):
                raise DeviceError("Nano 本地版仅支持 USB 灯控")
            if action in ("light_connect", "light_switch_serial"):
                if action == "light_connect" and self.link:
                    raise DeviceError("请先断开当前灯控连接")
                transport = data.get("transport")
                if action == "light_switch_serial" and transport != "serial":
                    raise DeviceError("切换串口只使用 USB")
                if transport == "serial":
                    if data.get("port") not in {p.device for p in list_ports.comports()}:
                        raise DeviceError("请选择 Nano 对应的 USB 串口")
                elif transport == "tcp":
                    validate_host(data.get("host"))
                    if type(data.get("tcp_port")) is not int or not 1 <= data["tcp_port"] <= 65535:
                        raise DeviceError("TCP 端口应为 1–65535")
                else:
                    raise DeviceError("请选择 USB 或 Wi-Fi TCP")
            elif action == 'light_sound':
                if not self.state['connected'] or not self.state.get('sound_v2'):
                    raise DeviceError('请连接已报告语音能力的 C3 灯板（USB 或局域网）')
                if data.get('op')=='play' and (not self.state.get('numbered_play') or type(data.get('track')) is not int or not 1<=data['track']<=3000):
                    raise DeviceError('请连接支持编号点播的 C3，输入 1–3000 的文件编号')
                if data.get('op') not in ('finish','count','play','state_finish') or (data['op']=='count' and (type(data.get('count')) is not int or not 0<=data['count']<=32)):
                    raise DeviceError('按键曲目数必须是 0–32 的整数')
                if data.get('op')=='state_finish' and not self.state.get('state_voice'):
                    raise DeviceError('当前设备不支持分状态播报')
            elif action == 'light_notice':
                if not self.state['connected']: raise DeviceError('请先连接 Nano')
                if self.state.get('screen_supported') is False:
                    raise DeviceError('当前灯板固件仅支持灯控，没有 OLED 通知功能')
                validate_screen({k:data.get(k) for k in ('rotation','mode')})
                if data.get('state') not in SCREEN_STATES: raise DeviceError('屏幕状态不支持')
                if data['mode']=='OFF':raise DeviceError('屏幕已关闭，请先切换显示模式')
                if type(data.get('seconds')) is not int or not 5<=data['seconds']<=120:raise DeviceError('通知停留时间应为 5–120 秒')
                data['bitmap']=render_notice(data.get('text'),data['rotation'])
            elif action == 'light_screen':
                if not self.state['connected']: raise DeviceError('请先连接 Nano')
                validate_screen({k:data.get(k) for k in ('rotation','mode')})
                if data.get('state') not in SCREEN_STATES: raise DeviceError('屏幕状态不支持')
            elif action == "light_ai":
                if not self.state["connected"]:
                    raise DeviceError("请先连接灯控")
                if (data.get("state") not in ("IDLE", "RUNNING", "WAITING", "ERROR", "OFFLINE") and
                        not (data.get("state")=="DONE" and self.state.get("state_voice"))):
                    raise DeviceError("AI 状态不支持")
                if "color" in data or "effect" in data:
                    pattern = validate_pattern({k: data.get(k) for k in ("color", "effect")})
                    if data["state"] == "OFFLINE" and pattern != DEFAULT_PATTERNS["offline"]:
                        raise DeviceError("离线状态必须熄灯")
            elif action == "light_send":
                if not self.state["connected"]:
                    raise DeviceError("请先连接灯控")
                if not isinstance(data.get("command"), str) or data["command"] not in COMMANDS | {"NET ON"}:
                    raise DeviceError("不支持的灯控指令")
                if data["command"] == "NET ON" and self.state["transport"] != "serial":
                    raise DeviceError("准备 Wi-Fi 需要通过 Nano USB 发送")
            elif action != "light_disconnect":
                raise DeviceError("不支持的灯控操作")
            self.cancelled.clear()
            self._set(busy=True, result="running", message="正在连接…" if action in ("light_connect", "light_switch_serial") else "正在等待回执…")
            self.worker = threading.Thread(target=self._run, args=(action, dict(data)), daemon=True)
            self.worker.start()

    def _run(self, action, data):
        try:
            if action in ("light_connect", "light_switch_serial"):
                if action == "light_switch_serial":
                    self._close()
                self._connect(data)
            elif action == "light_disconnect":
                self._close()
                self._set(message="灯控连接已释放；这不会停止 Nano 上的自动循环。", result="idle")
                self.log("灯控连接已释放；需要熄灯请先点击全灭。")
            elif action == 'light_notice':
                self._screen({**data,'done_text':'已完成'})
                if self.state.get('screen_supported') is False:
                    self._set(result='error', message='当前灯板没有 OLED 通知功能；灯控连接保持。')
                    return
                if self.state.get('screen_error'):raise DeviceError(self.state['screen_error'])
                self._notice(data['bitmap'],data['seconds'])
                self.completion_key=None
                self.notice_until=time.monotonic()+data['seconds']
                self._set(notice_message='通知已发送，停留 '+str(data['seconds'])+' 秒后恢复。')
            elif action == 'light_sound':
                self._sound(data)
            elif action == 'light_screen':
                self._screen(data)
            elif action == "light_ai":
                self._command("AI " + data["state"] + (" " + data["color"] + " " + data["effect"] if "color" in data else ""), quiet=data.get("quiet", False))
            else:
                self._command(data["command"])
        except Exception as error:
            message = str(error) if isinstance(error, DeviceError) else "灯控连接失败：检查端口占用、USB、模块供电和 TCP 地址。"
            self._close()
            self._set(result="error", message=message)
            self.log(message, "error")
        finally:
            self._set(busy=False)

    def _connect(self, data):
        self.completion_key=None
        self.notice_until=0
        with self.lock:
            self.state.update(sound_v2=False, numbered_play=False, sound_ready_at=0, sound_count=None, sound_message='',max_pcm=False,state_voice=False,pcm_features=[])
            self.state.pop('screen', None)
            self.state.pop('notice_message', None)
        transport = data["transport"]
        self._set(transport=transport, reply="", mode="", color="", pins=None, updated=0, ai_state="", level=0, ai_color="", ai_effect="", voice_hold=False,
                  screen_supported=None, screen_error='')
        if transport == "serial":
            found=next((p for p in list_ports.comports() if p.device==data["port"]),None)
            usb_id=(getattr(found,'vid',None),getattr(found,'pid',None))
            if usb_id==(0x303a,0x1001):
                self.link=serial.Serial(port=None,baudrate=115200,timeout=.05,write_timeout=1)
                self.link.dtr=False;self.link.rts=False;self.link.port=data["port"];self.link.open()
            elif os.name=="nt" and usb_id==(0x1a86,0x7523):
                self.link=CachedCH340Serial(data["port"],115200,timeout=.05,write_timeout=1)
            else:
                self.link=serial.Serial(data["port"],115200,timeout=.05,write_timeout=1)
            self._set(endpoint=data["port"]+" · 115200")
            # Opening classic Nano USB can reset it; hold one connection afterwards.
            if self.cancelled.wait(2):
                raise DeviceError("灯控连接已取消")
            self.link.reset_input_buffer()
        else:
            host = validate_host(data["host"])
            self.link = socket.create_connection((host, data["tcp_port"]), timeout=3)
            self.link.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            self.link.settimeout(.1)
            self._set(endpoint=f"{host}:{data['tcp_port']}")
        self._command("STATUS")
        if self.state.get('sound_v2'):
            self._probe_pcm()
        self._set(connected=True)
        if getattr(self.link,"reused_config",False) is True:
            self.log("CH340 驱动兼容模式：沿用 115200/8N1，Nano 已返回状态。","info")
        self.log("灯控已连接 " + self.state["endpoint"], "success")

    def _probe_pcm(self):
        self._write('PCM HELLO',quiet=True)
        for x in self._lines(1.35):
            if x=='OK PCM MAX98357A_V1':
                self._set(max_pcm=True,screen_supported=False,screen_error='')
                break
            if x.startswith('ERR'):return
        if not self.state.get('max_pcm'):return
        self._write('SOUND STATE STATUS',quiet=True)
        for x in self._lines(1.35):
            if x.startswith('OK SOUND STATE ') and ' done=' in x:
                self._set(state_voice=True)
                break
            if x.startswith('ERR'):break
        self._write('PCM FEATURES',quiet=True)
        for x in self._lines(1.35):
            if x.startswith('OK PCM features='):
                self._set(pcm_features=[s for s in x.split('=',1)[1].split(',') if s in {'LIST','USB_DELETE','LIVE','GAIN_SAVE','BUTTONS','LAN_READ'}])
                break
            if x.startswith('ERR'):break

    def _write(self, command, quiet=False):
        payload = (command + "\r\n").encode("ascii")
        if self.state["transport"] == "serial":
            self.link.reset_input_buffer()
            if self.link.write(payload) != len(payload):
                raise DeviceError("灯控指令未完整发送，请重新连接后查询状态")
            self.link.flush()
        else:
            self.link.sendall(payload)
        if not quiet:
            self.log("灯控发送 " + command, "tx")

    def _lines(self, timeout):
        end = time.monotonic() + timeout
        buffer = bytearray()
        while time.monotonic() < end and not self.cancelled.is_set():
            if self.state["transport"] == "serial":
                chunk = self.link.read(self.link.in_waiting or 1)
            else:
                try:
                    chunk = self.link.recv(1024)
                except socket.timeout:
                    continue
                if not chunk:
                    raise DeviceError("TCP 已断开，请重新连接并查询状态")
            buffer.extend(chunk)
            if len(buffer) > 4096:
                raise DeviceError("收到的灯控数据过长，请确认连接的是 Nano 灯控程序")
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer = bytearray(rest)
                yield line.decode("ascii", errors="replace").strip()

    def _sound(self, data):
        if data.get('op')=='state_finish':
            from audio_bridge import SharedWire
            from device import get_state_tracks, play_track
            f=SharedWire(self)
            slot=get_state_tracks(f).get('done',0)
            if slot:
                try:play_track(f,slot)
                except RuntimeError:
                    self._set(sound_message='完成播报编号未能播放，保持灯态。');return
                self._set(sound_message=f'已触发完成播报 · {slot:04}')
            return
        track=data.get('track',1)
        cfg=data['op']=='count'
        cmd='SOUND COUNT '+str(data['count']) if cfg else (f'SOUND PLAY {track}' if data['op']=='play' else 'SOUND FINISH')
        expected='OK SOUND count='+str(data['count']) if cfg else f'OK SOUND queued track={track:04}'
        self._write(cmd,quiet=True)
        for line in self._lines(2):
            if line==expected:
                if cfg:self._set(sound_count=data['count'])
                self._set(sound_message='按键曲目数已存入灯板' if cfg else f'已请求点播 /mp3/{track:04}.mp3（无播放回传）')
                return
            if line.startswith('ERR'):break
        self.log('语音指令未确认；不重发，保留灯光跟随','warn')
        self._set(sound_message='语音指令未确认，不重发')

    def _screen(self, data):
        if self.state.get('screen_supported') is False:
            self._set(result='idle')
            return
        self._write(f"SCREEN {data['rotation']} {data['mode']} {data['state']}", quiet=True)
        for line in self._lines(4):
            # Known non-OLED firmware explicitly rejects this top-level command.
            # Do not infer capability from silence or mask a real ERR SCREEN.
            if line.startswith('ERR use LIGHT HELP'):
                self._set(screen_supported=False, screen_error='', result='idle',
                          message='当前固件仅支持灯控；AI 跟随正常，已停止屏幕查询。')
                return
            if line.startswith('ERR'):
                self._set(screen_error='Nano 未确认屏幕指令，请检查 OLED 固件。')
                return
            match=SCREEN_REPLY.fullmatch(line)
            if match:
                rotation,mode,state,online=match.groups()
                if (int(rotation),mode,state)!=(data['rotation'],data['mode'],data['state']):
                    raise DeviceError('屏幕回执与指令不匹配')
                self._set(screen_supported=True, screen={'rotation':int(rotation),'mode':mode,'state':state,'online':online=='1','updated':time.time()},
                          result='success' if online=='1' else 'error', message='屏幕设置已确认' if online=='1' else 'OLED 暂未应答，正在重试。',
                          screen_error='' if online=='1' else 'Nano 已收设置，OLED 暂未应答，正在重试。')
                done = mode == 'DONE' or (mode in ('AUTO','STATUS') and state == 'DONE')
                key = (rotation,mode,state,data.get('done_text','已完成')) if done else None
                if not done:self.completion_key=None
                if done and data.get('done_text','已完成')=='已完成' and self.completion_key is not None:
                    self._oled_exchange('OLED CLEAR','OK OLED CLEAR')
                    self.completion_key=None
                if online=='1' and done and data.get('done_text','已完成')!='已完成' and key!=self.completion_key and time.monotonic()>=self.notice_until:
                    self._notice(render_notice(data['done_text'],int(rotation)),0)
                    self.completion_key=key
                return
        self._set(screen_error='屏幕回执超时；需要烧录配套 OLED 固件。')

    def _oled_exchange(self, command, expected):
        self._write(command,quiet=True)
        for line in self._lines(4):
            if line==expected:return
            if line.startswith('ERR'):raise DeviceError('OLED 通知发送失败：'+line)
        raise DeviceError('OLED 通知回执超时')

    def _notice(self, bitmap, seconds):
        self._oled_exchange('OLED BEGIN '+str(seconds),'OK OLED BEGIN')
        for offset in range(0,1024,16):
            self._oled_exchange('OLED DATA '+str(offset)+' '+bitmap[offset:offset+16].hex().upper(),'OK OLED DATA '+str(offset))
        self._oled_exchange('OLED END','OK OLED END')

    def _command(self, command, quiet=False):
        if command == "NET ON":
            self._write(command)
            for line in self._lines(6):
                if line.startswith("NET READY:"):
                    self._set(reply=line, result="success", message="无线已就绪。按下方回执地址切换到 Wi-Fi TCP；USB 线可继续供电。")
                    self.log(line, "rx")
                    return
                if line.startswith("NET unconfirmed:"):
                    self._set(reply=line, result="error", message="C3 请先用 USB 配网工具查看是否已连路由器；老灯请检查 M30 透传及接线。")
                    self.log(line, "warn")
                    return
            raise DeviceError("没有收到网络准备结果。请确认 Nano 灯控程序版本。")
        self._write("LIGHT " + command, quiet=quiet)
        for line in self._lines(4):
            if command=="STATUS":
                if line=="CAPS TOY_PLAY_V1 max=3000" and self.state.get("sound_v2"):
                    self._set(numbered_play=True);continue
                name="TOY_SOUND_V2" if self.state["transport"]=="serial" else "TOY_SOUND_LAN_V1"
                cap=re.fullmatch(r"CAPS "+name+r" count=(\d{1,2})",line)
                if cap and int(cap[1])<=32:
                    self._set(sound_v2=True,sound_count=int(cap[1]),sound_ready_at=self.state.get('sound_ready_at') or time.time())
                    continue
            if line.startswith("ERR LIGHT"):
                raise DeviceError(line)
            if command == "HELP" and line.startswith("HELP LIGHT "):
                self._set(reply=line, result="success", message="已收到指令帮助")
                self.log(line, "rx")
                return
            voice_match = VOICE_REPLY.fullmatch(line)
            if voice_match:
                ai_state, color = voice_match.group(1, 2)
                pins = tuple(map(int, voice_match.group(3, 4, 5)))
                if pins != BITS[color]:
                    raise DeviceError("语音接管回执颜色与电平不一致")
                if command.startswith("AI "):
                    if ai_state != command.split()[1]:
                        raise DeviceError("语音接管回执中的 AI 状态不匹配")
                elif command != "STATUS":
                    raise DeviceError("手动指令未被灯板确认")
                self._set(reply=line, result="success", message="语音已接管灯光；说“恢复跟随”恢复 AI 灯效。",
                          mode="VOICE", color=color, pins=list(pins), voice_hold=True,
                          ai_state=ai_state, level=255 if color != "OFF" else 0,
                          ai_color="", ai_effect="", updated=time.time())
                if not quiet:
                    self.log(line, "rx")
                return
            ai_match = AI_REPLY.fullmatch(line)
            if ai_match:
                state = ai_match.group(1)
                pins = tuple(map(int, ai_match.group(2,3,4)))
                level, lease = int(ai_match.group(5)), int(ai_match.group(6))
                configured_color, effect = ai_match.group(7, 8)
                if configured_color:
                    validate_pattern(dict(color=configured_color, effect=effect))
                    if state == "OFFLINE" and configured_color != "OFF":
                        raise DeviceError("离线状态回执未熄灯")
                    valid_pins = {BITS[configured_color]}
                    if effect in ("SLOW", "FAST"):
                        valid_pins.add(BITS["OFF"])
                    valid_level = 20 <= level <= 255 if effect == "BREATHE" else level == (255 if any(pins) else 0)
                else:
                    legacy = DEFAULT_PATTERNS["idle" if state=="DONE" else state.lower()]
                    configured_color, effect = legacy["color"], legacy["effect"]
                    valid_pins = {BITS[configured_color]}
                    if effect in ("SLOW", "FAST"):
                        valid_pins.add(BITS["OFF"])
                    valid_level = 20 <= level <= 255 if effect == "BREATHE" else level == (255 if any(pins) else 0)
                if pins not in valid_pins or not valid_level or lease != 15000:
                    raise DeviceError("AI 灯态回执格式或电平不一致")
                parts = command.split()
                if command.startswith("AI ") and state != parts[1]:
                    raise DeviceError("AI 回执状态与指令不符")
                if len(parts) == 4 and (ai_match.group(7) != parts[2] or ai_match.group(8) != parts[3]):
                    raise DeviceError("灯板未确认自定义灯效，请更新 Nano 灯控程序")
                if not command.startswith("AI ") and command != "STATUS":
                    raise DeviceError("手动指令未退出 AI 模式")
                color = next((name for name,bits in BITS.items() if bits == pins), "OFF")
                if command.startswith('AI '):self.last_ai_command='LIGHT '+command
                self._set(reply=line, result="success", message="已收到 AI指示灯回执", mode="AI", voice_hold=False,
                    color=color, pins=list(pins), ai_state=state, level=level, ai_color=configured_color, ai_effect=effect, updated=time.time())
                if not quiet:
                    self.log(line,"rx")
                return
            if command.startswith("AI "):
                continue
            match = STATE_REPLY.fullmatch(line)
            if not match:
                continue
            mode, color = match.group(1, 2)
            pins = tuple(map(int, match.group(3, 4, 5)))
            if pins != BITS[color] or (mode == "AUTO" and color == "OFF"):
                raise DeviceError("灯控回执颜色与电平不一致，请重新查询")
            if command in BITS and (color != command or mode != "MANUAL"):
                raise DeviceError("灯态与指令不符，请重新连接并查询状态")
            if command == "AUTO" and mode != "AUTO":
                raise DeviceError("自动循环未确认，请重新查询状态")
            self.last_ai_command=None
            self._set(reply=line, result="success", message="已收到灯控回执", mode=mode, voice_hold=False,
                      color=color, pins=list(pins), updated=time.time(), ai_state="", level=255 if color != "OFF" else 0)
            self.log(line, "rx")
            return
        raise DeviceError("未收到灯控回执，已断开且未重发。检查 Nano 灯控程序、模块透传及连接；重连后先查询状态。")

    def serial_handle_alive(self):
        """Read driver status only after a removal notification, never by timer."""
        if self.link is None:
            return False
        try:
            _ = self.link.in_waiting
        except OSError:
            return False
        return True

    def _close(self):
        if self.link:
            try:
                self.link.close()
            except OSError:
                pass
        self.link = None
        self.last_ai_command=None
        self._set(connected=False,sound_v2=False,numbered_play=False,sound_ready_at=0,max_pcm=False,state_voice=False,pcm_features=[])

    def shutdown(self):
        self.cancelled.set()
        if self.worker:
            self.worker.join(timeout=4)
        self._close()
