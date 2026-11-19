"""C3 MAX98357A preview. Nothing is opened until the user asks."""
import ipaddress, re, shutil, socket, subprocess, tempfile, wave, zlib
from pathlib import Path
from contextlib import contextmanager
LIMIT=1024000
RATE=16000
MAX_GAIN=9
def valid(data):
    if not data or len(data)%2 or len(data)>LIMIT:
        raise ValueError("音频需为非空单声道 PCM，时长最多 32 秒。请先裁剪。")
    return data
def readwav(path):
    with wave.open(str(path),'rb') as f:
        if (f.getnchannels(),f.getsampwidth(),f.getframerate(),f.getcomptype())!=(1,2,RATE,'NONE'):
            raise ValueError("需转换采样格式")
        n=f.getnframes()
        if n*2>LIMIT: raise ValueError("超过 32 秒")
        data=f.readframes(n)
        if len(data)!=n*2: raise ValueError("WAV 不完整")
        return valid(data)
def loadfile(path,ffmpeg=None):
    if not Path(path).is_file():raise ValueError("找不到音频文件")
    try:return readwav(path)
    except (ValueError, wave.Error,EOFError):pass
    ff=ffmpeg or shutil.which('ffmpeg')
    if not ff:raise ValueError("MP3/其他 WAV 格式需要 FFmpeg，请选择 ffmpeg.exe。")
    p=subprocess.run([ff,'-nostdin','-hide_banner','-loglevel','error','-i',str(Path(path).resolve()),'-map','0:a:0','-t','32.001','-ac','1','-ar',str(RATE),'-f','s16le','pipe:1'],capture_output=True,timeout=45,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if p.returncode: raise ValueError("FFmpeg 无法解码此文件")
    return valid(p.stdout)
def savewav(path,data):
    with wave.open(str(path),'wb') as w:
        w.setparams((1,2,RATE,0,'NONE','not compressed'));w.writeframes(valid(data))
def speech(text):
    if not text.strip() or len(text)>500:raise ValueError("请输入 1–500 字，生成的语音最多 32 秒。")
    import os
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/'tts.wav'
        script=r"""$ErrorActionPreference='Stop';Add-Type -AssemblyName System.Speech;
$s=[System.Speech.Synthesis.SpeechSynthesizer]::new();
$t=[IO.File]::ReadAllText($env:C3_SPEECH_FILE,[Text.Encoding]::UTF8);
if($t -match '[\u3400-\u9fff]') {
$v=$s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'zh-*' } | Select-Object -First 1;
if(!$v){throw 'No Chinese SAPI voice installed'};$s.SelectVoice($v.VoiceInfo.Name) };
$f=[System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(16000,[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,[System.Speech.AudioFormat.AudioChannel]::Mono);
try {$s.SetOutputToWaveFile($env:C3_SPEECH_WAV,$f);$s.Speak($t)} finally {$s.Dispose()}"""
        inp=Path(d)/'text.txt';inp.write_text(text,encoding='utf-8')
        p=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
            env={**os.environ,'C3_SPEECH_WAV':str(path),'C3_SPEECH_FILE':str(inp)},capture_output=True,timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if p.returncode: raise RuntimeError("Windows 本地语音合成失败。中文需要可用的中文 SAPI 语音，也可导入录音。")
        return readwav(path)
def ports():
    from serial.tools import list_ports
    return [p.device for p in list_ports.comports() if (p.vid,p.pid)==(0x303a,0x1001)]
def usb_address(address):
    name=address.strip().upper()
    if re.fullmatch(r'[0-9]{1,5}',name):name='COM'+name
    available={p.upper():p for p in ports()}
    if name not in available:
        raise ValueError("未找到 C3 原生 USB。请选择 C3 串口（例如 COM19 或 19）；Nano/CH340 不会被打开。")
    return available[name]
@contextmanager
def connection(mode,address):
    if mode=='USB':
        address=usb_address(address)
        import serial
        port=serial.Serial(port=None,baudrate=115200,timeout=4,write_timeout=4)
        port.dtr=False;port.rts=False;port.port=address
        with port as f:
            f.reset_input_buffer()
            yield f
    else:
        addr=ipaddress.IPv4Address(address)
        if not any(addr in ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):raise ValueError("请输入局域网 IPv4 地址")
        with socket.create_connection((str(addr),8080),timeout=4) as sock, sock.makefile('rwb',buffering=0) as f:
            # sendall rather than short writes from an unbuffered socket file.
            class Link:
                def write(self,data):sock.sendall(data)
                def readline(self,n):return f.readline(n)
            yield Link()
def send(f,line):f.write((line+'\n').encode('ascii'))
def response(f):
    r=f.readline(320)
    if not r.endswith(b'\n'):raise RuntimeError("响应超时或过长")
    return r.decode('ascii','replace').strip()
def require(f,line,want):
    send(f,line);s=response(f)
    if s!=want:raise RuntimeError(s)
def identify(f):
    send(f,'LIGHT STATUS');caps=False
    for _ in range(4):
        s=response(f)
        if re.fullmatch(r'CAPS TOY_SOUND_(V2|LAN_V1) count=\d+',s):caps=True
        if s.startswith(('OK LIGHT ','OK AI ')):break
    else:raise ValueError("不是可识别的灯控设备")
    if not caps:raise ValueError("老灯没有音频能力，已停止。")
    require(f,'PCM HELLO','OK PCM MAX98357A_V1')
def upload(f,slot,data,progress=lambda n:None):
    valid(data)
    if not 1<=slot<=3000:raise ValueError("编号 1–3000")
    send(f,f'PCM START {slot} {len(data)} {zlib.crc32(data):08X}')
    s=response(f);m=re.fullmatch(r'OK PCM token=([0-9A-F]{8})',s)
    if not m:raise RuntimeError(s)
    token=m[1]
    for off in range(0,len(data),256):
        chunk=data[off:off+256]
        require(f,f'PCM DATA {token} {off} {chunk.hex().upper()}',f'OK PCM at={off+len(chunk)}')
        if off%8192==0:progress((off+len(chunk))*100//len(data))
    require(f,'PCM END '+token,'OK PCM saved');progress(100)

# Application-level API: no GUI, device registry, or credentials persisted here.
import time
from dataclasses import dataclass

@dataclass(frozen=True)
class Target:
    mode: str
    address: str
    def __post_init__(self):
        if self.mode not in ('USB','WiFi'):
            raise ValueError("请选择 USB 或 Wi-Fi。")
        if not self.address.strip():
            raise ValueError("请先选择设备或填写局域网 IP。")

def okay(f, line, prefix='OK '):
    send(f,line)
    info=response(f)
    if not info.startswith(prefix):
        raise RuntimeError(info if info.startswith('ERR ') else '设备响应无法识别。')
    return info

def lamp_status(f):
    send(f,'LIGHT STATUS')
    for _ in range(4):
        s=response(f)
        if s.startswith(('OK LIGHT ','OK AI ')):return kv(s)
        if s.startswith('ERR '):raise RuntimeError(s)
    raise RuntimeError("读不到灯的当前状态。")

def kv(line):
    return dict(re.findall(r'([a-z_][a-z_0-9]*)=([^\s]+)',line))

def net_status(f):
    return kv(okay(f,'NET STATUS','OK NET '))

def encode_wifi(ssid,password):
    n=ssid.encode('utf-8')
    p=password.encode('utf-8')
    if not 1<=len(n)<=32 or b'\0' in n:
        raise ValueError("Wi-Fi 名称需要 1–32 个 UTF-8 字节。")
    if not (8<=len(p)<=63 or len(p)==64 and re.fullmatch(rb'[\da-fA-F]{64}',p)) or b'\0' in p:
        raise ValueError("请输入 8–63 字节密码，或 64 位十六进制 PSK；不支持开放网络。")
    return 'NET SET '+n.hex().upper()+' '+p.hex().upper()

def wifi_provision(f,ssid,password,progress=lambda n:None,timeout=23,pause=time.sleep):
    require(f,encode_wifi(ssid,password),'OK NET saved')
    start=time.monotonic()
    while True:
        states=net_status(f)
        if states.get('state')=='ONLINE' and states.get('ip') not in (None,'0.0.0.0'):
            progress(100)
            return states
        elapsed=time.monotonic()-start
        if elapsed>=timeout:return states
        progress(8+min(84,int(elapsed/timeout*84)))
        pause(min(1.0,max(.05,timeout-elapsed)))

def with_link(target,work,connector=connection):
    with connector(target.mode,target.address) as f:
        identify(f)
        return work(f)

def check_device(target,connector=connection):
    def work(f):
        return {'lamp':lamp_status(f),'pcm':kv(okay(f,'PCM STATUS','OK PCM ')),
                'net':net_status(f) if target.mode=='USB' else {'state':'ONLINE','ip':target.address}}
    return with_link(target,work,connector)

def set_gain(f,n):
    if not isinstance(n,int) or not 0<=n<=9:raise ValueError("音量是 0–9 档。")
    require(f,f'PCM GAIN {n}',f'OK PCM gain={n}')

def set_button_count(f,n):
    if not isinstance(n,int) or not 0<=n<=32:raise ValueError("按键语音数须为 0–32。")
    require(f,f'SOUND COUNT {n}',f'OK SOUND count={n}')

def play_track(f,slot):
    if not isinstance(slot,int) or not 1<=slot<=3000:raise ValueError("编号是 1–3000。")
    require(f,f'PCM PLAY {slot}','OK PCM playing')

def transfer(target,data,slot,play,gain,progress=lambda n:None,connector=connection):
    def work(f):
        st=kv(okay(f,'PCM STATUS','OK PCM '))
        if st.get('i2s')!='1':raise RuntimeError("功放 I²S 尚未初始化，请先检查接线和固件。")
        if st.get('fs')!='1':
            if target.mode!='USB':raise RuntimeError("语音分区未就绪；先用 USB 连接，初始化空白分区。")
            require(f,'PCM INIT','OK PCM ready')
        upload(f,slot,data,progress)
        if play:
            set_gain(f,gain)
            try:play_track(f,slot)
            except (RuntimeError,OSError) as e:
                return f"音频 {slot:04d} 已保存并校验，但试播未确认：{e}"
        return f"音频 {slot:04d} 已保存并校验"+("，已开始播报。" if play else "。")
    return with_link(target,work,connector)


MAX_SOURCE=300*RATE*2
STATES=(('IDLE','空闲'),('RUNNING','运行中'),('WAITING','等待'),('ERROR','报错'),('DONE','完成'),('OFFLINE','离线'))

def load_source(path,ffmpeg=None):
    path=Path(path)
    if not path.is_file():raise ValueError("找不到音频文件。")
    try:
        with wave.open(str(path),'rb') as f:
            if (f.getnchannels(),f.getsampwidth(),f.getframerate(),f.getcomptype())==(1,2,RATE,'NONE'):
                length=f.getnframes()*2
                if not 0<length<=MAX_SOURCE:raise OverflowError("音频源不能超过 5 分钟。")
                data=f.readframes(length//2)
                if length==len(data):return data
    except (wave.Error,EOFError):pass
    except OverflowError as ex:raise ValueError(str(ex)) from ex
    ff=ffmpeg or shutil.which('ffmpeg')
    if not ff:raise ValueError("该格式要用 FFmpeg 转换；也可导入 16 kHz 单声道 WAV。")
    p=subprocess.run([ff,'-nostdin','-hide_banner','-loglevel','error','-i',str(path.resolve()),
       '-map','0:a:0','-t','300.001','-ac','1','-ar',str(RATE),'-f','s16le','pipe:1'],
       capture_output=True,timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if p.returncode or not p.stdout:raise ValueError("无法解码这份音频。")
    if len(p.stdout)>MAX_SOURCE:raise ValueError("音频源不能超过 5 分钟。")
    return p.stdout

@dataclass(frozen=True)
class Segment:
    start:int
    end:int
    slot:int
    state:str=''
    def audio(self,source):
        if not 0<=self.start<self.end<=len(source)//2:raise ValueError("选段超出音频范围。")
        if not 1<=self.slot<=3000:raise ValueError("设备编号须为 1–3000。")
        if self.state and self.state not in {s for s,_ in STATES}:raise ValueError("状态无效。")
        return valid(source[self.start*2:self.end*2])

def get_state_tracks(f):
    info=okay(f,'SOUND STATE STATUS','OK SOUND STATE ')
    return {k:int(v) for k,v in kv(info).items() if v.isdigit()}
def set_state_track(f,state,slot):
    if state not in {k for k,_ in STATES} or not isinstance(slot,int) or not 0<=slot<=3000:
        raise ValueError("状态绑定须用有效状态和 0–3000 编号。")
    require(f,f'SOUND STATE {state} {slot}',f'OK SOUND STATE track={slot}')

def stored_audio(target,slot,progress=lambda n:None,connector=connection):
    if target.mode!='USB':raise ValueError("请通过 USB 读取灯里已存的语音。")
    if not 1<=slot<=3000:raise ValueError("编号须为 1–3000。")
    def work(f):
        info=kv(okay(f,f'PCM INFO {slot}','OK PCM '))
        size=int(info.get('bytes','0'))
        if not 0<size<=LIMIT or size%2:raise ValueError("设备返回的音频长度无效。")
        data=bytearray()
        for off in range(0,size,120):
            n=min(120,size-off)
            q=kv(okay(f,f'PCM READ {slot} {off} {n}','OK PCM '))
            seq=q.get('hex','')
            if q.get('off')!=str(off) or len(seq)!=2*n or not re.fullmatch(r'[A-F0-9]+',seq):
                raise RuntimeError("音频读回数据不完整。")
            data.extend(bytes.fromhex(seq))
            if off%3840==0:progress(len(data)*100//size)
        progress(100)
        return valid(bytes(data))
    return with_link(target,work,connector)

def push_segments(target,source,segments,play,gain,progress=lambda n:None,connector=connection):
    parts=[(s,s.audio(source)) for s in segments]
    ids={s.slot for s,_ in parts};state=[s.state for s,_ in parts if s.state]
    if not parts or len(ids)!=len(parts) or len(state)!=len(set(state)):
        raise ValueError("请选择选段；同批不能重复编号或状态。")
    n=sum(len(data) for _,data in parts)
    def work(f):
        st=kv(okay(f,'PCM STATUS','OK PCM '))
        if st.get('i2s')!='1':raise RuntimeError("设备功放未初始化。")
        if st.get('fs')!='1':
            if target.mode!='USB':raise RuntimeError("语音分区未就绪；先用 USB 初始化空白分区。")
            require(f,'PCM INIT','OK PCM ready')
        if state:get_state_tracks(f) # fail early if this firmware does not support state mapping
        done=0
        for s,data in parts:
            try:
                upload(f,s.slot,data,lambda pct:progress((done*100+pct*len(data))//n))
            except (RuntimeError,OSError) as e:
                raise RuntimeError(f"编号 {s.slot:04d} 未保存；此前完成的片段 {done} 字节已留在设备，尚未绑定状态。{e}") from e
            done+=len(data)
        for s,_ in parts:
            if s.state:
                try:set_state_track(f,s.state,s.slot)
                except (RuntimeError,OSError) as e:
                    raise RuntimeError(f"片段已保存，但绑定 {s.state} 时中断。请读取当前绑定后重试。{e}") from e
        progress(100)
        if play:
            try:
                set_gain(f,gain);play_track(f,parts[0][0].slot)
            except (RuntimeError,OSError) as e:
                return f"{len(parts)} 段已保存，但试听未开始：{e}"
        return f"{len(parts)} 段已保存并校验"+(f"，已绑定 {len(state)} 个任务状态" if state else "")+"。"
    return with_link(target,work,connector)
