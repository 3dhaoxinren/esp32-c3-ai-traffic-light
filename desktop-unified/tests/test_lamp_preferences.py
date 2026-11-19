import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

from lamp_preferences import LampPreferences, DEFAULT_PATTERNS
from core import DeviceError
from ai_status import AiCoordinator
from desktop_tray import tray_status
from server import AppServer
import test_lights


class PreferencesTests(unittest.TestCase):
    def test_restart_and_invalid_save_preserve_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            p = LampPreferences(path)
            data = p.snapshot()
            data['running'] = dict(color='GREEN', effect='BREATHE')
            p.save(data)
            self.assertEqual(LampPreferences(path).snapshot(), data)
            before = path.read_bytes()
            for invalid in [dict(color='RED', effect='BREATHE'), dict(color='OFF', effect='FAST'), dict(color='BLUE', effect='SOLID')]:
                bad = p.snapshot(); bad['running'] = invalid
                with self.assertRaises(DeviceError): p.save(bad)
                self.assertEqual(path.read_bytes(), before)
            bad = p.snapshot(); bad['offline'] = dict(color='GREEN', effect='SOLID')
            with self.assertRaises(DeviceError): p.save(bad)
            self.assertEqual(p.snapshot(), data)

    def test_corrupt_config_falls_back_and_save_repairs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'; path.write_text('broken')
            p = LampPreferences(path)
            self.assertTrue(p.warning)
            self.assertEqual(p.snapshot(), DEFAULT_PATTERNS)
            p.save(DEFAULT_PATTERNS)
            self.assertFalse(p.warning)
            self.assertFalse(LampPreferences(path).warning)

    def test_api_only_session_token_may_save(self):
        with tempfile.TemporaryDirectory() as directory:
            p = LampPreferences(Path(directory) / 'settings.json')
            server = AppServer(preferences=p)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                def request(data, header):
                    return urllib.request.urlopen(urllib.request.Request(server.origin + '/api/ai_settings',
                        data=json.dumps(data).encode(), headers={'Content-Type':'application/json', **header}), timeout=2)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request({'patterns':DEFAULT_PATTERNS}, {'X-AI-Token':server.ai_token})
                self.assertEqual(error.exception.code,403)
                self.assertFalse(p.path.exists())
                with request({'patterns':DEFAULT_PATTERNS},{'X-Session-Token':server.token}) as response:
                    self.assertEqual(response.status,202)
                before=p.path.read_bytes()
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request({'patterns':{}},{'X-Session-Token':server.token})
                self.assertEqual(error.exception.code,409)
                self.assertEqual(before,p.path.read_bytes())
            finally:
                server.shutdown();server.ai.shutdown();server.lights.shutdown();server.server_close()

    def test_changed_pattern_dispatched_without_state_transition(self):
        class Lamp:
            def __init__(self): self.sent=[];self.arrived=threading.Event()
            def snapshot(self): return {'connected':True,'busy':False}
            def start(self,action,data): self.sent.append(data);self.arrived.set()
        with tempfile.TemporaryDirectory() as directory:
            lamp=Lamp(); lock=threading.RLock()
            ai=AiCoordinator(lamp,lock,lambda *a:None,demo=True,preferences=LampPreferences(Path(directory)/'settings.json'))
            ai.registry.event(dict(source='Test',session='one',state='running',ttl=120))
            ai.set_enabled(True);ai.start()
            try:
                self.assertTrue(lamp.arrived.wait(2));self.assertEqual(lamp.sent[-1]['color'],'YELLOW')
                with lock:
                    lamp.arrived.clear()
                    data=ai.preferences.snapshot();data['running']['color']='GREEN';ai.configure(data)
                self.assertTrue(lamp.arrived.wait(2));self.assertEqual(lamp.sent[-1]['color'],'GREEN')
                self.assertEqual(lamp.sent[-1]['state'],'RUNNING')
            finally: ai.shutdown()


class CustomProtocolTests(unittest.TestCase):
    tcp_peer=test_lights.LightTests.tcp_peer

    def test_custom_green_breathing_reply(self):
        c,writes=self.tcp_peer([b'OK AI state=RUNNING R=0 Y=0 G=1 level=123 lease=15000 color=GREEN effect=BREATHE\r\n'])
        c.start('light_ai',dict(state='RUNNING',color='GREEN',effect='BREATHE'));c.worker.join(3)
        s=c.snapshot();self.assertEqual(s['result'],'success');self.assertEqual(s['ai_color'],'GREEN')
        self.assertEqual(writes,[b'LIGHT AI RUNNING GREEN BREATHE\r\n'])

    def test_custom_flash_off_phase(self):
        c,_=self.tcp_peer([b'OK AI state=WAITING R=0 Y=0 G=0 level=0 lease=15000 color=GREEN effect=SLOW\r\n'])
        c.start('light_ai',dict(state='WAITING',color='GREEN',effect='SLOW'));c.worker.join(3)
        self.assertEqual(c.snapshot()['result'],'success')

    def test_legacy_or_wrong_custom_ack_cannot_confirm_custom_command(self):
        for suffix in ('',' color=YELLOW effect=BREATHE'):
            with self.subTest(suffix=suffix):
                c,_=self.tcp_peer([('OK AI state=RUNNING R=0 Y=1 G=0 level=123 lease=15000'+suffix+'\r\n').encode()])
                c.start('light_ai',dict(state='RUNNING',color='GREEN',effect='BREATHE'));c.worker.join(3)
                self.assertEqual(c.snapshot()['result'],'error')


class TrayTests(unittest.TestCase):
    def test_transport_and_configured_color(self):
        light=dict(connected=True,transport='tcp',endpoint='192.168.1.100:8080',result='success')
        ai=dict(enabled=True,target='running',patterns={**DEFAULT_PATTERNS,'running':dict(color='GREEN',effect='BREATHE')})
        icon,text=tray_status(light,ai)
        self.assertEqual(icon,'green');self.assertIn('Wi-Fi TCP',text);self.assertIn('绿灯呼吸',text)
        self.assertIn(light['endpoint'],text)
        light.update(transport='serial',endpoint='COM3 · 115200')
        self.assertIn('USB',tray_status(light,ai)[1])
        ai['enabled']=False;self.assertEqual(tray_status(light,ai)[0],'blue')
        light['connected']=False;self.assertEqual(tray_status(light,ai)[0],'gray')
        light['result']='error';self.assertEqual(tray_status(light,ai)[0],'red')


if __name__=='__main__':unittest.main()
