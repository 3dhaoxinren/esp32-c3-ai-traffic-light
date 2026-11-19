"""Offline package check: no device access, browser launch, startup registry or saved profile writes."""
import io,json,threading,time,urllib.error,urllib.request,uuid,wave
from pathlib import Path
from desktop_tray import DesktopTray
import desktop_tray

def run(AppServer,out):
    assets=None;t=None
    old=desktop_tray.window_class
    desktop_tray.window_class=lambda:'AILamp.WinPortableCheck.'+uuid.uuid4().hex
    s=AppServer(demo=True)
    # The demo server reads no user state that is included in the report and never publishes an endpoint.
    w=threading.Thread(target=s.serve_forever,daemon=True);w.start()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    uid=s.token
    def get(path,obj=None):
        headers={'X-Session-Token':uid}
        body=obj
        if isinstance(obj,dict):
            headers['Content-Type']='application/json';body=json.dumps(obj).encode()
        if path=='/api/audio_import':
            headers.update({'Content-Type':'application/octet-stream','X-Audio-Name':'self-test.wav'})
        with opener.open(urllib.request.Request(s.origin+path,data=body,headers=headers),timeout=4) as f:
            return f.read()
    try:
        for path in ('/','/app.js','/audio.js','/discovery.js','/style.css','/unified.css','/Nano_HLK_M30.ino'):
            assert len(get(path))>100,path
        assert b'3.0.0' in get('/') and b'id="tab-nano"' not in get('/')
        assert b'id="advanced-at-toggle"' in get('/') and b'id="discover-lan"' in get('/')
        # Unique window class even when the user's real tray application is running.
        key='AILamp.WinPortableCheck.'+uuid.uuid4().hex
        desktop_tray.window_class=lambda:key
        t=DesktopTray(s);t.start()
        assert t.hwnd is not None and t.hwnd!=0
        state=json.loads(get('/api/state'));assert state['app_version']=='3.0.0'
        seed=bytes(range(48))*180
        temp=io.BytesIO()
        with wave.open(temp,'wb') as f:
            f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(seed)
        get('/api/audio_import',temp.getvalue())
        until=time.monotonic()+5
        while True:
            q=json.loads(get('/api/audio'))
            if not q['busy']:break
            if time.monotonic()>until:raise TimeoutError('audio import stopped responding')
            time.sleep(.03)
        assert not q['error'] and q['source']['frames']==len(seed)//2
        p=io.BytesIO(get('/api/audio_preview',{'source':q['source']['id'],'start':16,'end':257,'slot':2}))
        with wave.open(p,'rb') as f:assert f.readframes(241)==seed[32:514]
        d=json.loads(get('/api/discover'));assert not d['busy']
        report=dict(version='3.0.0',packaged=bool(getattr(__import__('sys'),'frozen',False)),
                    localhost_assets=True,win32_tray_registered=True,demo_audio_import_and_cut=True,
                    discovery_ui_available=True,nano_wiring_page_absent=True,device_untouched=True)
        Path(out).write_text(json.dumps(report,sort_keys=True,indent=2)+'\n',encoding='utf8')
    finally:
        if t:t.stop()
        s.shutdown();s.server_close()
        desktop_tray.window_class=old
