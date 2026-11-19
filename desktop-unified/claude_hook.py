"""Optional Claude Code hook adapter. Emits no stdout, never blocks AI work."""
import json
import sys

from ai_signal import send_event


def map_event(event):
    name=event.get('hook_event_name')
    if name in ('UserPromptSubmit','PreToolUse','PostToolUse'):
        return 'running', 900
    if name=='Notification' and event.get('notification_type') in ('permission_prompt','idle_prompt'):
        return 'waiting', 3600
    if name=='PermissionRequest':
        return 'waiting', 3600
    if name=='Stop':
        return 'idle', 3600
    if name=='StopFailure':
        return 'error', 3600
    if name=='SessionEnd':
        return 'offline', 30
    return None


def main():
    try:
        raw=sys.stdin.read(65537)
        if len(raw)>65536:return
        event=json.loads(raw)
        status=map_event(event)
        session=event.get('session_id')
        if status and isinstance(session,str) and session:
            send_event('Claude',session,*status)
    except Exception:
        # A failed lamp must never turn a successful AI hook into a failed action.
        pass


if __name__=='__main__':main()
