import time,threading,unittest
from types import SimpleNamespace
from unittest.mock import Mock
from ai_status import AiCoordinator,StateRegistry
from light_control import LightController
from core import DeviceError
class SoundTests(unittest.TestCase):
    def setUp(self):
        self.light=Mock()
        self.link=dict(connected=True,transport='serial',sound_v2=True,sound_ready_at=time.time()-1)
        self.light.snapshot.return_value=self.link
        self.ai=AiCoordinator(self.light,threading.RLock(),lambda *args:None,demo=True)
        self.ai.set_enabled(True)
        self.reg=self.ai.registry
    def put(self,kind,id=None,s='a',**kw):
        self.reg.event(dict(source='qa',session=s,state=kind,event_id=id,**kw))
    def drive(self,t=0):return self.ai._sound_step(self.link,time.monotonic()+t)
    def test_finish_once_after_running(self):
        self.put('running');self.put('idle');self.assertTrue(self.drive())
        self.light.start.assert_called_once_with('light_sound',{'op':'finish'})
        self.put('heartbeat');self.put('idle');self.assertFalse(self.drive(10))
    def test_initial_idle_and_failure_silent(self):
        self.put('idle');self.put('running');self.put('error');self.put('idle');self.assertFalse(self.drive())
    def test_early_history_not_replayed_on_reconnect(self):
        self.reg.event(dict(source='Codex-local',session='x',state='idle',event_id='turn',completed=True),observed_completion_at=time.time()-1200)
        self.assertFalse(self.drive())
        self.put('running');self.put('idle')
        self.link['sound_ready_at']=time.time()+.01
        self.assertFalse(self.drive(10))
    def test_pause_and_preview_silent(self):
        self.put('running');self.put('idle')
        self.ai.set_enabled(False);self.assertFalse(self.drive())
        self.ai.set_enabled(True);self.ai.demonstrate('idle')
        self.assertFalse(self.drive(10))
    def test_old_hardware_excluded(self):
        self.put('running');self.put('idle');self.link['sound_v2']=False
        self.assertFalse(self.drive());self.link['sound_v2']=True
        self.assertFalse(self.drive(10))
    def test_tcp_capable_completion(self):
        self.link['transport']='tcp';self.put('running');self.put('idle');self.assertTrue(self.drive())
    def test_tcp_only_recognises_new_lan_capability(self):
        for header,ok in [('CAPS TOY_SOUND_V2 count=3',False),('CAPS TOY_SOUND_LAN_V1 count=3',True),('CAPS TOY_SOUND_LAN_V1 count=33',False)]:
            c=LightController(lambda *a:None);c._set(transport='tcp');c._write=Mock()
            c._lines=lambda _:iter([header,'OK LIGHT mode=MANUAL color=OFF R=0 Y=0 G=0'])
            c._command('STATUS');self.assertEqual(bool(c.snapshot().get('sound_v2')),ok)
    def test_two_tasks_and_aggregate_still_running(self):
        self.put('running',s='other');self.put('running');self.put('idle')
        self.assertEqual(self.reg.snapshot()['state'],'running')
        self.put('running',s='b');self.put('idle',s='b')
        self.assertTrue(self.drive());self.assertFalse(self.drive());self.assertTrue(self.drive(3))
        self.assertFalse(self.drive(6))
    def test_repeated_explicit_source_turns(self):
        for turn in ['1','2']:
            self.put('idle',turn,completed=True)
            self.put('idle',turn,completed=True)
        self.assertEqual(len(self.reg.finishes()[1]),2)
    def test_usb_caps_and_settings(self):
        c=LightController(lambda *a:None);c._write=Mock();c._lines=lambda _:iter(['CAPS TOY_SOUND_V2 count=3','OK LIGHT mode=MANUAL color=OFF R=0 Y=0 G=0'])
        c._command('STATUS');self.assertEqual(c.snapshot()['sound_count'],3);self.assertTrue(c.snapshot()['sound_v2'])
        c._set(connected=True);c._lines=lambda _:iter(['OK SOUND count=4'])
        c.start('light_sound',dict(op='count',count=4));c.worker.join(1);self.assertEqual(c.snapshot()['sound_count'],4)
        c._write.assert_called_with('SOUND COUNT 4',quiet=True)
        with self.assertRaises(DeviceError):c.start('light_sound',dict(op='count',count=-1))
        c._lines=lambda _:iter(['OK SOUND queued track=0001'])
        c.start('light_sound',dict(op='finish'));c.worker.join(1);c._write.assert_called_with('SOUND FINISH',quiet=True)
        c._close();self.assertFalse(c.snapshot()['sound_v2'])
    def test_manual_track_capability_and_request(self):
        c=LightController(lambda *a:None);c._set(transport='tcp',connected=True,sound_v2=True);c._write=Mock()
        with self.assertRaises(DeviceError):c.start('light_sound',dict(op='play',track=257))
        c._lines=lambda _:iter(['CAPS TOY_SOUND_LAN_V1 count=3','CAPS TOY_PLAY_V1 max=3000','OK LIGHT mode=MANUAL color=OFF R=0 Y=0 G=0']);c._command('STATUS')
        with self.assertRaises(DeviceError):c.start('light_sound',dict(op='play',track=3001))
        c._lines=lambda _:iter(['OK SOUND queued track=0257']);c.start('light_sound',dict(op='play',track=257));c.worker.join(1)
        c._write.assert_called_with('SOUND PLAY 257',quiet=True);self.assertIn('0257.mp3',c.snapshot()['sound_message'])
    def test_no_sound_ack_does_not_disconnect_or_retry(self):
        c=LightController(lambda *a:None);c._set(sound_v2=True,connected=True);c._lines=lambda _:iter([]);c._write=Mock()
        c.start('light_sound',{'op':'finish'});c.worker.join(1)
        self.assertTrue(c.snapshot()['connected']);self.assertEqual(c._write.call_count,1)
    def test_wrong_caps_or_plain_nano(self):
        for header in ['CAPS TOY_SOUND_V2 count=33','', 'CAPS TOY_SOUND_V1']:
            c=LightController(lambda *a:None);c._write=Mock();c._lines=lambda _:iter([header,'OK LIGHT mode=MANUAL color=OFF R=0 Y=0 G=0'])
            c._command('STATUS');self.assertFalse(c.snapshot().get('sound_v2'))
