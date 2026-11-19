import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from ai_status import AiCoordinator, StateRegistry, visible_errors
from core import DeviceError
from desktop_tray import DesktopTray
from light_control import LightController
from server import AppServer
from startup import AutoConnector, ConnectionPreferences


class TrayAckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.dict(os.environ, LOCALAPPDATA=tmp.name):
            self.ai = AiCoordinator(NS(snapshot=lambda: dict(connected=True)), threading.RLock(), Mock(), demo=True)
        self.ai.registry = StateRegistry(ack_path=Path(tmp.name)/'ack.json')
        self.event = dict(source='test', session='failed', event_id='old', state='error', ttl=60)
        self.ai.registry.event(self.event)
        self.ai.enabled = True
        self.ai.applied = 'error'

    def menu(self, during=None):
        light = dict(connected=True, busy=False, transport='serial', endpoint='COM3')
        server = NS(lights=NS(snapshot=lambda: light), ai=self.ai, connection_lock=self.ai.connection_lock)
        tray = DesktopTray(server)
        tray._update = Mock()
        gui = Mock()
        gui.GetCursorPos.return_value = (10, 10)
        gui.TrackPopupMenu.side_effect = lambda *args: (during() if during else None) or 5
        constants = NS(MF_STRING=0, MF_GRAYED=1, MF_SEPARATOR=2, TPM_RETURNCMD=4, TPM_RIGHTBUTTON=8, WM_NULL=0)
        with patch.dict('sys.modules', win32gui=gui, win32con=constants):
            tray._menu()
        return gui, tray

    def test_menu_action_acknowledges_current_error_and_updates_tray(self):
        self.ai.registry.event(dict(source='test', session='other', event_id='run', state='running', ttl=60))
        gui, tray = self.menu()
        gui.AppendMenu.assert_any_call(gui.CreatePopupMenu(), 0, 5, '已查看，恢复正常')
        self.assertEqual(self.ai.snapshot()['target'], 'running')
        self.assertEqual(self.ai.applied, '')
        self.assertTrue(self.ai.enabled)
        tray._update.assert_called_once()
        row = next(r for r in self.ai.snapshot()['sessions'] if r['session'] == 'failed')
        self.assertEqual(row['state'], 'error')
        self.assertTrue(row['acknowledged'])

    def test_menu_open_does_not_acknowledge_newer_error(self):
        self.menu(lambda: self.ai.registry.event({**self.event, 'event_id': 'new'}))
        self.assertEqual(self.ai.snapshot()['state'], 'error')
        self.assertFalse(self.ai.snapshot()['sessions'][0]['acknowledged'])

    def test_other_new_error_stays_visible(self):
        self.menu(lambda: self.ai.registry.event({**self.event, 'session': 'new-task'}))
        self.assertEqual(self.ai.snapshot()['state'], 'error')
        self.assertEqual(len(visible_errors(self.ai.snapshot())), 1)

    def test_no_error_menu_item_is_disabled(self):
        self.ai.acknowledge_errors(visible_errors(self.ai.snapshot()))
        gui, _ = self.menu()
        gui.AppendMenu.assert_any_call(gui.CreatePopupMenu(), 1, 5, '已查看，恢复正常')

    def test_confirmation_failure_preserves_error(self):
        self.ai.registry.ack_path = Path(self.ai.registry.ack_path.parent)  # directory, cannot replace
        with self.assertRaises(DeviceError):
            self.ai.acknowledge_errors(visible_errors(self.ai.snapshot()))
        self.assertEqual(self.ai.snapshot()['state'], 'error')


class LocalEditionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.dict(os.environ, LOCALAPPDATA=tmp.name):
            self.server = AppServer(local_only=True)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, action, data=None):
        req = urllib.request.Request(self.server.origin+'/api/'+action,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'X-Session-Token':self.server.token, 'Content-Type':'application/json'})
        with self.opener.open(req, timeout=3) as response:
            return json.load(response)

    def test_local_ui_and_api_do_not_expose_network_or_screen(self):
        self.assertTrue(self.call('state')['local_only'])
        with self.opener.open(self.server.origin+'/') as response:
            html = response.read().decode()
        self.assertIn('Nano 本地版', html)
        self.assertNotIn('模块 AT 配置', html)
        for action in ['connect', 'configure', 'transparent', 'upgrade', 'screen_settings', 'screen_notice']:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.call(action, {})
            self.assertEqual(error.exception.code, 409)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.opener.open(self.server.origin+'/Nano_HLK_M30.ino')
        self.assertEqual(error.exception.code, 404)

    def test_light_controller_rejects_tcp_net_and_oled_before_io(self):
        c = self.server.lights
        c._set(connected=True)
        for action, data in [('light_connect',dict(transport='tcp',host='192.168.1.2',tcp_port=8080)),
                             ('light_send',dict(command='NET ON')),('light_screen',{}),('light_notice',{})]:
            with patch('light_control.socket.create_connection') as socket_connect, patch('light_control.serial.Serial') as serial_connect:
                with self.assertRaises(DeviceError):
                    c.start(action,data)
                socket_connect.assert_not_called()
                serial_connect.assert_not_called()

    def test_legacy_tcp_target_is_suspended_not_connected(self):
        a = self.server.startup
        a.preferences.save(auto_connect=True,target=dict(transport='tcp',host='192.168.1.2',tcp_port=8080))
        with patch.object(self.server.lights,'start') as start:
            a.step()
        start.assert_not_called()
        self.assertTrue(a.suspended)

    def test_local_coordinator_never_sends_screen_commands(self):
        self.server.ai.demo = True
        with patch.object(self.server.lights,'snapshot',return_value=dict(connected=True,busy=False)), patch.object(self.server.lights,'start') as start:
            self.server.ai.start()
            try:
                time.sleep(.65)
            finally:
                self.server.ai.shutdown()
        start.assert_not_called()

    def test_local_web_ack_uses_the_same_state_logic(self):
        r = self.server.ai.registry
        r.event(dict(source='test', session='a',state='error',event_id='a1'))
        self.call('ai_ack_errors',dict(events=visible_errors(self.server.ai.snapshot())))
        self.assertEqual(self.call('state')['ai']['state'],'idle')
        r.event(dict(source='test', session='a',state='error',event_id='a2'))
        self.assertEqual(self.call('state')['ai']['state'],'error')


if __name__=='__main__':
    unittest.main()
