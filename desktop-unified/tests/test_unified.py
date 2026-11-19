"""End-to-end: authenticated HTTP, a real TCP stream, and the firmware host parser."""
from pathlib import Path
import io,json,os,socketserver,struct,subprocess,sys,tempfile,threading,time,unittest,urllib.request,urllib.error,wave
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import AppServer
from ai_status import AiCoordinator
from audio_desk import wav_bytes
from device import LIMIT

def wait_for(fn,timeout=8):
    deadline=time.monotonic()+timeout
    while not fn():
        if time.monotonic()>deadline:raise TimeoutError('condition did not appear')
        time.sleep(.02)

class Engine:
    def __init__(self,p,dir):
        self.c=subprocess.Popen([p],cwd=dir,stdin=subprocess.PIPE,stdout=subprocess.PIPE)
        self.lock=threading.Lock()
    def call(self,cmd,network=True):
        with self.lock:
            self.c.stdin.write((b'LAN:' if network else b'')+cmd)
            self.c.stdin.flush()
            replies=[]
            for line in iter(self.c.stdout.readline,b''):
                if line.strip()==b'@END':return b''.join(replies)
                replies.append(line.replace(b'\r\n',b'\n'))
            raise RuntimeError('simulator ended')
    def close(self):
        self.c.stdin.close();self.c.wait(timeout=5);self.c.stdout.close()

@unittest.skipUnless(os.environ.get('FIRMWARE_HOST_QA'),'set FIRMWARE_HOST_QA')
class Unified(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.env=patch.dict(os.environ,LOCALAPPDATA=self.tmp.name);self.env.start();self.addCleanup(self.env.stop)
        self.e=Engine(os.environ['FIRMWARE_HOST_QA'],self.tmp.name);self.addCleanup(self.e.close)
        self.assertIn(b'OK PCM ready',self.e.call(b'PCM INIT\n',network=False))
        self.wire_state={'connections':0,'cmd':[]}
        engine=self.e
        ws=self.wire_state
        class Conn(socketserver.StreamRequestHandler):
            def handle(self):
                ws['connections']+=1
                while cmd:=self.rfile.readline():
                    ws['cmd'].append(cmd.strip())
                    self.wfile.write(engine.call(cmd))
        self.peer=socketserver.ThreadingTCPServer(('127.0.0.1',0),Conn)
        self.addCleanup(self.peer.server_close);self.addCleanup(self.peer.shutdown)
        threading.Thread(target=self.peer.serve_forever,daemon=True).start()
        self.server=AppServer(demo=False)
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.lights.shutdown);self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with patch('light_control.validate_host',lambda x:x):
            self.server.lights.start('light_connect',{'transport':'tcp','host':'127.0.0.1','tcp_port':self.peer.server_address[1]})
            wait_for(lambda:not self.server.lights.snapshot()['busy'])
        assert self.server.lights.snapshot()['connected']
        assert self.server.lights.snapshot()['state_voice']
    def request(self,path,body=None,h=None,auth=True):
        headers=dict(h or {})
        if auth:headers['X-Session-Token']=self.server.token
        if isinstance(body,dict):
            body=json.dumps(body).encode()
            headers['Content-Type']='application/json'
        req=urllib.request.Request(self.server.origin+path,data=body,headers=headers)
        with self.client.open(req,timeout=6) as r:return r.status,r.read()
    def done(self):
        wait_for(lambda:not self.server.audio.snapshot()['busy'])
        return self.server.audio.snapshot()
    def test_integrated_audio_and_single_connection(self):
        with self.assertRaises(urllib.error.HTTPError) as err:self.request('/api/audio',auth=False)
        self.assertEqual(err.exception.code,403)
        pcm=b''.join(struct.pack('<h',x) for x in range(2400))
        self.assertEqual(self.request('/api/audio_import',wav_bytes(pcm),{'Content-Type':'application/octet-stream','X-Audio-Name':'qa.wav'})[0],202)
        state=self.done();self.assertFalse(state['error']);source=state['source']['id']
        first=dict(start=10,end=430,slot=46,state='RUNNING')
        second=dict(start=600,end=1730,slot=47,state='DONE')
        _,wav=self.request('/api/audio_preview',dict(first,source=source))
        with wave.open(io.BytesIO(wav)) as w:self.assertEqual(w.readframes(420),pcm[20:860])
        self.assertEqual(self.request('/api/audio_job',dict(op='push',source=source,cuts=[first,second],gain=7,play=False))[0],202)
        st=self.done();self.assertFalse(st['error'],st)
        self.assertEqual(self.wire_state['connections'],1)
        self.assertIn(b'SOUND STATE DONE 47',self.wire_state['cmd'])
        self.assertEqual(self.request('/api/audio_job',dict(op='read',slot=46))[0],202)
        self.assertIn('USB',self.done()['error'])
        self.assertEqual(self.request('/api/audio_job',dict(op='status'))[0],202)
        self.assertEqual(self.done()['result']['bindings']['done'],47)
        self.server.lights.start('light_send',{'command':'GREEN'})
        wait_for(lambda:not self.server.lights.snapshot()['busy'])
        self.assertEqual(self.server.lights.snapshot()['color'],'GREEN')
        self.assertEqual(self.wire_state['connections'],1)
    def test_nano_audio_interlock(self):
        self.server.lights._set(max_pcm=False)
        self.request('/api/audio_job',dict(op='gain',gain=9))
        self.assertIn('Nano',self.done()['error'])
        self.assertNotIn(b'PCM GAIN 9',self.wire_state['cmd'])

class StateRoute(unittest.TestCase):
    def coordinator(self):
        class Stub:
            calls=[]
            def snapshot(self):return dict(connected=True,busy=False,transport='tcp',sound_v2=True,sound_ready_at=0,screen_supported=False,state_voice=True)
            def start(self,op,data):self.calls.append((op,data))
        light=Stub();light.calls=[]
        ai=AiCoordinator(light,threading.RLock(),lambda *a:None,demo=True)
        ai.set_enabled(True);ai.start();self.addCleanup(ai.shutdown)
        return ai,light
    def test_completion_once_and_idle_not_replayed(self):
        ai,light=self.coordinator()
        event=lambda state:ai.registry.event(dict(source='QA',session='one',state=state,ttl=90))
        event('running')
        wait_for(lambda:any(x[1].get('state')=='RUNNING' for x in light.calls))
        event('idle')
        wait_for(lambda:any(x[1].get('state')=='DONE' for x in light.calls))
        time.sleep(.45)
        self.assertEqual(sum(c[1].get('state')=='DONE' for c in light.calls),1)
        self.assertFalse(any(c[0]=='light_sound' or c[1].get('state')=='IDLE' for c in light.calls))
    def test_parallel_completion_does_not_override_running(self):
        ai,light=self.coordinator()
        event=lambda id,state:ai.registry.event(dict(source='QA',session=id,state=state,ttl=90))
        event('one','running');event('two','running')
        wait_for(lambda:any(x[1].get('state')=='RUNNING' for x in light.calls))
        event('one','idle')
        wait_for(lambda:any(x[1].get('op')=='state_finish' for x in light.calls))
        self.assertFalse(any(x[1].get('state')=='DONE' for x in light.calls))

if __name__=='__main__':unittest.main(verbosity=2)
