"""End-to-end: authenticated HTTP, a real TCP stream, and the firmware host parser."""
from pathlib import Path
from contextlib import contextmanager
import io,json,os,socketserver,struct,subprocess,sys,tempfile,threading,time,unittest,urllib.request,urllib.error,wave
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import AppServer
from ai_status import AiCoordinator
from audio_desk import wav_bytes
from device import LIMIT
from device import live_play, usb_peer_for_ip

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
                    if cmd.startswith(b'PCM LIVE '):engine.call(b'QA TICK\n',network=False)
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

    def test_state_clips_inventory_and_ram_audition(self):
        pcm=b''.join(struct.pack('<h',x%32768) for x in range(4800))
        self.request('/api/audio_import',wav_bytes(pcm),{'Content-Type':'application/octet-stream','X-Audio-Name':'qa.wav'})
        source=self.done()['source']['id']
        _,wav=self.request('/api/audio_preview',dict(whole=True,source=source))
        with wave.open(io.BytesIO(wav)) as w:self.assertEqual(w.readframes(4800),pcm)
        # Pre-existing arbitrary recording in the usual DONE slot + old shared low bindings.
        self.request('/api/audio_job',dict(op='push',source=source,cuts=[
            dict(start=0,end=20,slot=2999),dict(start=30,end=80,slot=2,state='WAITING')]))
        self.assertFalse(self.done()['error'])
        self.request('/api/audio_job',dict(op='push',source=source,cuts=[
            dict(start=100,end=220,state='DONE'),dict(start=220,end=360,state='RUNNING')]))
        s=self.done();self.assertFalse(s['error'],s)
        lib=s['library'];self.assertTrue(lib['complete'])
        self.assertEqual(lib['bindings']['waiting'],2)
        self.assertEqual({v['slot']:v['bytes'] for v in lib['tracks']}[2999],40)
        self.assertNotEqual(lib['bindings']['done'],2999)
        cursor=len(self.wire_state['cmd'])
        self.request('/api/audio_job',dict(op='live',source=source,whole=True,gain=9))
        s=self.done();self.assertFalse(s['error'],s);self.assertIn('没有写入 Flash',s['result'])
        self.assertEqual(s['library'],lib)
        self.assertIn(b'PCM LIVE START 9',self.wire_state['cmd'][cursor:])
        self.assertFalse(any(x.startswith((b'PCM INIT',b'PCM START',b'SOUND STATE')) for x in self.wire_state['cmd'][cursor:]))
        self.assertEqual((Path(self.tmp.name)/'wire.bin').read_bytes(),b''.join(
            pcm[i:i+2]*2 for i in range(0,len(pcm),2)))
        self.request('/api/audio_job',dict(op='delete',slot=2,bytes=100,states=['WAITING']))
        self.assertIn('USB',self.done()['error'])
        self.assertNotIn(b'SOUND DELETE 2',self.wire_state['cmd'])

    def test_usb_remove_clears_shared_mapping_and_pages_inventory(self):
        # Exercise the same C3 parser with physical USB semantics in the host harness.
        out=lambda cmd:self.e.call((cmd+'\n').encode(),network=False).decode().strip()
        for slot in range(1,22):
            (Path(self.tmp.name)/f'track{slot:04}.pcm').write_bytes(b'\0\0'*19)
        self.assertIn('next=16',out('PCM LIST 0'))
        self.assertEqual(out('PCM LIST 16'),'OK PCM next=0 entries=17:38,18:38,19:38,20:38,21:38')
        self.assertEqual(out('SOUND STATE DONE 20'),'OK SOUND STATE track=20')
        self.assertEqual(out('SOUND STATE RUNNING 20'),'OK SOUND STATE track=20')
        self.assertIn(b'requires USB',self.e.call(b'SOUND DELETE 20\n'))
        self.assertEqual(out('SOUND DELETE 20'),'OK SOUND deleted=20')
        self.assertFalse((Path(self.tmp.name)/'track0020.pcm').exists())
        self.assertEqual(out('PCM INFO 21'),'OK PCM bytes=38')
        bindings=out('SOUND STATE STATUS')
        self.assertIn('done=0',bindings);self.assertIn('running=0',bindings)

    def test_live_operates_without_flash_mount_and_can_abort(self):
        with tempfile.TemporaryDirectory() as where:
            e=Engine(os.environ['FIRMWARE_HOST_QA'],where)
            try:
                out=lambda cmd:e.call((cmd+'\n').encode()).decode().strip()
                self.assertIn('fs=0',out('PCM STATUS'))
                self.assertEqual(out('PCM GAIN 9'),'OK PCM gain=9')
                key=out('PCM LIVE START').split('=')[1]
                self.assertEqual(out(f'PCM LIVE DATA {key} 0 2100'),'OK PCM stream at=2')
                self.assertEqual(out(f'PCM LIVE ABORT {key}'),'OK PCM stream stopped')
                e.call(b'QA TICK\n',network=False)
                self.assertFalse((Path(where)/'wire.bin').exists())
                key=out('PCM LIVE START').split('=')[1]
                out(f'PCM LIVE DATA {key} 0 3300')
                out(f'PCM LIVE END {key}')
                e.call(b'QA TICK\n',network=False)
                status=out('PCM STATUS')
                self.assertIn('fs=0',status);self.assertIn('last=ended',status)
                self.assertEqual((Path(where)/'wire.bin').read_bytes(),b'\x33\x00'*2)
                self.assertFalse(list(Path(where).glob('*.pcm')))
            finally:e.close()

    def test_clip_to_key_playlist_and_volume_isolation(self):
        pcm=b'\x37\x13'*4096
        self.request('/api/audio_import',wav_bytes(pcm),{'Content-Type':'application/octet-stream','X-Audio-Name':'qa.wav'})
        source=self.done()['source']['id']
        self.request('/api/audio_job',dict(op='gain',gain=5))
        self.assertEqual(self.done()['result']['default_gain'],5)
        self.request('/api/audio_job',dict(op='button_push',source=source,cuts=[dict(start=2,end=42),dict(start=100,end=230)]))
        out=self.done();self.assertFalse(out['error'],out)
        library=out['library'];cfg=library['buttons']
        self.assertEqual(cfg['slots'][:3],[2,3,4])
        take=cfg['slots'][3:]
        self.assertEqual(len(take),2)
        self.assertEqual([r['bytes'] for r in library['tracks']],[260,80])
        self.request('/api/audio_job',dict(op='buttons',mode='ALL',slots=take,expected=cfg))
        self.assertEqual(self.done()['library']['buttons'],dict(mode='ALL',slots=take))
        self.request('/api/audio_job',dict(op='play',slot=take[0],gain=1))
        self.assertFalse(self.done()['error'])
        self.assertIn(b'PCM PLAY 2936 1',self.wire_state['cmd'])
        self.request('/api/audio_job',dict(op='status'))
        state=self.done()['result']
        self.assertEqual(state['amp']['gain'],'5')
        self.assertEqual(state['amp']['default_gain'],'5')

    def test_direct_speech_uses_lan_without_replacing_draft_or_state(self):
        pcm=b'\x37\x13'*2000
        self.request('/api/audio_import',wav_bytes(pcm),{'Content-Type':'application/octet-stream','X-Audio-Name':'draft.wav'})
        src=self.done()['source']
        st_before=self.e.call(b'SOUND STATE STATUS\n')
        tracks_before=self.e.call(b'PCM LIST 0\n')
        self.request('/api/audio_job',dict(op='gain',gain=5))
        self.assertFalse(self.done()['error'])
        mark=len(self.wire_state['cmd'])
        with patch('audio_desk.synthesize',return_value=(pcm,'QA profile')) as synth:
            self.request('/api/audio_job',dict(op='speak',text='测试',profile='xiaoxiao',rate=-8))
            done=self.done()
            synth.assert_called_once_with('测试','xiaoxiao',-8)
        self.assertFalse(done['error'],done)
        self.assertIn('直接播报结束',done['result'])
        self.assertEqual(done['source'],src)
        commands=self.wire_state['cmd'][mark:]
        self.assertIn(b'PCM LIVE START 5',commands)
        self.assertFalse(any(x.startswith((b'PCM START',b'LIGHT ',b'SOUND STATE ',b'PCM GAIN')) for x in commands))
        self.assertEqual(self.e.call(b'SOUND STATE STATUS\n'),st_before)
        self.assertEqual(self.e.call(b'PCM LIST 0\n'),tracks_before)

    def test_text_audition_works_without_c3_and_leaves_workspace_untouched(self):
        pcm=b'\x37\x13'*900
        self.request('/api/audio_import',wav_bytes(pcm),{'Content-Type':'application/octet-stream','X-Audio-Name':'draft.wav'})
        self.server.lights._set(max_pcm=False)
        before=self.done()
        self.assertFalse(before['available'])
        wire_commands=len(self.wire_state['cmd'])
        args=dict(text='试听测试',profile='xiaoxiao',rate=8)
        with patch('audio_desk.synthesize',return_value=(pcm,'QA profile')) as synth:
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.request('/api/audio_voice_preview',args,auth=False)
            self.assertEqual(err.exception.code,403)
            status,wav=self.request('/api/audio_voice_preview',args)
            synth.assert_called_once_with('试听测试','xiaoxiao',8)
        self.assertEqual(status,200)
        with wave.open(io.BytesIO(wav)) as w:
            self.assertEqual(w.getframerate(),16000)
            self.assertEqual(w.readframes(w.getnframes()),pcm)
        self.assertEqual(len(self.wire_state['cmd']),wire_commands)
        self.assertEqual(before,self.done())
        with self.server.audio.voice_lock:
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.request('/api/audio_voice_preview',args)
            self.assertEqual(err.exception.code,409)

    def test_saved_recording_can_be_auditioned_over_its_matching_usb_while_lan_stays_connected(self):
        pcm=b''.join(struct.pack('<h',x) for x in range(2400))
        self.request('/api/audio_import',wav_bytes(pcm),{'Content-Type':'application/octet-stream','X-Audio-Name':'draft.wav'})
        source=self.done()['source']['id']
        self.request('/api/audio_job',dict(op='push',source=source,cuts=[dict(start=15,end=400,slot=4,state='WAITING')]))
        self.assertFalse(self.done()['error'])
        before=self.done(); lan_commands=self.wire_state['cmd'][:]; usb_commands=[]
        engine=self.e
        class Wire:
            def write(self,raw):
                usb_commands.append(raw.strip())
                self.reply=engine.call(raw,network=False)
            def readline(self,_):
                r,_,self.reply=self.reply.partition(b'\n')
                return r+b'\n'
        @contextmanager
        def side(ip):
            self.assertEqual(ip,'127.0.0.1')
            yield Wire()
        args=dict(slot=4,bytes=770,endpoint=before['endpoint'])
        with patch('device.usb_peer_for_ip',side):
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.request('/api/audio_recording_preview',{**args,'endpoint':'not-this-lamp'})
            self.assertEqual(err.exception.code,409)
            self.assertEqual(usb_commands,[])
            with self.assertRaises(urllib.error.HTTPError) as err:
                self.request('/api/audio_recording_preview',{**args,'bytes':768})
            self.assertEqual(err.exception.code,409)
            self.assertEqual(usb_commands,[b'PCM INFO 4'])
            status,data=self.request('/api/audio_recording_preview',args)
        with wave.open(io.BytesIO(data)) as wav:
            self.assertEqual(wav.readframes(wav.getnframes()),pcm[30:800])
        self.assertEqual(status,200)
        self.assertTrue(any(q.startswith(b'PCM READ 4 ') for q in usb_commands))
        self.assertFalse(any(q.startswith(b'PCM PLAY ') for q in usb_commands))
        self.assertEqual(self.wire_state['cmd'],lan_commands)
        self.assertEqual(self.done(),before)
        self.assertEqual(self.wire_state['connections'],1)

    def test_paired_lan_recording_never_opens_usb(self):
        self.server.lights._set(pcm_features=['LAN_READ'])
        self.server.audio._save(b'\0\0'*44,'keep.wav')
        before=self.done()
        audio=b'\x13\x34'*20
        with (patch('audio_bridge.lan_readback.read',return_value=audio) as lan,
              patch('device.usb_peer_for_ip',side_effect=AssertionError('must not open USB'))):
            status,wav=self.request('/api/audio_recording_preview',dict(slot=1,bytes=40,endpoint=before['endpoint']))
            self.assertEqual(lan.call_args.args[:3],(self.peer.server_address,1,40))
        self.assertEqual(status,200)
        with wave.open(io.BytesIO(wav)) as w:self.assertEqual(w.readframes(20),audio)
        self.assertEqual(self.done(),before)

    def test_hardware_key_selection_and_reboot_persistence(self):
        with tempfile.TemporaryDirectory() as where:
            e=Engine(os.environ['FIRMWARE_HOST_QA'],where)
            try:
                out=lambda cmd:e.call((cmd+'\n').encode(),network=False).decode().strip()
                self.assertEqual(out('PCM INIT'),'OK PCM ready')
                for n in [1,17,2936]:(Path(where)/f'track{n:04}.pcm').write_bytes(b'\x40\x20'*30)
                self.assertEqual(out('PCM GAIN SAVE 3'),'OK PCM default_gain=3')
                self.assertEqual(out('PCM GAIN 9'),'OK PCM gain=9')
                self.assertEqual(out('SOUND BUTTONS SET ALL 17,2936'), 'OK SOUND BUTTONS mode=ALL tracks=17,2936')
            finally:e.close()
            e=Engine(os.environ['FIRMWARE_HOST_QA'],where)
            try:
                out=lambda cmd:e.call((cmd+'\n').encode(),network=False).decode().strip()
                self.assertIn('gain=3 default_gain=3',out('PCM STATUS'))
                self.assertEqual(out('SOUND BUTTONS STATUS'),'OK SOUND BUTTONS mode=ALL tracks=17,2936')
                import re
                hit=lambda:int(re.search(r'last=(\d+)',out('QA BUTTON'))[1])
                seq=[hit() for _ in range(24)]
                self.assertEqual(set(seq),{1,17,2936})
                self.assertTrue(all(a!=b for a,b in zip(seq,seq[1:])))
                self.assertEqual(out('SOUND BUTTONS SET CUSTOM 17,2936,2000'),'OK SOUND BUTTONS mode=CUSTOM tracks=17,2936,2000')
                seq=[hit() for _ in range(8)]
                self.assertEqual(set(seq),{17,2936})
                self.assertTrue(all(a!=b for a,b in zip(seq,seq[1:])))
                self.assertEqual(out('SOUND DELETE 17'),'OK SOUND deleted=17')
                self.assertEqual(out('SOUND BUTTONS STATUS'),'OK SOUND BUTTONS mode=CUSTOM tracks=2936,2000')
                self.assertEqual([hit() for _ in range(3)],[2936]*3)
            finally:e.close()

class StateRoute(unittest.TestCase):
    def test_usb_side_channel_only_returns_a_unique_online_ip_match(self):
        from types import SimpleNamespace
        closed=[]
        @contextmanager
        def conn(mode,address):
            self.assertEqual(mode,'USB')
            try:yield SimpleNamespace(name=address)
            finally:closed.append(address)
        net={'COM19':dict(state='ONLINE',ip='192.168.1.20'),'COM20':dict(state='ONLINE',ip='192.168.1.21')}
        with (patch('device.ports',return_value=list(net)),
              patch('device.connection',conn),patch('device.identify'),
              patch('device.net_status',side_effect=lambda f:net[f.name])):
            with usb_peer_for_ip('192.168.1.20') as f:
                self.assertEqual(f.name,'COM19')
                self.assertEqual(closed,['COM20'])
            self.assertEqual(closed,['COM20','COM19'])
            net['COM20']=net['COM19']
            with self.assertRaisesRegex(ValueError,'没有找到'):
                with usb_peer_for_ip('192.168.1.20'):self.fail('ambiguous device')

    def test_stream_window_recovers_from_ring_backpressure(self):
        from collections import deque
        class Wire:
            def __init__(self):
                self.data=bytearray();self.acks=deque();self.batches=0
            def write(self,raw):
                lines=raw.decode().splitlines()
                if len(lines)>1:self.batches+=1
                for line in lines:
                    if line=='PCM LIVE START 0':r='OK PCM stream=12345678'
                    elif line.startswith('PCM LIVE DATA '):
                        _,_,_,_,pos,hex_data=line.split()
                        off=int(pos)
                        if off!=len(self.data):r='ERR PCM command, token or offset'
                        elif off==256 and self.batches==1:r='OK PCM stream wait'
                        else:
                            self.data.extend(bytes.fromhex(hex_data))
                            r=f'OK PCM stream at={len(self.data)}'
                    elif line=='PCM LIVE END 12345678':r='OK PCM stream draining'
                    elif line=='PCM STATUS':r='OK PCM last=ended'
                    else:raise AssertionError(line)
                    self.acks.append((r+'\n').encode())
            def readline(self,_):return self.acks.popleft()
        wire=Wire();pcm=bytes(range(256))*5
        self.assertIn('试听结束',live_play(wire,pcm,0,lambda _:None,threading.Event(),scoped_gain=True,window=4))
        self.assertEqual(wire.data,pcm)
        self.assertFalse(wire.acks)

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
