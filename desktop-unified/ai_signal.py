"""Send only status metadata to the local workbench, no AI API key required."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request
import uuid


def send_event(source, session, state, ttl=120):
    path = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'HLK-M30' / 'ai-endpoint.json'
    info = json.loads(path.read_text(encoding='utf-8'))
    # The endpoint file is local user configuration; never send its token off-host.
    from urllib.parse import urlsplit
    parsed = urlsplit(info['url'])
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password:
        raise ValueError('Invalid local workbench endpoint')
    req = urllib.request.Request(info['url'] + '/api/ai_event',
        data=json.dumps(dict(source=source,session=session,state=state,ttl=ttl)).encode(),
        headers={'Content-Type':'application/json', 'X-AI-Token':info['token']})
    with urllib.request.urlopen(req, timeout=3) as response:
        return json.load(response)


def run_tracked(source, session, command):
    """Wrap a CLI job. Heartbeats preserve externally reported WAITING/ERROR."""
    stopped=threading.Event()
    def beat():
        while not stopped.wait(15):
            try: send_event(source,session,'heartbeat',60)
            except (OSError,ValueError): pass
    try: send_event(source,session,'running',60)
    except (OSError,ValueError):
        print('Status lamp unavailable; continuing the AI command.',file=sys.stderr)
    worker=threading.Thread(target=beat,daemon=True);worker.start()
    code=1
    try:
        code=subprocess.call(command, shell=False)
        return code
    finally:
        stopped.set();worker.join(4)
        try:send_event(source,session,'idle' if code==0 else 'error',3600)
        except (OSError,ValueError):pass


def main():
    p=argparse.ArgumentParser(description='Report an AI session state to the Nano status light')
    p.add_argument('state',nargs='?',choices=['idle','running','waiting','error','offline','heartbeat','remove'])
    p.add_argument('--source',default='custom-ai');p.add_argument('--session',default='main');p.add_argument('--ttl',type=int,default=120)
    p.add_argument('--run',nargs=argparse.REMAINDER,help='Run a CLI command, report start/exit and keep a lease')
    a=p.parse_args()
    try:
        if a.run:
            return run_tracked(a.source, a.session if a.session!='main' else str(uuid.uuid4())[:12],a.run)
        if not a.state:p.error('state or --run is required')
        send_event(a.source,a.session,a.state,a.ttl)
        print('AI status sent:',a.source,a.session,a.state)
        return 0
    except (OSError,ValueError,KeyError) as error:
        print('Unable to report AI state; open the workbench first. '+type(error).__name__,file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
