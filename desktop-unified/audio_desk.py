"""Bounded in-memory audio workspace. No kHz sampling loops run while idle."""
from array import array
from io import BytesIO
from pathlib import Path
import json,os,sys,threading,uuid,wave
from core import DeviceError
import device as d
import audio_bridge as bridge

def peaks(data,bins=560):
    a=array('h');a.frombytes(data)
    if sys.byteorder!='little':a.byteswap()
    step=max(1,(len(a)+bins-1)//bins)
    return [[min(seg),max(seg)] for i in range(0,len(a),step) if (seg:=a[i:i+step])]

def wav_bytes(data):
    f=BytesIO()
    with wave.open(f,'wb') as w:
        w.setparams((1,2,d.RATE,0,'NONE','not compressed'))
        w.writeframes(data)
    return f.getvalue()

class AudioDesk:
    def __init__(self,server):
        self.server=server;self.lock=threading.RLock()
        self.source=None;self.metadata=None
        self.busy=False;self.progress=0;self.result=None;self.error='';self.seq=0
        self.worker=None
    def snapshot(self):
        light=self.server.lights.snapshot()
        with self.lock:
            return dict(busy=self.busy,progress=self.progress,result=self.result,error=self.error,seq=self.seq,source=self.metadata,
                available=bool(light.get('connected') and light.get('max_pcm')),usb=light.get('transport')=='serial',
                linked_busy=bool(light.get('busy')), endpoint=light.get('endpoint',''), state_voice=bool(light.get('state_voice')))
    def progress_at(self,n):
        with self.lock:self.progress=max(0,min(100,int(n)))
    def _data(self,source_id):
        with self.lock:
            if self.source is None or source_id!=self.metadata['id']:raise ValueError('当前音频源已切换，请重新选段。')
            return self.source
    def _save(self,a,name):
        if not a or len(a)>d.MAX_SOURCE or len(a)%2:raise ValueError('音频源为空或超过 5 分钟。')
        m={'id':uuid.uuid4().hex,'name':name[:128],'frames':len(a)//2,'rate':d.RATE,'peaks':peaks(a)}
        with self.lock:self.source,self.metadata=a,m
    @staticmethod
    def get_cut(v):
        if not isinstance(v,dict):raise ValueError('选段格式不正确。')
        st,end,slot=v.get('start'),v.get('end'),v.get('slot',2);state=v.get('state','')
        if not all(type(x) is int for x in (st,end,slot)) or not isinstance(state,str):raise ValueError('选段参数不正确。')
        return d.Segment(st,end,slot,state)
    def start(self,op,data,file=None):
        if op not in {'import','tts','clear','status','wifi','net','read','push','gain','play','stop','count','bind'}:
            raise ValueError('不支持此音频操作。')
        with self.lock:
            if self.busy:raise DeviceError('音频工作区正在忙。')
            self.seq+=1;self.busy=True;self.progress=0;self.result=None;self.error=''
            def job():
                try:result=self.work(op,data,file)
                except (DeviceError,ValueError,RuntimeError,OSError) as e:
                    with self.lock:self.error=str(e) or '设备操作未完成。'
                except Exception:
                    with self.lock:self.error='音频工作区出现异常。'
                else:
                    with self.lock:self.result=result
                finally:
                    # Audio payload, name, and Wi-Fi credentials never enter general logs.
                    if file:
                        try:Path(file).unlink(missing_ok=True)
                        except OSError:pass
                    with self.lock:self.progress=100 if not self.error else 0;self.busy=False
            self.worker=threading.Thread(target=job,name='ailamp-audio',daemon=True)
            self.worker.start()
    def work(self,op,data,file):
        p=self.progress_at
        if op=='import':
            self._save(d.load_source(file),data.get('name','本地音频'))
            return '录音已载入；没有写入设备。'
        if op=='tts':
            text=data.get('text','')
            if not isinstance(text,str):raise ValueError('请输入播报文本。')
            self._save(d.speech(text),'自定义播报')
            return '本地语音已生成。'
        if op=='clear':
            with self.lock:self.source=None;self.metadata=None
            return '音频已清除。'
        if op=='push':
            s=self._data(data.get('source'))
            cuts=data.get('cuts')
            if not isinstance(cuts,list) or not 1<=len(cuts)<=32:raise ValueError('每次推送 1–32 个选段。')
            if type(data.get('play',False)) is not bool:raise ValueError('试播开关格式不正确。')
            if type(data.get('gain',7)) is not int or not 0<=data.get('gain',7)<=9:raise ValueError('音量应为 0–9。')
            return bridge.run(self.server,op,data,p,s,[self.get_cut(v) for v in cuts])
        if op=='wifi':
            if any(not isinstance(data.get(k),str) for k in ('ssid','password')):raise ValueError('Wi-Fi 配置格式无效。')
        if op in ('read','play') and type(data.get('slot')) is not int:raise ValueError('音频编号无效。')
        result=bridge.run(self.server,op,data,p)
        if op=='read':
            self._save(result,f"灯内语音 {data['slot']:04}")
            return '已用 USB 读回，可直接剪辑。'
        return result
    def preview(self,data):
        s=self._data(data.get('source'))
        return wav_bytes(self.get_cut(data).audio(s))
