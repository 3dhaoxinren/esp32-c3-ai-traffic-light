import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request

from core import Controller, DemoSerial, DeviceError, Timing, UDPTransport, validate_wifi, validate_host
from server import AppServer


class FakeSerial(DemoSerial):
    def __init__(self, failure=None, version=None, chunk=4096, stale=False):
        super().__init__()
        self.writes = []
        self.failure = failure
        self.version = version
        self.chunk = chunk
        self.stale = stale

    def write(self, payload):
        self.writes.append(payload)
        if self.failure and self.failure.encode() in payload:
            self.buffer.extend(b'error\r\n')
            return len(payload)
        if self.version is not None and payload == b'at+ver=?\r':
            self.buffer.extend(self.version)
            return len(payload)
        if self.stale and payload == b'at+ver=?\r':
            self.buffer.extend(b'ok\r\n')
            return len(payload)
        return super().write(payload)

    def read(self, size=1):
        return super().read(min(size, self.chunk))


class CoreTests(unittest.TestCase):
    def controller(self, port=None, **timing):
        port = port or FakeSerial()
        c = Controller(demo=True, factory=lambda: port,
                       timing=Timing(**dict(reply=0.2, quiet=0.012, pmk=0.015, reboot=0.015, **timing)))
        return c, port

    def run_job(self, c, action, data=None):
        c.start(action, data)
        c.worker.join(4)
        self.assertFalse(c.snapshot()['busy'])
        return c.snapshot()

    def connect(self, c):
        return self.run_job(c, 'connect', dict(port='DEMO', baud=115200))

    def test_config_realistic_echo_fragmentation_and_cr(self):
        c, p = self.controller(FakeSerial(chunk=3))
        self.assertTrue(self.connect(c)['at_ready'])
        s = self.run_job(c,'configure',dict(ssid='AILamp_Demo',password='example987',auth=9))
        self.assertEqual(s['result'],'success')
        self.assertTrue(s['at_ready'])
        self.assertNotIn('example987',json.dumps(s))
        self.assertNotIn('at+WA=0\r', [x.decode() for x in p.writes])
        self.assertNotIn('at+WM=2\r', [x.decode() for x in p.writes])
        self.assertIn(b'at+Sssidl=11\r',p.writes)
        self.assertIn(b'at+Spwl=10\r',p.writes)
        reboot=p.writes.index(b'at+Rb=1\r')
        self.assertEqual(p.writes[reboot-1],b'at+WC=1\r')
        self.assertIn(b'at+ver=?\r',p.writes[reboot+1:])
        for command in p.writes:
            self.assertTrue(command.endswith(b'\r'))
            self.assertEqual(command.count(b'\r'),1)
            self.assertNotIn(b'\n',command)

    def test_error_stops_without_reboot(self):
        c,p=self.controller(FakeSerial(failure='Sam='))
        self.connect(c)
        s=self.run_job(c,'configure',dict(ssid='Test',password='example987',auth=7))
        self.assertEqual(s['result'],'error')
        self.assertFalse(any(b'Spw=' in b or b'Rb=' in b for b in p.writes))

    def test_wrong_firmware_is_never_ready(self):
        c,p=self.controller(FakeSerial(version=b'ESP8266 AT v1\r\n'))
        self.assertFalse(self.connect(c)['at_ready'])
        with self.assertRaises(DeviceError):
            c.start('configure',dict(ssid='Test',password='example987',auth=9))
        self.assertEqual(p.writes,[b'at+ver=?\r'])

    def test_ok_alone_is_not_version_or_query_value(self):
        c,p=self.controller(FakeSerial(stale=True))
        s=self.connect(c)
        self.assertFalse(s['at_ready'])
        self.assertEqual(s['result'],'error')

    def test_validation_blocks_line_injection_before_writes(self):
        for key,bad in [('ssid','bad\rat+Df=1'),('password','pass\nat+Df=1'),('password','short'),('ssid','x'*33)]:
            values=dict(ssid='Test',password='example987',auth=9); values[key]=bad
            with self.assertRaises(DeviceError):validate_wifi(values)

    def test_read_excludes_password_queries(self):
        c,p=self.controller();self.connect(c)
        s=self.run_job(c,'read')
        self.assertEqual(s['config']['Sssid'],'AILamp_Demo')
        self.assertFalse(any(b'Spw' in x for x in p.writes))

    def test_mode_changes_only_when_needed(self):
        c,p=self.controller();p.values['WA']='1';p.values['WM']='0';self.connect(c)
        self.run_job(c,'configure',dict(ssid='Test',password='example987',auth=7))
        self.assertIn(b'at+WA=0\r',p.writes);self.assertIn(b'at+WM=2\r',p.writes)

    def test_cancel_wait_prevents_reboot_and_parallel_jobs(self):
        c,p=self.controller();c.timing=Timing(0.2,0.012,1,0.01);self.connect(c)
        c.start('configure',dict(ssid='Test',password='example987',auth=9))
        with self.assertRaises(DeviceError):c.start('detect')
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and b'at+WC=1\r' not in p.writes:time.sleep(0.005)
        c.cancel();c.worker.join(2)
        self.assertEqual(c.snapshot()['result'],'cancelled')
        self.assertNotIn(b'at+Rb=1\r',p.writes)

    def test_transparent_marks_at_unknown(self):
        c,p=self.controller();self.connect(c)
        s=self.run_job(c,'transparent')
        self.assertFalse(s['at_ready']);self.assertEqual(p.writes[-1],b'at+TS=1\r')

    def test_disconnect_closes_port_during_query(self):
        c,p=self.controller(FakeSerial(version=b''))
        c.start('connect',dict(port='DEMO',baud=115200));time.sleep(0.02)
        c.cancel(disconnect=True);c.worker.join(2)
        self.assertFalse(c.snapshot()['connected']);self.assertFalse(p.is_open)

    def test_remote_audit_is_readonly_and_keeps_uart_unknown(self):
        c,p=self.controller()
        self.run_job(c,'connect',dict(transport='udp',host='192.168.1.100'))
        s=self.run_job(c,'audit')
        self.assertEqual(s['transport'],'udp')
        self.assertEqual(s['config']['DE'],'1')
        self.assertTrue(all(command.endswith(b'=?\r') for command in p.writes))

    def test_udp_host_validation(self):
        self.assertEqual(validate_host('192.168.1.100'),'192.168.1.100')
        for invalid in ['example.com','8.8.8.8','127.0.0.1','224.0.0.1','192.168.1.100\rat+Df=1']:
            with self.assertRaises(DeviceError):validate_host(invalid)

    def test_udp_framing_and_unterminated_datagram_reply(self):
        receiver=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        receiver.bind(('127.0.0.1',0));receiver.settimeout(2)
        transport=UDPTransport('127.0.0.1',receiver.getsockname()[1])
        try:
            self.assertEqual(transport.write(b'at+ver=?\r'),9)
            payload,peer=receiver.recvfrom(1024)
            self.assertEqual(payload,b'hlkATat+ver=?\r')
            receiver.sendto(b'HLK-M30(V3.18(Dec  9 2015))',peer)
            c=Controller(demo=True,timing=Timing(0.4,0.02,0.01,0.01));c.port=transport
            self.assertEqual(c._receive('at+ver=?'),'HLK-M30(V3.18(Dec  9 2015))')
        finally:transport.close();receiver.close()

    def test_empty_ssid_is_unconfigured_not_lost_at(self):
        c,p=self.controller();p.values.update(Sssidl='0',Sssid='',WM='0')
        s=self.connect(c)
        self.assertTrue(s['at_ready'])
        self.assertEqual(s['result'],'success')
        self.assertEqual(s['device']['wifi'],'unconfigured')
        self.assertEqual(s['config']['Sssid'],'')
        self.assertNotIn(b'at+Sssid=?\r',p.writes)
        self.assertTrue(s['upgrade_supported'])

    def test_one_failed_query_keeps_verified_at_and_other_fields(self):
        c,p=self.controller(FakeSerial(failure='Sam=?'))
        s=self.connect(c)
        self.assertTrue(s['at_ready'])
        self.assertEqual(s['device']['mac'],'3C:33:00:0C:22:80')
        self.assertIn('加密方式未读取到',s['warnings'])

    def test_configured_ip_does_not_claim_wifi_connected(self):
        c,p=self.controller();s=self.connect(c)
        self.assertEqual(s['device']['ip'],'192.168.1.100')
        self.assertEqual(s['device']['wifi'],'unverified')

    def test_unpatched_firmware_cannot_enter_recovery(self):
        c,p=self.controller();p.values['UP']='0';self.connect(c)
        s=self.run_job(c,'upgrade')
        self.assertEqual(s['result'],'error')
        self.assertNotIn(b'at+UP=1\r',p.writes)

    def test_transparent_auto_status_sends_no_at_to_network(self):
        c,p=self.controller();self.connect(c);self.run_job(c,'transparent')
        previous=len(p.writes);self.run_job(c,'status',{'quiet':True})
        self.assertEqual(len(p.writes),previous)

    def test_nano_preparation_verifies_9600(self):
        c,p=self.controller();self.connect(c)
        s=self.run_job(c,'nano_baud')
        self.assertEqual(s['result'],'success')
        self.assertEqual(s['baud'],9600)
        self.assertEqual(p.baudrate,9600)
        self.assertEqual(s['device']['module_baud'],'9600')

    def test_upgrade_round_trip_sends_restart_only(self):
        c,p=self.controller();self.connect(c)
        updated=c.snapshot()['status_updated']
        s=self.run_job(c,'upgrade')
        self.assertEqual(s['mode'],'recovery')
        self.assertFalse(s['at_ready'])
        self.assertEqual(s['status_updated'],updated)
        start=len(p.writes)
        s=self.run_job(c,'return_at')
        self.assertEqual(s['result'],'success')
        self.assertEqual(s['mode'],'at')
        writes=p.writes[start:]
        handshake=writes.index(b'\xaa')
        packet=b''.join(writes[handshake+1:handshake+34])
        self.assertEqual(len(packet),4105)
        self.assertEqual(packet[:2],b'\x21\xfe')
        self.assertFalse(any(packet[2:-1]))
        self.assertEqual(packet[-1],0xdf)

    def test_connect_recognizes_already_running_recovery(self):
        c,p=self.controller();p.mode='recovery'
        s=self.connect(c)
        self.assertEqual(s['result'],'success')
        self.assertEqual(s['mode'],'recovery')
        self.assertFalse(s['at_ready'])
        self.assertEqual(s['status_updated'],0)
        self.assertTrue(self.run_job(c,'return_at')['at_ready'])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.server=AppServer(demo=True)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.controller.shutdown();self.server.server_close();self.thread.join(2)

    def request(self,path,data=None,token=True,origin=None,host=None):
        headers={}
        if token:headers['X-Session-Token']=self.server.token
        if origin:headers['Origin']=origin
        if host:headers['Host']=host
        body=None
        if data is not None:body=json.dumps(data).encode();headers['Content-Type']='application/json'
        # These are loopback protocol tests; an OS proxy must not reinterpret a
        # deliberately invalid Host header as an external network request.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(urllib.request.Request(self.server.origin+path,data=body,headers=headers),timeout=2)

    def test_assets_and_no_remote_scripts(self):
        with self.request('/') as r:
            self.assertIn('frame-ancestors',r.headers['Content-Security-Policy'])
            self.assertIn('AI指示灯',r.read().decode())
        with self.request('/api/ports') as r:self.assertEqual(json.load(r)[0]['device'],'DEMO')

    def test_unauthorized_cross_origin_and_host_are_rejected(self):
        for kwargs in [dict(token=False),dict(origin='https://example.com'),dict(host='example.com')]:
            with self.assertRaises(urllib.error.HTTPError) as error:self.request('/api/state',**kwargs)
            self.assertEqual(error.exception.code,403)

    def test_connect_api_does_not_touch_hardware_in_demo(self):
        with self.request('/api/connect',dict(port='DEMO',baud=115200)) as r:self.assertEqual(r.status,202)
        self.server.controller.worker.join(2)
        with self.request('/api/state') as r:s=json.load(r)
        self.assertTrue(s['demo']);self.assertTrue(s['at_ready'])


if __name__=='__main__':unittest.main()
