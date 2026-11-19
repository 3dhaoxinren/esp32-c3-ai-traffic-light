import json
from pathlib import Path
import os
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
import urllib.request

from ai_status import AiCoordinator
from core import DeviceError
from server import AppServer
import test_lights


class UniversalTests(unittest.TestCase):
    tcp_peer = test_lights.LightTests.tcp_peer

    def test_general_entry_keeps_full_ui_and_version(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, LOCALAPPDATA=tmp):
            server = AppServer(demo=True)
        self.addCleanup(server.server_close)
        self.assertFalse(server.local_only)
        self.assertEqual(server.controller.snapshot()['app_version'], '3.0.0')
        html = (server.assets/'index.html').read_text(encoding='utf-8')
        for text in ['Wi-Fi 配置', 'TCP 客户端', 'Nano USB', '已查看，恢复正常', '开机自启', 'C3 音频']:
            self.assertIn(text, html)
        self.assertIn('3.0.0', html)

    def test_general_api_accepts_tcp_and_serial_without_local_restriction(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, LOCALAPPDATA=tmp):
            server = AppServer(demo=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with patch.object(server.lights, 'start') as start:
            for data in [dict(transport='tcp', host='192.168.1.2', tcp_port=8080),
                         dict(transport='serial', port='COM18'), dict(transport='serial', port='COM3')]:
                req = urllib.request.Request(server.origin+'/api/light_connect',
                    data=json.dumps(data).encode(), headers={'Content-Type':'application/json', 'X-Session-Token':server.token})
                with opener.open(req, timeout=3) as response:
                    self.assertEqual(response.status, 202)
                start.assert_called_with('light_connect', data)

    def test_toy_no_screen_is_not_a_broken_lamp_or_fake_screen_ack(self):
        c, writes = self.tcp_peer([b'ERR use LIGHT HELP (USB-only; no NET/AT/SCREEN/VOICE)\r\n'])
        c.start('light_screen', dict(rotation=0, mode='AUTO', state='RUNNING'))
        c.worker.join(3)
        s = c.snapshot()
        self.assertTrue(s['connected'])
        self.assertFalse(s['screen_supported'])
        self.assertEqual(s['screen_error'], '')
        self.assertEqual(s['result'], 'idle')
        self.assertNotIn('screen', s)
        self.assertEqual(writes, [b'SCREEN 0 AUTO RUNNING\r\n'])
        c._screen(dict(rotation=0, mode='AUTO', state='IDLE'))
        self.assertEqual(len(writes), 1)

    def test_legacy_unknown_screen_response_also_stops_screen_queries(self):
        c, _ = self.tcp_peer([b'ERR use LIGHT HELP, NET ON, or an at+ command.\r\n'])
        c.start('light_screen', dict(rotation=0, mode='AUTO', state='IDLE'))
        c.worker.join(3)
        self.assertIs(c.snapshot()['screen_supported'], False)

    def test_real_oled_failure_not_misclassified_as_unsupported(self):
        c, _ = self.tcp_peer([b'OK SCREEN rotation=0 mode=AUTO state=RUNNING online=0\r\n'])
        c.start('light_screen', dict(rotation=0, mode='AUTO', state='RUNNING'))
        c.worker.join(3)
        self.assertTrue(c.snapshot()['screen_supported'])
        self.assertFalse(c.snapshot()['screen']['online'])
        self.assertTrue(c.snapshot()['screen_error'])

    def test_screen_argument_error_stays_visible(self):
        c, _ = self.tcp_peer([b'ERR SCREEN invalid state\r\n'])
        c.start('light_screen', dict(rotation=0, mode='AUTO', state='RUNNING'))
        c.worker.join(3)
        self.assertIsNot(c.snapshot().get('screen_supported'), False)
        self.assertTrue(c.snapshot()['screen_error'])

    def test_reconnect_clears_old_capability_and_stale_screen(self):
        c, _ = self.tcp_peer([b'OK LIGHT mode=MANUAL color=OFF R=0 Y=0 G=0\r\n'])
        link = c.link
        c.link = None
        c._set(connected=False, screen_supported=False, screen={'online':True}, screen_error='old')
        with patch('light_control.socket.create_connection', return_value=link):
            c.start('light_connect', dict(transport='tcp', host='192.168.1.2', tcp_port=8080))
            c.worker.join(3)
        self.assertIsNone(c.snapshot()['screen_supported'])
        self.assertNotIn('screen', c.snapshot())
        self.assertEqual(c.snapshot()['screen_error'], '')

    def test_unsupported_notice_rejected_without_closing_lamp(self):
        c, writes = self.tcp_peer([])
        c._set(screen_supported=False)
        with self.assertRaises(DeviceError):
            c.start('light_notice', dict(rotation=0, mode='AUTO', state='RUNNING', seconds=5, text='test'))
        self.assertTrue(c.snapshot()['connected'])
        self.assertEqual(writes, [])

    def test_first_notice_detects_no_screen_without_sending_bitmap(self):
        c, writes = self.tcp_peer([b'ERR use LIGHT HELP (USB-only; no NET/AT/SCREEN/VOICE)\r\n'])
        c.start('light_notice', dict(rotation=0, mode='AUTO', state='RUNNING', seconds=5, text='test'))
        c.worker.join(3)
        self.assertTrue(c.snapshot()['connected'])
        self.assertIs(c.snapshot()['screen_supported'], False)
        self.assertEqual(c.snapshot()['result'], 'error')
        self.assertEqual(writes, [b'SCREEN 0 AUTO RUNNING\r\n'])

    def test_ai_heartbeat_keeps_working_without_screen(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, LOCALAPPDATA=tmp):
            lights = NS(snapshot=lambda: dict(connected=True,busy=False,screen_supported=False), start=Mock())
            ai = AiCoordinator(lights, threading.RLock(), Mock(), demo=True)
            ai.enabled = True
            ai.start()
            try:
                time.sleep(.65)
            finally:
                ai.shutdown()
        self.assertTrue(lights.start.called)
        self.assertTrue(all(call.args[0]=='light_ai' for call in lights.start.call_args_list))

    def test_net_on_is_still_supported_for_wireless_boards(self):
        c, _ = self.tcp_peer([b'NET READY: module confirmed\r\n'])
        c._command('NET ON')
        self.assertEqual(c.snapshot()['result'], 'success')


if __name__ == '__main__':
    unittest.main()
