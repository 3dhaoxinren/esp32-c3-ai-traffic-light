"""Local multi-session state aggregation and a read-only Codex adapter."""
import json
import os
from pathlib import Path
import re
from collections import deque
import sqlite3
import subprocess
import threading
import time
import uuid

from core import DeviceError
from lamp_preferences import LampPreferences
from screen_preferences import ScreenPreferences, screen_target

PRIORITY = {"offline": 0, "idle": 1, "running": 2, "waiting": 3, "error": 4}
LABELS = {"offline": "未接入 / 状态失效", "idle": "空闲 / 完成", "running": "执行中", "waiting": "等待确认", "error": "异常 / 报错"}
IDENTIFIER = re.compile(r"^[\w .:@-]{1,80}$")


def visible_errors(snapshot):
    return [{k: row[k] for k in ('source', 'session', 'event_id')}
            for row in snapshot['sessions'] if row['effective'] == 'error' and not row['stale']]


class StateRegistry:
    def __init__(self, clock=time.time, ack_path=None):
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions = {}
        self.done_log = deque(maxlen=256)
        self.done_seq = 0
        self.ack_path = Path(ack_path) if ack_path else None
        self.acknowledged = []
        if self.ack_path:
            try:
                values = json.loads(self.ack_path.read_text(encoding='utf-8'))
                if not isinstance(values, list): raise ValueError()
                self.acknowledged = [tuple(v) for v in values[-512:]
                    if isinstance(v, list) and len(v) == 3 and all(isinstance(s, str) for s in v)]
            except (OSError, ValueError, TypeError):
                pass

    def finishes(self, after=0):
        with self.lock:
            return self.done_seq, [(seq, stamp) for seq, stamp, _ in self.done_log if seq>after]

    def event(self, data, *, observed_completion_at=None):
        source, session, state = data.get("source"), data.get("session"), data.get("state")
        if any(not isinstance(x, str) or not IDENTIFIER.fullmatch(x) for x in (source, session)):
            raise DeviceError("source 和 session 需为 1–80 位名称或标识")
        if not isinstance(state, str) or state not in {*PRIORITY, "heartbeat", "remove"}:
            raise DeviceError("AI 状态不支持")
        ttl = data.get("ttl", 120)
        if type(ttl) not in (int, float) or not 10 <= ttl <= 86400:
            raise DeviceError("状态有效期应为 10–86400 秒")
        now = self.clock()
        key = (source, session)
        event_id = data.get('event_id')
        if event_id is not None and (not isinstance(event_id, str) or not IDENTIFIER.fullmatch(event_id)):
            raise DeviceError('event_id 需为 1–80 位标识')
        with self.lock:
            if state == "remove":
                self.sessions.pop(key, None)
                return
            if key not in self.sessions and len(self.sessions) >= 64:
                expired = [k for k, v in self.sessions.items() if v["expires"] <= now]
                for k in expired:
                    del self.sessions[k]
                if len(self.sessions) >= 64:
                    raise DeviceError("最多接入 64 个会话，请先移除旧会话")
            if state == "heartbeat":
                if key not in self.sessions or self.sessions[key]["expires"] <= now:
                    raise DeviceError("会话不存在或已失效，请重新上报明确状态")
                state = self.sessions[key]["state"]
                event_id = self.sessions[key]['event_id']
            previous = self.sessions.get(key, {})
            completed = state == 'idle' and (
                data.get('completed') is True or
                (previous.get('expires', 0) > now and previous.get('state') in ('running', 'waiting')) or
                (previous.get('expires', 0) > now and previous.get('completed', False)
                 and (state == previous.get('state')) and (not event_id or event_id == previous.get('event_id'))))
            continuing = completed and previous.get('state') == 'idle' and previous.get('completed') and previous.get('expires',0)>now
            event_id = event_id or (previous['event_id'] if continuing else uuid.uuid4().hex)
            if completed and not (continuing and event_id==previous['event_id']):
                identity=(source,session,event_id)
                if not any(k==identity for _,_,k in self.done_log):
                    self.done_seq+=1
                    stamp=now if observed_completion_at is None else min(float(observed_completion_at),now)
                    self.done_log.append((self.done_seq,stamp,identity))
            self.sessions[key] = dict(source=source, session=session, state=state,
                                      event_id=event_id, updated=now, expires=now + ttl, completed=completed)

    def acknowledge_errors(self, events):
        # Acknowledge exactly what was displayed, never a newer unseen failure.
        if not isinstance(events, list) or len(events) > 64:
            raise DeviceError('请提交当前显示的异常列表')
        with self.lock:
            acknowledgements = list(self.acknowledged)
            for event in events:
                if not isinstance(event, dict) or any(not isinstance(event.get(k), str)
                        for k in ('source', 'session', 'event_id')):
                    raise DeviceError('异常标识不完整')
                key = (event['source'], event['session'])
                row = self.sessions.get(key)
                identity = (*key, event['event_id'])
                if (row and row['state'] == 'error' and row['expires'] > self.clock()
                        and row['event_id'] == event['event_id'] and identity not in acknowledgements):
                    acknowledgements.append(identity)
            acknowledgements = acknowledgements[-512:]
            if self.ack_path:
                try:
                    self.ack_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.ack_path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(acknowledgements), encoding='utf-8')
                    temporary.replace(self.ack_path)
                except OSError as error:
                    raise DeviceError('无法保存异常确认记录，请重试') from error
            self.acknowledged = acknowledgements

    def snapshot(self):
        now = self.clock()
        with self.lock:
            rows = []
            for v in self.sessions.values():
                acknowledged = v['state'] == 'error' and (v['source'], v['session'], v['event_id']) in self.acknowledged
                effective = 'offline' if v['expires'] <= now else 'idle' if acknowledged else v['state']
                rows.append({**v, 'stale': v['expires'] <= now, 'acknowledged': acknowledged,
                             'effective': effective})
        state = max((r["effective"] for r in rows), key=PRIORITY.get, default="offline")
        rows.sort(key=lambda r: (-PRIORITY[r["effective"]], r["source"], r["session"]))
        return {"state": state, "label": LABELS[state], "sessions": rows}


class CodexObserver:
    """Compatibility adapter for the observed desktop history schema.

    Selects status, timestamps and whether structured questions exist; never
    returns prompt, answer, tool arguments, reasoning, credentials or titles.
    It is not an official stable SDK and fails visibly on schema changes.
    """
    def __init__(self, registry, codex_root=None):
        self.registry = registry
        self.root = Path(codex_root or os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        self.enabled = True
        self.health = "尚未检查"
        self._process_checked = 0
        self._alive = False
        self._registered = set()
        self._turn_ids = {}
        self._finished_at = {}
        self._db_signature=None;self._db_cached_at=0;self._db_rows=[]

    def process_alive(self):
        now = time.monotonic()
        if now - self._process_checked >= 10:
            self._process_checked = now
            try:
                if os.name == "nt":
                    from host_process import running
                    self._alive = running('codex.exe')
                else:
                    self._alive = False
            except (OSError, subprocess.TimeoutExpired):
                self._alive = False
        return self._alive

    def read_states(self, now=None):
        now = time.time() if now is None else now
        db = self.root / "thread_history_1.sqlite"
        if not db.exists():
            raise DeviceError("未找到兼容的 Codex 本地任务数据库，可改用通用接口")
        conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=.2)
        try:
            conn.execute("PRAGMA query_only=ON")
            # Current/recent sessions only, not historical errors from previous days.
            turns = conn.execute("""SELECT thread_id,turn_id,status,started_at,completed_at
                FROM thread_turns WHERE started_at >= ? ORDER BY started_at DESC LIMIT 128""", (now-86400,)).fetchall()
            seen = set()
            result = []
            for thread_id, turn_id, status, started, completed in turns:
                if thread_id in seen:
                    continue
                seen.add(thread_id)
                latest, question, user, approval = conn.execute("""SELECT
                    MAX(MAX(created_at_ms,COALESCE(completed_at_ms,0))),
                    MAX(CASE WHEN item_type='agentMessage' AND json_array_length(json_extract(item_json,'$.questions'))>0 THEN rollout_ordinal END),
                    MAX(CASE WHEN item_type='userMessage' THEN rollout_ordinal END),
                    MAX(CASE WHEN json_extract(item_json,'$.status') IN ('waitingForApproval','waitingForInput') THEN rollout_ordinal END)
                    FROM thread_items WHERE thread_id=? AND turn_id=?""", (thread_id, turn_id)).fetchone()
                activity = max(started or 0, completed or 0, (latest or 0)/1000)
                if now-activity > (900 if status == 'inProgress' else 3600):
                    continue
                if status == 'failed':
                    state = 'error'
                elif status in ('inProgress', 'completed') and (
                    (question is not None and question > (user if user is not None else -1)) or
                    (approval is not None and status == 'inProgress')):
                    state = 'waiting'
                elif status == 'inProgress':
                    state = 'running'
                elif status == 'completed':
                    state = 'idle'
                else:
                    # Interrupted/unknown is not a successful completion.
                    state = 'offline'
                result.append((thread_id, state))
                self._turn_ids[thread_id] = turn_id
                self._finished_at[thread_id] = completed or 0
            return result
        finally:
            conn.close()

    def poll(self):
        try:
            if not self.enabled:
                self.health = "已暂停"
                rows = []
            elif not self.process_alive():
                self.health = "未检测到运行中的 Codex"
                rows = []
            else:
                path=self.root/'thread_history_1.sqlite'
                sig=[]
                for file in (path,path.with_name(path.name+'-wal')):
                    try:q=file.stat();sig.extend((q.st_mtime_ns,q.st_size))
                    except FileNotFoundError:sig.extend((0,0))
                tick=time.monotonic()
                if tuple(sig)!=self._db_signature or tick-self._db_cached_at>8:
                    rows=self.read_states()
                    self._db_signature=tuple(sig);self._db_cached_at=tick;self._db_rows=rows
                else:rows=self._db_rows
                self.health = f"本地只读监听正常 · {len(rows)} 个近期会话"
            active = {session for session, state in rows}
            for session in self._registered - active:
                self.registry.event(dict(source='Codex-local', session=session, state='remove'))
            self._registered = active
            for session, state in rows:
                self.registry.event(dict(source='Codex-local', session=session, state=state,
                                         event_id=self._turn_ids[session], ttl=15, completed=state == 'idle'),
                    observed_completion_at=self._finished_at.get(session,0))
        except (OSError, sqlite3.Error, DeviceError):
            self.health = "本地状态读取失败；旧状态将失效，可使用通用接口"


class AiCoordinator:
    def __init__(self, lights, connection_lock, log, demo=False, preferences=None, local_only=False):
        self.lights = lights
        self.connection_lock = connection_lock
        self.log = log
        self.preferences = preferences or LampPreferences()
        self.screen_preferences = ScreenPreferences()
        self.screen_applied = None
        self.screen_sent = 0
        self.registry = StateRegistry(ack_path=None if demo else endpoint_path().with_name('ai-acknowledged.json'))
        self.observer = CodexObserver(self.registry)
        self.demo = demo
        self.local_only = local_only
        self.enabled = False
        self.preview = None
        self.pending_off = False
        self.manual_off = False
        self.applied = ""
        self.last_send = 0
        self.sound_after=0
        self.sound_since=time.time()
        self.sound_next=0
        self.last_observe = 0
        self.ui_recent=lambda:True
        self.done_mode=False
        self.stop_event = threading.Event()
        self.worker = None

    def start(self):
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise DeviceError("enabled 必须为布尔值")
        if enabled and not self.lights.snapshot()["connected"]:
            raise DeviceError("请先在灯控调试中连接 Nano")
        if self.enabled != enabled:
            self.sound_after=self.registry.finishes()[0]
            self.sound_since=time.time()
        self.enabled = enabled
        self.done_mode=False
        self.preview = None
        self.pending_off = not enabled
        self.manual_off = not enabled
        self.applied = ""
        self.log("AI 状态自动跟随已开启" if enabled else "AI 跟随已暂停，状态灯将熄灭")

    def detach(self):
        self.enabled = False
        self.reset_link_state()

    def reset_link_state(self):
        # Drop old-device previews, pending OFF and cached writes, not the
        # user's automatic-follow toggle.
        self.done_mode=False
        self.preview = None
        self.pending_off = False
        self.manual_off = False
        self.applied = ""
        self.screen_applied = None

    def demonstrate(self, state):
        if not isinstance(state, str) or state not in PRIORITY:
            raise DeviceError("不支持的演示状态")
        if not self.lights.snapshot()["connected"]:
            raise DeviceError("请先连接灯控")
        self.preview = {"state": state, "until": time.time()+8}
        self.sound_since=self.preview["until"]
        self.applied = ""

    def configure(self, patterns):
        self.preferences.save(patterns)
        self.applied = ""
        self.log("灯色与灯效已保存，当前状态将应用新设置")

    def acknowledge_errors(self, events):
        with self.connection_lock:
            self.registry.acknowledge_errors(events)
            self.applied = ""
            self.log("已查看，恢复正常：已解除所查看异常的灯光报警，继续跟随其他会话")

    def snapshot(self):
        value = self.registry.snapshot()
        preview = self.preview if self.preview and self.preview['until'] > time.time() else None
        return {**value, "enabled": self.enabled, "preview": preview,
                "patterns": self.preferences.snapshot(), "settings_warning": self.preferences.warning,
                "screen": self.screen_preferences.snapshot(), "screen_warning": self.screen_preferences.warning,
                "observer": self.observer.health, "codex_enabled": self.observer.enabled,
                "applied": self.applied, "target": preview['state'] if preview else value['state'] if self.enabled else 'offline'}

    def _next_completion(self,light):
        floor=max(self.sound_since,light.get('sound_ready_at',0),time.time()-120)
        for seq, stamp in self.registry.finishes(self.sound_after)[1]:
            if stamp<floor:self.sound_after=seq;continue
            return seq
        return None

    def _sound_step(self, light, now):
        # Never trigger from lamp color or idle heartbeats. Use completed events
        # and the real Codex completion timestamp to suppress historical imports.
        if not (self.enabled and not self.preview and light.get('transport') in ('serial','tcp')
                and light.get('sound_v2') and light.get('connected')):
            self.sound_after=self.registry.finishes()[0]
            return False
        if now < self.sound_next:return False
        seq=self._next_completion(light)
        if seq is None:return False
        try:self.lights.start('light_sound',{'op':'state_finish' if light.get('state_voice') else 'finish'})
        except DeviceError:return False
        self.sound_after=seq;self.sound_next=now+2
        return True

    def _loop(self):
        while not self.stop_event.wait(.25):
            now = time.monotonic()
            if not self.demo and (self.enabled or self.preview or self.ui_recent()) and now-self.last_observe >= 2:
                self.last_observe = now
                self.observer.poll()
            with self.connection_lock:
                if self.preview and self.preview['until'] <= time.time():
                    self.preview = None
                    if not self.enabled:
                        self.pending_off = True
                l = self.lights.snapshot()
                if not l['connected'] or l['busy']:
                    if not l['connected']:
                        self.screen_applied=None;self.done_mode=False
                    continue
                target = self.snapshot()['target']
                # Modern C3: a true completion uses DONE, not an IDLE transition plus
                # an extra generic finish sample. Keep DONE through its idle lease.
                # While other tasks are active, do not replace the aggregate lamp state.
                if target!='idle' or not l.get('state_voice'):self.done_mode=False
                finishing=self._next_completion(l) if (target=='idle' and not self.done_mode and self.enabled and not self.preview and l.get('state_voice')) else None
                if (self.enabled or self.preview or self.pending_off) and (target != self.applied or now-self.last_send >= 5 or finishing is not None):
                    try:
                        if self.pending_off and self.manual_off:
                            # An explicit pause must extinguish even a voice-held
                            # lamp. Ordinary offline heartbeats preserve that hold.
                            self.lights.start('light_send', {'command': 'OFF'})
                        else:
                            wire='DONE' if target=='idle' and not self.preview and (self.done_mode or finishing is not None) else target.upper()
                            self.lights.start('light_ai', {'state': wire, **self.preferences.snapshot()[target], 'quiet': target == self.applied})
                    except DeviceError:
                        continue
                    if finishing is not None:
                        self.done_mode=True;self.sound_after=finishing;self.sound_next=now+2
                    self.applied = target
                    self.last_send = now
                    self.pending_off = False
                    self.manual_off = False
                    continue
                if self._sound_step(l,now):continue
                if self.local_only or l.get('screen_supported') is False:
                    continue
                screen = {**self.screen_preferences.snapshot(), 'state': screen_target(self.snapshot())}
                # Same worker/connection serializes screen commands and AI heartbeats.
                if screen != self.screen_applied or ((self.enabled or self.preview) and now-self.screen_sent >= 5):
                    try:self.lights.start('light_screen', screen)
                    except DeviceError:continue
                    self.screen_applied = screen
                    self.screen_sent = now

    def shutdown(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(4)
        # Nano lease handles process crashes too, without relying on cleanup writes.


def endpoint_path():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'HLK-M30' / 'ai-endpoint.json'


def publish_endpoint(origin, token):
    path = endpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'url': origin, 'token': token, 'pid': os.getpid()}), encoding='utf-8')
    temporary.replace(path)
