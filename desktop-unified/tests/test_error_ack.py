import json
from pathlib import Path
import tempfile
import unittest

from ai_status import StateRegistry
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


if __name__=='__main__': unittest.main()
