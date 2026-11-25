import json
from pathlib import Path
import tempfile
import unittest

from ai_status import StateRegistry, visible_status_alerts
from core import DeviceError


class ErrorAckTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'ack.json'
        self.r = StateRegistry(clock=lambda:self.now, ack_path=self.path)
        self.error = dict(source='Codex-local',session='one',state='error',event_id='turn-1')
        self.r.event(self.error)

    def ack(self): self.r.acknowledge_errors([self.error])

    def test_ack_restores_running_without_changing_original_error(self):
        self.r.event(dict(source='Codex-local',session='two',state='running',event_id='turn-2'))
        self.ack()
        snap=self.r.snapshot()
        self.assertEqual(snap['state'],'running')
        row=next(r for r in snap['sessions'] if r['session']=='one')
        self.assertEqual(row['state'],'error')
        self.assertTrue(row['acknowledged'])

    def test_same_poll_and_restart_keep_acknowledgement(self):
        self.ack()
        self.r.event(self.error)
        self.assertEqual(self.r.snapshot()['state'],'idle')
        reloaded=StateRegistry(clock=lambda:self.now, ack_path=self.path)
        reloaded.event(self.error)
        self.assertEqual(reloaded.snapshot()['state'],'idle')

    def test_new_failure_realerts(self):
        self.ack()
        self.r.event({**self.error,'event_id':'turn-new'})
        self.assertEqual(self.r.snapshot()['state'],'error')

    def test_stale_ui_cannot_acknowledge_new_failure(self):
        self.r.event({**self.error,'event_id':'turn-new'})
        self.ack()
        self.assertEqual(self.r.snapshot()['state'],'error')

    def test_ack_does_not_mask_other_errors(self):
        self.r.event({**self.error,'session':'another'})
        self.ack()
        self.assertEqual(self.r.snapshot()['state'],'error')

    def test_generic_heartbeat_keeps_ack_but_new_error_does_not(self):
        event=dict(source='Other',session='task',state='error')
        self.r.event(event)
        row=next(r for r in self.r.snapshot()['sessions'] if r['source']=='Other')
        self.r.acknowledge_errors([row,self.error])
        self.r.event({**event,'state':'heartbeat'})
        self.assertEqual(self.r.snapshot()['state'],'idle')
        self.r.event(event)
        self.assertEqual(self.r.snapshot()['state'],'error')

    def test_ack_does_not_extend_ttl(self):
        self.ack();self.now+=121
        self.assertEqual(self.r.snapshot()['state'],'offline')

    def test_bad_request_has_no_partial_effect(self):
        with self.assertRaises(DeviceError): self.r.acknowledge_errors([self.error,{}])
        self.assertEqual(self.r.snapshot()['state'],'error')

    def test_display_reset_keeps_heartbeat_but_new_occurrence_realerts(self):
        e = {**self.error,'state':'waiting'}
        self.r.event(e)
        alerts = visible_status_alerts(self.r.snapshot())
        self.assertEqual(self.r.reset_status_display(alerts), (1, 0))
        self.r.event({**e,'state':'heartbeat'})
        self.assertEqual(self.r.snapshot()['state'],'idle')
        self.r.event({**e,'state':'running'})
        self.r.event(e)
        self.assertEqual(self.r.reset_status_display(alerts), (0, 0))
        self.assertEqual(self.r.snapshot()['state'],'waiting')

    def test_display_reset_is_not_persisted_and_does_not_change_link_or_ttl(self):
        self.r.reset_status_display(visible_status_alerts(self.r.snapshot()))
        self.assertFalse(self.path.exists())
        row = self.r.snapshot()['sessions'][0]
        self.assertEqual(row['state'],'error')
        self.assertTrue(row['suppressed'])
        self.assertFalse(row['acknowledged'])
        self.now += 121
        self.assertEqual(self.r.snapshot()['state'],'offline')


if __name__=='__main__': unittest.main()
