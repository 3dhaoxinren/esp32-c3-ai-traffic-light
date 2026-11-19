"""One verified C3 link, shared by lamp polling, Wi-Fi and audio."""
import contextlib, json, socket, time
import device as d
from core import DeviceError

class WireLost(OSError):pass

class SharedWire:
    def __init__(self,lights):
        self.lights=lights
        self.link=lights.link
        self.serial=lights.snapshot()['transport']=='serial'
        self.buffer=bytearray()
        self.last_lease=time.monotonic()
    def write(self,raw):
        try:
            if self.serial:
                if self.link.write(raw)!=len(raw):raise WireLost('C3 串口写入不完整。')
                self.link.flush()
            else:self.link.sendall(raw)
        except (OSError,socket.error) as e:raise WireLost('C3 连接已断开。') from e
    def readline(self,n):
        end=time.monotonic()+4
        while time.monotonic()<end:
            idx=self.buffer.find(b'\n')
            if idx>=0:
                i=idx+1;b=bytes(self.buffer[:i]);del self.buffer[:i]
                if i>n:raise WireLost('C3 回执太长。')
                return b
            if len(self.buffer)>=n:raise WireLost('C3 回执超出上限。')
            try:
                if self.serial:b=self.link.readline(n-len(self.buffer))
                else:
                    b=self.link.recv(256)
                    if not b:raise WireLost('C3 网络连接已断开。')
                self.buffer.extend(b)
            except socket.timeout:continue
            except (OSError,ValueError) as ex:raise WireLost('C3 连接中断。') from ex
        raise WireLost('C3 回执超时；连接已释放，自动重连可重新接管。')
    def mark(self,server,callback):
        def progress(n):
            callback(n)
            # Only repeat the exact applied LIGHT AI command between fully acknowledged
            # PCM chunks. This extends the lease without firing a second state broadcast.
            cmd=self.lights.last_ai_command
            if server.ai.enabled and cmd and time.monotonic()-self.last_lease>=5:
                d.okay(self,cmd,'OK AI ')
                self.last_lease=time.monotonic()
        return progress
    @contextlib.contextmanager
    def connector(self,_mode,_address):
        yield self

@contextlib.contextmanager
def reserve(server):
    if server.controller.demo:raise DeviceError('演示模式不写真实 C3。')
    lights=server.lights
    end=time.monotonic()+4
    while True:
        with server.connection_lock:
            s=lights.snapshot()
            if not s['connected']:raise DeviceError('请先在灯控页连接 C3。')
            if not s.get('max_pcm'):raise DeviceError('当前是 Nano 或旧款灯板；C3 音频要求已识别的 MAX98357A 固件。')
            if not s['busy']:
                lights._set(busy=True)
                break
        if time.monotonic()>=end:raise DeviceError('灯控正在忙，请稍后再试。')
        time.sleep(.06)
    wire=SharedWire(lights)
    target=d.Target('USB' if wire.serial else 'WiFi',s['endpoint'])
    try:
        yield target,wire
    except WireLost:
        lights._close()
        raise
    finally:
        lights._set(busy=False)

def run(server,op,data,progress,source=None,segments=None):
    with reserve(server) as (target,f):
        meter=f.mark(server,progress)
        if op=='status':
            return {'amp':d.kv(d.okay(f,'PCM STATUS','OK PCM ')),
                    'bindings':d.get_state_tracks(f) if server.lights.snapshot().get('state_voice') else None}
        if op=='wifi':
            if target.mode!='USB':raise ValueError('请在灯控页用 C3 USB 连接后配网。')
            return d.wifi_provision(f,data.get('ssid',''),data.get('password',''),meter)
        if op=='net':
            if target.mode!='USB':raise ValueError('查询配网状态需 C3 USB 连接。')
            return d.net_status(f)
        if op=='read':
            return d.stored_audio(target,data.get('slot',0),meter,connector=f.connector)
        if op=='push':
            return d.push_segments(target,source,segments,data.get('play',False),data.get('gain',7),meter,connector=f.connector)
        if op=='gain':
            d.set_gain(f,data.get('gain',-1));return 'C3 RAM 音量已应用；重启回到 7 档。'
        if op=='play':
            d.set_gain(f,data.get('gain',-1));d.play_track(f,data.get('slot',0))
            return 'C3 已开始播放所选编号。'
        if op=='stop':
            d.require(f,'PCM STOP','OK PCM stopped');return '已停止。'
        if op=='count':
            d.set_button_count(f,data.get('count',-1));return '顶部按键语音数已写入。'
        if op=='bind':
            updates=data.get('bindings')
            if not isinstance(updates,dict) or not updates or len(updates)>6:raise ValueError('状态绑定格式不正确。')
            if any(k not in dict(d.STATES) or type(v) is not int or not 0<=v<=3000 for k,v in updates.items()):
                raise ValueError('状态编号必须是 0–3000 的整数。')
            for state,n in updates.items():d.set_state_track(f,state,n)
            return '状态播报配置已写入 C3。'
        raise ValueError('未知音频操作。')
