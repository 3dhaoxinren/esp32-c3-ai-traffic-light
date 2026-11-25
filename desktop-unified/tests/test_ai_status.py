import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error

from ai_status import StateRegistry, CodexObserver, AiCoordinator, visible_status_alerts
from core import DeviceError
from server import AppServer
import test_lights


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.now=1000
        self.r=StateRegistry(clock=lambda:self.now)

    def put(self,source,state,session='main',ttl=120):
        self.r.event(dict(source=source,session=session,state=state,ttl=ttl))

    def test_priority_across_independent_sessions(self):
        for source,state in [('Codex','running'),('Claude','waiting'),('Other','error'),('Done','idle')]:
            self.put(source,state)
        self.assertEqual(self.r.snapshot()['state'],'error')
        self.put('Other','idle');self.assertEqual(self.r.snapshot()['state'],'waiting')
        self.put('Claude','idle');self.assertEqual(self.r.snapshot()['state'],'running')
        self.put('Codex','idle');self.assertEqual(self.r.snapshot()['state'],'idle')

    def test_heartbeat_preserves_waiting_and_expired_is_not_complete(self):
        self.put('Claude','waiting',ttl=10);self.now+=8
        self.put('Claude','heartbeat',ttl=10);self.now+=8
        self.assertEqual(self.r.snapshot()['state'],'waiting')
        self.now+=3;self.assertEqual(self.r.snapshot()['state'],'offline')
        with self.assertRaises(DeviceError):self.put('Claude','heartbeat')

    def test_same_ai_different_sessions_do_not_overwrite(self):
        self.put('Codex','running','one');self.put('Codex','idle','two')
        self.assertEqual(len(self.r.snapshot()['sessions']),2)
        self.assertEqual(self.r.snapshot()['state'],'running')

    def test_invalid_events_cannot_replace_current_state(self):
        self.put('Codex','running')
        for values in [dict(source='<script>',state='idle'),dict(source='A',state='foo'),dict(source='A',state='idle',ttl=float('nan'))]:
            with self.assertRaises(DeviceError):self.put(**values)
        self.assertEqual(self.r.snapshot()['state'],'running')


class CodexTests(unittest.TestCase):
    def test_reset_reawakens_on_new_action_in_same_turn_but_not_continuation_or_completion(self):
        with tempfile.TemporaryDirectory() as temp, closing(sqlite3.connect(Path(temp)/'thread_history_1.sqlite')) as c:
            c.executescript("""
                CREATE TABLE thread_turns(thread_id, turn_id, status, started_at, completed_at);
                CREATE TABLE thread_items(thread_id, turn_id, item_type, item_json, created_at_ms, completed_at_ms, rollout_ordinal);
                INSERT INTO thread_turns VALUES('test','turn','inProgress',900,NULL);
                INSERT INTO thread_items VALUES('test','turn','commandExecution','{}',910000,NULL,1);
                """)
            c.commit()
            now = 1000
            r = StateRegistry(clock=lambda:now)
            o = CodexObserver(r,temp)
            o.process_alive = lambda:True
            def sync(sql=None):
                if sql:
                    c.executescript(sql)
                    c.commit()
                    o._db_signature = None
                with patch('ai_status.time.time', return_value=now):
                    o.poll()
                return r.snapshot()['state']
            self.assertEqual(sync(),'running')
            self.assertEqual(r.reset_status_display(visible_status_alerts(r.snapshot())), (1,0))
            now += 2
            self.assertEqual(sync(),'idle')
            r.event(dict(source='Codex-local',session='test',state='heartbeat'))
            self.assertEqual(r.snapshot()['state'],'idle')
            self.assertEqual(sync("""UPDATE thread_items SET completed_at_ms=1001000 WHERE rollout_ordinal=1;
                INSERT INTO thread_items VALUES('test','turn','reasoning','{}',1001001,NULL,2);"""), 'idle')
            # A database write arriving late for an action already started before reset.
            self.assertEqual(sync("""INSERT INTO thread_items VALUES('test','turn','mcpToolCall','{}',999999,NULL,3);"""), 'idle')
            self.assertEqual(sync("""INSERT INTO thread_items VALUES('test','turn','mcpToolCall','{}',1001500,NULL,4);"""), 'running')
            self.assertEqual(r.snapshot()['sessions'][0]['event_id'],'turn')
            self.assertFalse(r.snapshot()['sessions'][0]['suppressed'])
            self.assertEqual(r.finishes(),(0,[]))

    def test_a_new_execution_resolves_old_question_but_not_new_question_or_pending_approval(self):
        with tempfile.TemporaryDirectory() as temp, closing(sqlite3.connect(Path(temp)/'thread_history_1.sqlite')) as c:
            c.executescript("""
                CREATE TABLE thread_turns(thread_id, turn_id, status, started_at, completed_at);
                CREATE TABLE thread_items(thread_id, turn_id, item_type, item_json, created_at_ms, completed_at_ms, rollout_ordinal);
                INSERT INTO thread_turns VALUES('test','turn','inProgress',900,NULL);
                INSERT INTO thread_items VALUES('test','turn','agentMessage','{"questions":[{}]}',910000,NULL,1);
                """)
            o=CodexObserver(StateRegistry(),temp)
            self.assertEqual(o.read_states(now=1000),[('test','waiting')])
            c.executescript("""INSERT INTO thread_items VALUES('test','turn','commandExecution','{}',920000,NULL,2);""")
            self.assertEqual(o.read_states(now=1000),[('test','running')])
            c.executescript("""INSERT INTO thread_items VALUES('test','turn','agentMessage','{"questions":[{}]}',930000,NULL,3);""")
            self.assertEqual(o.read_states(now=1000),[('test','waiting')])
            c.executescript("""
                INSERT INTO thread_items VALUES('test','turn','commandExecution','{"status":"waitingForApproval"}',940000,NULL,4);
                INSERT INTO thread_items VALUES('test','turn','commandExecution','{}',950000,NULL,5);
                """)
            self.assertEqual(o.read_states(now=1000),[('test','waiting')])

    def test_lifecycle_questions_and_tool_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            c=sqlite3.connect(Path(temp)/'thread_history_1.sqlite')
            c.executescript('''CREATE TABLE thread_turns(thread_id TEXT, turn_id TEXT, status TEXT, started_at INTEGER, completed_at INTEGER);
              CREATE TABLE thread_items(thread_id TEXT, turn_id TEXT,item_type TEXT,item_json TEXT,created_at_ms INTEGER,completed_at_ms INTEGER,rollout_ordinal INTEGER);
              INSERT INTO thread_turns VALUES('test','turn','inProgress',900,NULL);
              INSERT INTO thread_items VALUES('test','turn','userMessage','{}',900000,900000,1);
              INSERT INTO thread_items VALUES('test','turn','commandExecution','{"status":"failed"}',910000,910000,2);''')
            c.commit();o=CodexObserver(StateRegistry(),temp)
            self.assertEqual(o.read_states(now=1000),[('test','running')])
            c.execute("INSERT INTO thread_items VALUES('test','turn','agentMessage',?,920000,920000,3)",(json.dumps({'questions':[{'title':'fixture'}]}),));c.commit()
            self.assertEqual(o.read_states(now=1000),[('test','waiting')])
            c.execute("INSERT INTO thread_items VALUES('test','turn','userMessage','{}',930000,930000,4)");c.commit()
            self.assertEqual(o.read_states(now=1000),[('test','running')])
            c.execute("UPDATE thread_turns SET status='completed',completed_at=940");c.commit()
            self.assertEqual(o.read_states(now=1000),[('test','idle')])
            c.execute("UPDATE thread_turns SET status='failed'");c.commit()
            self.assertEqual(o.read_states(now=1000),[('test','error')])
            self.assertEqual(o.read_states(now=5000),[])
            c.close()


class AiProtocolTests(unittest.TestCase):
    tcp_peer = test_lights.LightTests.tcp_peer
    run_command = test_lights.LightTests.run_command
    # Reuse real socket-pair transport fixture, not an Arduino implementation clone.
    def test_ai_fragmented_pwm_reply(self):
        c,writes=self.tcp_peer([b'OK AI state=RUNNING R=0 Y=1 G=0 level=',b'128 lease=15000\r\n'])
        c.start('light_ai',dict(state='RUNNING'));c.worker.join(3)
        self.assertEqual(c.snapshot()['mode'],'AI');self.assertEqual(c.snapshot()['level'],128)
        self.assertEqual(writes,[b'LIGHT AI RUNNING\r\n'])

    def test_ai_flash_off_phase_is_valid(self):
        c,_=self.tcp_peer([b'OK AI state=WAITING R=0 Y=0 G=0 level=0 lease=15000\r\n'])
        self.assertEqual(self.run_command(c,'STATUS')['ai_state'],'WAITING')

    def test_ai_wrong_color_rejected(self):
        c,_=self.tcp_peer([b'OK AI state=ERROR R=0 Y=0 G=1 level=255 lease=15000\r\n'])
        self.assertEqual(self.run_command(c,'STATUS')['result'],'error')


class ApiTests(unittest.TestCase):
    def test_event_token_only_grants_event_access(self):
        server=AppServer();worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            data=json.dumps(dict(source='TestAI',session='one',state='running')).encode()
            def req(path,token):
                return urllib.request.Request(server.origin+path,data=data,headers={'Content-Type':'application/json','X-AI-Token':token})
            with urllib.request.urlopen(req('/api/ai_event',server.ai_token),timeout=2) as result:self.assertEqual(result.status,202)
            self.assertEqual(server.ai.registry.snapshot()['state'],'running')
            for path,token in [('/api/ai_event','bad'),('/api/exit',server.ai_token)]:
                with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(req(path,token),timeout=2)
                self.assertEqual(error.exception.code,403)
        finally:
            server.shutdown();server.ai.shutdown();server.lights.shutdown();server.server_close()


if __name__=='__main__':unittest.main()
