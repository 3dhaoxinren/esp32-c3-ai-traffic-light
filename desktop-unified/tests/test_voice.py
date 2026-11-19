"""Voice-hold replies must not masquerade as completed AI lamp commands."""
import unittest
import threading
from unittest.mock import Mock

import test_lights
from ai_status import AiCoordinator
from lamp_preferences import DEFAULT_PATTERNS


class VoiceProtocolTests(unittest.TestCase):
    tcp_peer = test_lights.LightTests.tcp_peer
    run_command = test_lights.LightTests.run_command

    def test_ai_heartbeat_reports_actual_held_lamp(self):
        c, writes = self.tcp_peer([b'VOICE CMD 4\r\nOK VO',
            b'ICE hold=1 ai=RUNNING color=OFF R=0 Y=0 G=0\r\n'])
        c.start('light_ai', dict(state='RUNNING', color='YELLOW', effect='BREATHE'))
        c.worker.join(3)
        s = c.snapshot()
        self.assertEqual(s['result'], 'success')
        self.assertTrue(s['connected'])
        self.assertTrue(s['voice_hold'])
        self.assertEqual((s['mode'], s['color'], s['level']), ('VOICE', 'OFF', 0))
        self.assertEqual(s['ai_color'], '')
        self.assertEqual(writes, [b'LIGHT AI RUNNING YELLOW BREATHE\r\n'])

    def test_status_accepts_manual_voice_color(self):
        c, _ = self.tcp_peer([b'OK VOICE hold=1 ai=IDLE color=RED R=1 Y=0 G=0\r\n'])
        s = self.run_command(c, 'STATUS')
        self.assertEqual((s['mode'], s['color'], s['pins']), ('VOICE', 'RED', [1, 0, 0]))

    def test_mismatched_cached_ai_state_is_rejected(self):
        c, _ = self.tcp_peer([b'OK VOICE hold=1 ai=IDLE color=OFF R=0 Y=0 G=0\r\n'])
        c.start('light_ai', dict(state='RUNNING'))
        c.worker.join(3)
        self.assertEqual(c.snapshot()['result'], 'error')
        self.assertFalse(c.snapshot()['connected'])

    def test_mismatched_voice_pins_are_rejected(self):
        c, _ = self.tcp_peer([b'OK VOICE hold=1 ai=IDLE color=RED R=0 Y=0 G=1\r\n'])
        self.assertEqual(self.run_command(c, 'STATUS')['result'], 'error')

    def test_manual_button_requires_exit_from_voice_hold(self):
        c, _ = self.tcp_peer([b'OK VOICE hold=1 ai=IDLE color=RED R=1 Y=0 G=0\r\n'])
        self.assertEqual(self.run_command(c, 'RED')['result'], 'error')

    def test_legacy_manual_reply_clears_hold(self):
        c, _ = self.tcp_peer([b'OK LIGHT mode=MANUAL color=GREEN R=0 Y=0 G=1\r\n'])
        c._set(voice_hold=True)
        s = self.run_command(c, 'GREEN')
        self.assertEqual(s['result'], 'success')
        self.assertFalse(s['voice_hold'])

    def test_ai_resume_reply_clears_hold(self):
        c, _ = self.tcp_peer([b'OK AI state=IDLE R=0 Y=0 G=1 level=255 lease=15000\r\n'])
        c._set(voice_hold=True)
        s = self.run_command(c, 'STATUS')
        self.assertEqual(s['mode'], 'AI')
        self.assertFalse(s['voice_hold'])

    def test_explicit_pause_sends_manual_off_even_during_voice_hold(self):
        lights = Mock()
        lights.snapshot.return_value = dict(connected=True, busy=False, voice_hold=True)
        prefs = Mock()
        prefs.snapshot.return_value = DEFAULT_PATTERNS
        coordinator = AiCoordinator(lights, threading.RLock(), lambda *a: None,
                                    demo=True, preferences=prefs)
        coordinator.set_enabled(False)
        coordinator.stop_event = Mock()
        coordinator.stop_event.wait.side_effect = [False, True]
        coordinator._loop()
        lights.start.assert_called_once_with('light_send', {'command': 'OFF'})

    def test_ordinary_offline_heartbeat_preserves_voice_hold(self):
        lights = Mock()
        lights.snapshot.return_value = dict(connected=True, busy=False, voice_hold=True)
        prefs = Mock()
        prefs.snapshot.return_value = DEFAULT_PATTERNS
        coordinator = AiCoordinator(lights, threading.RLock(), lambda *a: None,
                                    demo=True, preferences=prefs)
        coordinator.set_enabled(True)
        coordinator.stop_event = Mock()
        coordinator.stop_event.wait.side_effect = [False, True]
        coordinator._loop()
        self.assertEqual(lights.start.call_args.args[0], 'light_ai')
        self.assertEqual(lights.start.call_args.args[1]['state'], 'OFFLINE')


if __name__ == '__main__':
    unittest.main()
