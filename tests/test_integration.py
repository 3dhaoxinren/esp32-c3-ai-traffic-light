"""Host integration: actual firmware parser with mocked GPIO, flash and I2S."""
from contextlib import contextmanager
from pathlib import Path
import os,socket,socketserver,struct,subprocess,sys,tempfile,threading,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from desktop import device as d

class Engine:
    def __init__(self,binary,cwd):
        self.child=subprocess.Popen([binary],cwd=cwd,stdin=subprocess.PIPE,stdout=subprocess.PIPE)
        self.lock=threading.Lock()
    def command(self,line,prefix=b''):
        with self.lock:
            self.child.stdin.write(prefix+line)
            self.child.stdin.flush()
            out=[]
            while True:
                r=self.child.stdout.readline()
                if not r:raise RuntimeError('firmware harness stopped')
                if r.strip()==b'@END':return b''.join(out)
                out.append(r.replace(b'\r\n',b'\n'))
    def close(self):
        self.child.stdin.close();self.child.wait(timeout=5);self.child.stdout.close()

class SerialMock:
    def __init__(self,b):self.b=b;self.buf=b''
    def write(self,data):self.buf+=self.b.command(data)
    def readline(self,n):
        i=self.buf.find(b'\n')+1
        out=self.buf[:i];self.buf=self.buf[i:];return out

@unittest.skipUnless(os.environ.get('FIRMWARE_HOST_QA'),'set FIRMWARE_HOST_QA to the host test executable')
class TestFirmwareTransport(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.engine=Engine(os.environ['FIRMWARE_HOST_QA'],self.tmp.name)
        self.usb=d.Target('USB','COM_TEST')
        self.tcp=d.Target('WiFi','192.168.10.22')
    def tearDown(self):
        self.engine.close();self.tmp.cleanup()
    @contextmanager
    def usb_open(self,_mode,_address):
        yield SerialMock(self.engine)
    @contextmanager
    def tcp_open(self,_mode,_address):
        engine=self.engine
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                while data:=self.rfile.readline():
                    self.wfile.write(engine.command(data,b'LAN:'))
        with socketserver.ThreadingTCPServer(('127.0.0.1',0),Handler) as server:
            t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
            with socket.create_connection(server.server_address,timeout=2) as s, s.makefile('rb') as f:
                class Link:
                    def write(self,data):s.sendall(data)
                    def readline(self,n):return f.readline(n)
                yield Link()
            server.shutdown()
    def tick(self,word='QA TICK'):
        return self.engine.command((word+'\n').encode()).decode().strip()
    def test_usb_audio_readback_and_status_binding(self):
        base=d.check_device(self.usb,self.usb_open)
        self.assertEqual(base['pcm']['i2s'],'1')
        self.assertEqual(base['pcm']['gain'],'7')
        data=b''.join(struct.pack('<h',i) for i in range(2400))
        cuts=[d.Segment(20,920,42,'RUNNING'),d.Segment(1000,2280,43,'DONE')]
        result=d.push_segments(self.usb,data,cuts,False,7,connector=self.usb_open)
        self.assertIn('已绑定 2',result)
        for s in cuts:
            self.assertEqual(d.stored_audio(self.usb,s.slot,connector=self.usb_open),s.audio(data))
        def talk(f):
            self.assertEqual(d.get_state_tracks(f)['running'],42)
            self.assertEqual(d.get_state_tracks(f)['done'],43)
            d.okay(f,'LIGHT AI RUNNING','OK AI ')
            self.assertIn('plays=1',self.tick())
            d.okay(f,'LIGHT AI RUNNING','OK AI ')
            self.assertIn('plays=1',self.tick()) # regular heartbeat does not replay
            d.okay(f,'LIGHT AI DONE','OK AI ')
            self.assertIn('plays=2',self.tick())
            d.set_state_track(f,'OFFLINE',43)
            self.assertIn('pending=43',self.tick('QA EXPIRE'))
            self.assertIn('plays=3',self.tick())
            self.assertIn('pending=0',self.tick('QA EXPIRE'))
            self.assertIn('plays=3',self.tick())
        d.with_link(self.usb,talk,self.usb_open)
    def test_tcp_audio_push_and_status_through_socket(self):
        d.with_link(self.usb,lambda f:d.require(f,'PCM INIT','OK PCM ready'),self.usb_open)
        data=b'\x35\x01'*1000
        result=d.push_segments(self.tcp,data,[d.Segment(1,871,55,'ERROR')],True,8,connector=self.tcp_open)
        self.assertIn('已绑定 1',result)
        self.assertEqual(d.stored_audio(self.usb,55,connector=self.usb_open),data[2:1742])
        d.with_link(self.tcp,lambda f:d.require(f,'PCM INFO 55','ERR PCM command, token or offset'),self.tcp_open)
        self.assertEqual(d.check_device(self.tcp,self.tcp_open)['pcm']['gain'],'8')
    def test_wifi_provisioning_commands(self):
        # Host build does not join RF; it exercises the real framing and settings parser.
        with self.usb_open(None,None) as f:
            self.assertEqual(d.net_status(f)['state'],'UNCONFIGURED')
            r=d.wifi_provision(f,'中文测试网','ExamplePassword123',timeout=0)
            self.assertEqual(r['state'],'JOINING')
        with self.assertRaises(ValueError):d.encode_wifi('test','short')
        with self.tcp_open(None,None) as f:
            d.require(f,'NET STATUS','ERR NET USB only')

class TestEditing(unittest.TestCase):
    def test_full_source_and_sample_accurate_cuts(self):
        import wave
        data=b''.join(struct.pack('<h',i%30000) for i in range(40*16000))
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'input.wav'
            with wave.open(str(p),'wb') as wf:
                wf.setparams((1,2,16000,0,'NONE','not compressed'));wf.writeframes(data)
            self.assertEqual(d.load_source(p),data)
            self.assertEqual(d.Segment(9600,11200,3,'IDLE').audio(data),data[19200:22400])
            with self.assertRaises(ValueError):d.Segment(0,640000,2).audio(data)
            with self.assertRaises(ValueError):d.Segment(15,14,2).audio(data)
if __name__=='__main__':unittest.main(verbosity=2)
