"""One verified C3 link, shared by lamp polling, Wi-Fi and audio."""
import contextlib, json, socket, time
import device as d
import lan_readback
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

def preview_recording(server,endpoint,slot,size,progress):
    if server.controller.demo:raise DeviceError('演示模式不连接真实 C3。')
    lights=server.lights
    with server.connection_lock:
        light=lights.snapshot()
        live_link=lights.link
        if not light.get('connected') or not light.get('max_pcm') or light.get('endpoint')!=endpoint:
            raise DeviceError('灯板已切换，请刷新设备列表。')
        serial=light.get('transport')=='serial'
        peer=None if serial else live_link.getpeername()
    if serial:
        with reserve(server) as (target,f):
            if f.link is not live_link:raise DeviceError('灯板已切换，请刷新设备列表。')
            return d.read_track_pcm(f,slot,f.mark(server,progress),size),'usb'
    needs_pair=None
    if 'LAN_READ' in light.get('pcm_features',()):
        try:
            pcm=lan_readback.read(peer,slot,size,progress)
        except lan_readback.PairRequired as exc:
            needs_pair=exc
        else:
            with server.connection_lock:
                if lights.link is not live_link:raise DeviceError('灯板连接已切换；请重新试听。')
            return pcm,'lan'
    # Unpaired and legacy firmware can still read over a verified physical USB side link.
    opened=False
    try:
        with d.usb_peer_for_ip(peer[0]) as f:
            opened=True
            pcm=d.read_track_pcm(f,slot,progress,size)
    except ValueError as exc:
        if needs_pair and not opened:raise DeviceError('首次需接同一块 C3 的原生 USB，然后点「配对局域网回读」。') from exc
        raise
    with server.connection_lock:
        if lights.link is not live_link:raise DeviceError('灯板连接已切换；请重新试听。')
    return pcm,'usb'

def pair_recording_readback(server):
    if server.controller.demo:raise DeviceError('演示模式不连接真实 C3。')
    with server.connection_lock:
        light=server.lights.snapshot();link=server.lights.link
        if not light.get('connected') or 'LAN_READ' not in light.get('pcm_features',()):
            raise DeviceError('请先连接支持局域网回读的新版 C3 固件。')
        on_usb=light['transport']=='serial'
        peer=None if on_usb else link.getpeername()[0]
    if on_usb:
        with reserve(server) as (_,wire):
            if wire.link is not link:raise DeviceError('灯板已切换，请重新配对。')
            lan_readback.pair(wire)
    else:
        with d.usb_peer_for_ip(peer) as wire:
            with server.connection_lock:
                if server.lights.link is not link:raise DeviceError('灯板已切换，请重新配对。')
            lan_readback.pair(wire)
    return '局域网回读已配对；现在可拔掉 USB 数据线，保持 C3 供电。'

def run(server,op,data,progress,source=None,segments=None,cancel=None):
    with reserve(server) as (target,f):
        meter=f.mark(server,progress)
        light=server.lights.snapshot()
        features=light.get('pcm_features',())
        def inventory():
            st=d.kv(d.okay(f,'PCM STATUS','OK PCM '))
            bindings=d.get_state_tracks(f) if light.get('state_voice') else {}
            return {'tracks':d.list_tracks(f,meter) if st.get('fs')=='1' else [],
                    'bindings':bindings,'library_endpoint':light['endpoint'],'complete':True,
                    'buttons':d.buttons_status(f) if 'BUTTONS' in features else None,
                    'default_gain':int(st['default_gain']) if 'GAIN_SAVE' in features else None}
        if op=='status':
            return {'amp':d.kv(d.okay(f,'PCM STATUS','OK PCM ')),
                    'bindings':d.get_state_tracks(f) if light.get('state_voice') else None,
                    'buttons':d.buttons_status(f) if 'BUTTONS' in features else None}
        if op=='wifi':
            if target.mode!='USB':raise ValueError('请在灯控页用 C3 USB 连接后配网。')
            return d.wifi_provision(f,data.get('ssid',''),data.get('password',''),meter)
        if op=='net':
            if target.mode!='USB':raise ValueError('查询配网状态需 C3 USB 连接。')
            return d.net_status(f)
        if op=='read':
            return d.stored_audio(target,data.get('slot',0),meter,connector=f.connector)
        if op in ('push','button_push'):
            if op=='button_push':
                if 'BUTTONS' not in features:raise DeviceError('请更新 C3 固件后使用自选歌单。')
                result=d.push_button_clips(target,f,source,segments,meter,features)
            else:
                result=d.push_segments(target,source,segments,data.get('play',False),data.get('gain',7),meter,connector=f.connector,features=features)
            result['library_endpoint']=light['endpoint']
            if 'LIST' in features:
                try:result.update(inventory())
                except (OSError,RuntimeError) as e:result['message']+=f' 刷新设备列表未完成：{e}'
            return result
        if op=='library':
            if 'LIST' not in features:raise DeviceError('当前 C3 固件不支持读取完整录音列表。')
            return {**inventory(),'message':'已读取 C3 里的录音。'}
        if op=='delete':
            if not (target.mode=='USB' and 'USB_DELETE' in features):
                raise DeviceError('删除请用支持录音管理的 C3 固件并连接原生 USB。')
            n=data['slot'];expected=data.get('bytes')
            d.require(f,f'PCM INFO {n}',f'OK PCM bytes={expected}')
            bindings=d.get_state_tracks(f)
            if sorted(k.upper() for k,v in bindings.items() if v==n)!=sorted(data.get('states',[])):
                raise DeviceError('该录音的状态绑定有变化；请刷新列表再删除。')
            d.require(f,f'SOUND DELETE {n}',f'OK SOUND deleted={n}')
            result={'library_endpoint':light['endpoint'],'deleted_slot':n,'message':'录音已从 C3 删除；相关状态已解除绑定。'}
            try:result.update(inventory())
            except (RuntimeError,OSError):result['message']+=' 请刷新以重新核对设备列表。'
            return result
        if op=='live':
            if 'LIVE' not in features:raise DeviceError('当前 C3 固件不支持 RAM 实时试听。')
            return d.live_play(f,source,data.get('gain',7),meter,cancel,scoped_gain='GAIN_SAVE' in features,
                               window=16 if not f.serial else 1)
        if op=='speak':
            if 'LIVE' not in features:raise DeviceError('当前 C3 不支持直接播报。')
            if data.get('speech_target')!=(light['transport'],light['endpoint']):
                raise DeviceError('生成期间连接的灯板已切换，未发送播报。')
            st=d.kv(d.okay(f,'PCM STATUS','OK PCM '))
            if st.get('playing')!='0' or st.get('last')=='streaming':
                raise DeviceError('C3 正在播报，请等当前语音结束后再发送。')
            gain=int(st.get('default_gain',st.get('gain','7')))
            r=d.live_play(f,source,gain,meter,cancel,scoped_gain='GAIN_SAVE' in features,window=16 if not f.serial else 1)
            return 'C3 直接播报结束；没有保存录音或更改状态绑定。' if r.startswith('C3 实时试听结束') else r
        if op=='gain':
            if 'GAIN_SAVE' not in features:raise DeviceError('当前固件不能保存默认音量，请更新 C3。')
            gain=data.get('gain');d.save_gain(f,gain)
            return {'message':f'C3 默认音量已保存为 {gain} 档，重启后仍使用此设置。','default_gain':gain}
        if op=='play':
            gain=data.get('gain',-1);slot=data.get('slot',0)
            if 'GAIN_SAVE' in features:d.play_track(f,slot,gain)
            else:d.set_gain(f,gain);d.play_track(f,slot)
            return 'C3 已开始播放所选编号。'
        if op=='stop':
            d.require(f,'PCM STOP','OK PCM stopped');return '已停止。'
        if op=='count':
            d.set_button_count(f,data.get('count',-1))
            return {'message':'固定编号按键歌单已写入。','buttons':d.buttons_status(f) if 'BUTTONS' in features else None}
        if op=='buttons':
            if 'BUTTONS' not in features:raise DeviceError('请更新 C3 固件后设置按键随机播放。')
            before=d.buttons_status(f)
            if data.get('expected')!=before:raise DeviceError('设备歌单已变化；请先刷新。')
            b=d.save_buttons(f,data.get('mode'),data.get('slots'))
            return {'message':'顶部按键随机播放设置已保存。','buttons':b}
        if op=='bind':
            updates=data.get('bindings')
            if not isinstance(updates,dict) or not updates or len(updates)>6:raise ValueError('状态绑定格式不正确。')
            if any(k not in dict(d.STATES) or type(v) is not int or not 0<=v<=3000 for k,v in updates.items()):
                raise ValueError('状态编号必须是 0–3000 的整数。')
            for state,n in updates.items():d.set_state_track(f,state,n)
            return {'message':'状态播报配置已写入 C3。','bindings':d.get_state_tracks(f)}
        raise ValueError('未知音频操作。')
