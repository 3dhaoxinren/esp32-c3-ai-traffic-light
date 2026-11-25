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
        assert b'3.1.5' in get('/') and b'id="tab-nano"' not in get('/')
        assert b'id="advanced-at-toggle"' in get('/') and b'id="discover-lan"' in get('/')
        assert b'id="startup-roam"' in get('/')
        # Unique window class even when the user's real tray application is running.
        key='AILamp.WinPortableCheck.'+uuid.uuid4().hex
        desktop_tray.window_class=lambda:key
        t=DesktopTray(s);t.start()
        assert t.hwnd is not None and t.hwnd!=0
        import win32gui as g
        import win32con as c
        menu = g.CreatePopupMenu()
        try:
            assert t._append_serial_menu(menu, s.lights.snapshot()) == {}
            popup = g.GetSubMenu(menu, 0)
            assert g.GetMenuItemCount(popup) == 1
            assert g.GetMenuState(popup, 0, c.MF_BYPOSITION) & c.MF_GRAYED
        finally:
            g.DestroyMenu(menu)
        from serial_notifications import WindowsSerialNotifications
        watcher = WindowsSerialNotifications(lambda *_: None)
        try:
            assert watcher.handle
        finally:
            watcher.close()
        assert not watcher.handle
        from ai_status import visible_status_alerts
        s.lights._set(connected=True)
        s.ai.enabled = True
        event = dict(source='self-test', session='waiting', state='waiting', event_id='test-1')
        s.ai.registry.event(event)
        s.ai.reset_status_display(visible_status_alerts(s.ai.snapshot()))
        assert s.ai.snapshot()['target'] == 'idle' and s.ai.enabled
        s.ai.registry.event({**event, 'event_id':'test-2'})
        assert s.ai.snapshot()['target'] == 'waiting'
        s.ai.registry.clock = lambda:1000
        same_turn = {**event, 'session':'execution', 'state':'running'}
        s.ai.registry.event(same_turn, observed_execution=(10,999000))
        s.ai.reset_status_display(visible_status_alerts(s.ai.snapshot()))
        s.ai.registry.event(same_turn, observed_execution=(10,999000))
        assert s.ai.snapshot()['target'] == 'idle'
        s.ai.registry.clock = lambda:1001
        s.ai.registry.event(same_turn, observed_execution=(11,1000500))
        assert s.ai.snapshot()['target'] == 'running' and s.ai.enabled
        s.ai.registry.clock = time.time
        s.lights._set(connected=False)
        s.ai.enabled = False
        state=json.loads(get('/api/state'));assert state['app_version']=='3.1.5'
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
        import audio_desk, edge_tts, miniaudio
        from ch340_serial import is_setcommstate_31
        assert is_setcommstate_31("Cannot configure port, something went wrong. Original message: PermissionError(13, 'test', None, 31)")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from tempfile import TemporaryDirectory
        from lan_readback import KeyStore,key_id
        assert b'id="aud-voice-listen"' in get('/') and b'id="aud-lan-read-pair"' in get('/')
        assert callable(edge_tts.Communicate)
        decoded=miniaudio.decode(temp.getvalue(),output_format=miniaudio.SampleFormat.SIGNED16,nchannels=1,sample_rate=16000)
        assert len(decoded.samples)==len(seed)//2
        key=bytes(range(32));nonce=b'\x01'*12
        crypto=AESGCM(key)
        assert crypto.decrypt(nonce,crypto.encrypt(nonce,seed,b'package-check'),b'package-check')==seed
        with TemporaryDirectory() as folder:
            store=KeyStore(folder);store.save(key)
            assert store.load(key_id(key))==key
        before=json.loads(get('/api/audio'))
        synth=audio_desk.synthesize
        try:
            audio_desk.synthesize=lambda text,profile,rate:(seed,'offline-fixture')
            voice=get('/api/audio_voice_preview',{'text':'QA','profile':'local','rate':0})
            with wave.open(io.BytesIO(voice),'rb') as w:assert w.readframes(w.getnframes())==seed
        finally:audio_desk.synthesize=synth
        assert json.loads(get('/api/audio'))==before
        d=json.loads(get('/api/discover'));assert not d['busy']
        report=dict(version='3.1.5',packaged=bool(getattr(__import__('sys'),'frozen',False)),
                    localhost_assets=True,win32_tray_registered=True,win32_usb_submenu=True,
                    windows_serial_notifications=True,reset_status_display=True,execution_rearms_same_turn=True,
                    demo_audio_import_and_cut=True,
                    discovery_ui_available=True,nano_wiring_page_absent=True,device_untouched=True,
                    text_audition_unchanged_source=True,encrypted_lan_dependencies=True,voice_packages_available=True)
        Path(out).write_text(json.dumps(report,sort_keys=True,indent=2)+'\n',encoding='utf8')
    finally:
        if t:t.stop()
        s.shutdown();s.server_close()
        desktop_tray.window_class=old
