import tempfile
from pathlib import Path
import unittest
from core import DeviceError
from screen_preferences import ScreenPreferences,screen_target,validate_screen,render_notice
from ai_status import StateRegistry
import test_lights

class ScreenTests(unittest.TestCase):
    tcp_peer=test_lights.LightTests.tcp_peer
    def test_preferences_and_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'screen.json';s=ScreenPreferences(p)
            s.save({'rotation':90,'mode':'RAIN'})
            self.assertEqual(ScreenPreferences(p).snapshot(),{'rotation':90,'mode':'RAIN','done_text':'已完成'})
            for bad in [{'rotation':True,'mode':'AUTO'},{'rotation':45,'mode':'AUTO'},{'rotation':0,'mode':'bad'}]:
                with self.assertRaises(DeviceError):s.save(bad)
            self.assertEqual(ScreenPreferences(p).snapshot(),{'rotation':90,'mode':'RAIN','done_text':'已完成'})
    def test_notice_bitmaps_and_invalid_text(self):
        a=render_notice('已完成，请查收',0);b=render_notice('已完成，请查收',90)
        self.assertEqual(len(a),1024);self.assertTrue(any(a));self.assertNotEqual(a,b)
        for text in ['', 'a'*33, 'hello\x00', '\n'*10+'a']:
            with self.assertRaises(DeviceError):render_notice(text,0)
    def test_completion_is_not_idle_error_ack_or_expiry(self):
        now=[100];r=StateRegistry(clock=lambda:now[0])
        def event(state,**kwargs):r.event(dict(source='test',session='one',state=state,ttl=30,**kwargs))
        def target():
            s=r.snapshot();return screen_target({**s,'target':s['state'],'preview':None})
        event('idle');self.assertEqual(target(),'IDLE')
        event('running');event('waiting');event('idle');self.assertEqual(target(),'DONE')
        event('heartbeat');self.assertEqual(target(),'DONE')
        event('error',event_id='failed');r.acknowledge_errors([{'source':'test','session':'one','event_id':'failed'}]);self.assertEqual(target(),'IDLE')
        event('running');now[0]+=31;event('idle');self.assertEqual(target(),'IDLE')
        event('idle',completed=True);self.assertEqual(target(),'DONE')
        now[0]+=31;self.assertEqual(target(),'OFFLINE')
    def test_screen_real_transport_ack(self):
        c,w=self.tcp_peer([b'OK SCREEN rotation=90 mode=AUTO state=DONE online=1\r\n'])
        c.start('light_screen',dict(rotation=90,mode='AUTO',state='DONE'));c.worker.join(3)
        self.assertTrue(c.snapshot()['screen']['online']);self.assertEqual(w,[b'SCREEN 90 AUTO DONE\r\n'])
    def test_wrong_reply_cannot_be_applied(self):
        c,_=self.tcp_peer([b'OK SCREEN rotation=0 mode=AUTO state=DONE online=1\r\n'])
        c.start('light_screen',dict(rotation=90,mode='AUTO',state='DONE'));c.worker.join(3)
        self.assertFalse(c.snapshot()['connected']);self.assertNotIn('screen',c.snapshot())

if __name__=='__main__':unittest.main()
