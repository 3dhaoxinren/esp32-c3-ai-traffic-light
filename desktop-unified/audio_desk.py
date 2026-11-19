"""Bounded in-memory audio workspace. No kHz sampling loops run while idle."""
from array import array
from io import BytesIO
from pathlib import Path
import json,os,sys,threading,uuid,wave
from core import DeviceError
import device as d
import audio_bridge as bridge
from voice_pack import synthesize

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
        self.server=server;self.lock=threading.RLock();self.voice_lock=threading.Lock()
        self.source=None;self.metadata=None
        self.busy=False;self.progress=0;self.result=None;self.error='';self.seq=0
        self.readback=False;self.readback_progress=0
        self.worker=None
        self.cancelled=threading.Event();self.op='';self.library=None
    def snapshot(self):
        light=self.server.lights.snapshot()
        with self.lock:
            connected=bool(light.get('connected') and light.get('max_pcm'))
            lib=self.library if connected and self.library and self.library['endpoint']==light['endpoint'] else None
            return dict(busy=self.busy or self.readback,progress=self.readback_progress if self.readback else self.progress,
                result=self.result,error=self.error,seq=self.seq,source=self.metadata,
                available=bool(light.get('connected') and light.get('max_pcm')),usb=light.get('transport')=='serial',
                linked_busy=bool(light.get('busy')), endpoint=light.get('endpoint',''), state_voice=bool(light.get('state_voice')),
                features=light.get('pcm_features',[]),library=lib,live=self.busy and self.op in ('live','speak'))
    def cancel_live(self):
        with self.lock:
            if self.busy and self.op in ('live','speak'):self.cancelled.set()
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
    def _voice(self,data):
        if not self.voice_lock.acquire(blocking=False):
            raise DeviceError('正在生成另一段语音，请稍后再试。')
        try:
            return synthesize(data.get('text',''),data.get('profile','local'),data.get('rate',0))
        finally:
            self.voice_lock.release()
    def preview_voice(self,data):
        # Never attach the preview to the workspace or reserve the C3 connection.
        try:
            pcm,_=self._voice(data)
        except (RuntimeError,OSError) as exc:
            raise DeviceError(str(exc)) from exc
        return wav_bytes(pcm)
    def preview_recording(self,data):
        n,size,endpoint=data.get('slot'),data.get('bytes'),data.get('endpoint')
        if (type(n) is not int or not 1<=n<=3000 or type(size) is not int or
            not 0<size<=d.LIMIT or size%2 or not isinstance(endpoint,str)):
            raise ValueError('录音编号或大小无效；请刷新列表。')
        with self.lock:
            if self.busy or self.readback:raise DeviceError('音频工作区正在忙，请稍后再试。')
            self.readback=True;self.readback_progress=0
        def progress(p):
            with self.lock:self.readback_progress=p
        try:
            pcm,mode=bridge.preview_recording(self.server,endpoint,n,size,progress)
            return wav_bytes(pcm),mode
        except (OSError,RuntimeError) as e:
            raise DeviceError(str(e)) from e
        finally:
            with self.lock:self.readback=False;self.readback_progress=0
    @staticmethod
    def get_cut(v):
        if not isinstance(v,dict):raise ValueError('选段格式不正确。')
        st,end,slot=v.get('start'),v.get('end'),v.get('slot');state=v.get('state','')
        if 'slot' not in v:slot=0 if state else 2
        if not all(type(x) is int for x in (st,end,slot)) or not isinstance(state,str):raise ValueError('选段参数不正确。')
        return d.Segment(st,end,slot,state)
    def start(self,op,data,file=None):
        if op not in {'import','tts','speak','clear','status','wifi','net','read','push','gain','play','stop','count','bind','live','library','delete','buttons','button_push','lan_read_pair'}:
            raise ValueError('不支持此音频操作。')
        with self.lock:
            if self.busy or self.readback:raise DeviceError('音频工作区正在忙。')
            self.seq+=1;self.busy=True;self.progress=0;self.result=None;self.error='';self.op=op;self.cancelled.clear()
            def job():
                try:result=self.work(op,data,file)
                except (DeviceError,ValueError,RuntimeError,OSError) as e:
                    with self.lock:self.error=str(e) or '设备操作未完成。'
                except Exception:
                    with self.lock:self.error='音频工作区出现异常。'
                else:
                    with self.lock:
                        self.result=result
                        if isinstance(result,dict):self._update_library(result)
                finally:
                    # Audio payload, name, and Wi-Fi credentials never enter general logs.
                    if file:
                        try:Path(file).unlink(missing_ok=True)
                        except OSError:pass
                    with self.lock:self.progress=100 if not self.error else 0;self.busy=False
            self.worker=threading.Thread(target=job,name='ailamp-audio',daemon=True)
            self.worker.start()
    def _update_library(self,result):
        endpoint=result.get('library_endpoint')
        old=self.library
        if endpoint:
            same=old and old['endpoint']==endpoint
            rows={x['slot']:x for x in (old['tracks'] if same else [])}
            if 'tracks' in result:rows={x['slot']:x for x in result['tracks']}
            rows.update({x['slot']:x for x in result.get('saved_tracks',[])})
            rows.pop(result.get('deleted_slot'),None)
            self.library={'endpoint':endpoint,'tracks':sorted(rows.values(),key=lambda x:x['slot']),
                          'bindings':result.get('bindings') or (old['bindings'] if same else {}),
                          'buttons':result.get('buttons') or (old.get('buttons') if same else None),
                          'complete':bool(result.get('complete'))}
        elif old:
            self.library={**old,**{k:result[k] for k in ('bindings','buttons') if result.get(k) is not None}}
    def work(self,op,data,file):
        p=self.progress_at
        if op=='lan_read_pair':
            return {'message':bridge.pair_recording_readback(self.server),'lan_read_paired':True}
        if op=='import':
            self._save(d.load_source(file),data.get('name','本地音频'))
            return '录音已载入；没有写入设备。'
        if op in ('tts','speak'):
            text=data.get('text','')
            if not isinstance(text,str):raise ValueError('请输入播报文本。')
            if op=='speak':
                link=self.server.lights.snapshot()
                if not link.get('connected') or 'LIVE' not in link.get('pcm_features',()):
                    raise DeviceError('请先通过局域网或原生 USB 连接支持实时播报的 C3。')
            pcm,voice=self._voice(data)
            if op=='speak':
                if self.cancelled.is_set():return '已取消直接播报；没有写入设备。'
                return bridge.run(self.server,op,{**data,'speech_target':(link['transport'],link['endpoint'])},p,pcm,cancel=self.cancelled)
            self._save(pcm,'自定义播报 · '+voice)
            return '本地语音已生成。'
        if op=='clear':
            with self.lock:self.source=None;self.metadata=None
            return '音频已清除。'
        if op in ('push','button_push'):
            s=self._data(data.get('source'))
            cuts=data.get('cuts')
            if not isinstance(cuts,list) or not 1<=len(cuts)<=32:raise ValueError('每次推送 1–32 个选段。')
            if type(data.get('play',False)) is not bool:raise ValueError('试播开关格式不正确。')
            if type(data.get('gain',7)) is not int or not 0<=data.get('gain',7)<=9:raise ValueError('音量应为 0–9。')
            if op=='button_push' and any(not isinstance(x,dict) or 'state' in x or 'slot' in x for x in cuts):
                raise ValueError('按键选段只需时间范围。')
            return bridge.run(self.server,op,data,p,s,[self.get_cut(v) for v in cuts])
        if op=='live':
            s=self._data(data.get('source'))
            if type(data.get('gain',7)) is not int or not 0<=data.get('gain',7)<=9:raise ValueError('音量应为 0–9。')
            if data.get('whole') is not True:s=self.get_cut(data).audio(s)
            return bridge.run(self.server,op,data,p,s,cancel=self.cancelled)
        if op=='delete':
            if (type(data.get('slot')) is not int or not 1<=data['slot']<=3000 or
                type(data.get('bytes')) is not int or not 1<=data['bytes']<=d.LIMIT or
                not isinstance(data.get('states'),list) or len(data['states'])>6 or
                any(not isinstance(k,str) or k not in dict(d.STATES) for k in data['states'])):
                raise ValueError('删除参数不正确；请刷新列表。')
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
        if data.get('whole') is True:return wav_bytes(s)
        return wav_bytes(self.get_cut(data).audio(s))
