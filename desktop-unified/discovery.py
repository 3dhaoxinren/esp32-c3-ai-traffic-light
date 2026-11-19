"""On-demand discovery; no password storage, auto-connect or background network sweep."""
import concurrent.futures as cf, ipaddress as ip, json, os, re, socket, subprocess, threading, time

LAN=(ip.IPv4Network('10.0.0.0/8'),ip.IPv4Network('172.16.0.0/12'),ip.IPv4Network('192.168.0.0/16'))
def hidden(args,timeout=8):
    try:p=subprocess.run(args,capture_output=True,timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except (OSError,subprocess.TimeoutExpired) as e:raise ValueError('Windows 网络查询没有完成。') from e
    if p.returncode:raise ValueError('Windows 拒绝或未完成网络查询；也可手动输入。')
    for enc in ('utf-8-sig','gb18030','oem'):
        try:return p.stdout.decode(enc)
        except (UnicodeError,LookupError):pass
    return p.stdout.decode(errors='replace')

def parse_wifi(text):
    seen={}; rec=None;bss=None
    def flush_bss():
        if rec and bss and bss.get('channel'):
            ch=bss['channel']
            rec['channels'].append(ch)
            band=bss.get('band')
            if band=='2.4' or (band is None and 1<=ch<=14):rec['has_24']=True
    def finish():
        flush_bss()
        if rec and rec['ssid'] and len(rec['ssid'].encode('utf8'))<=32:
            n=rec['ssid'];old=seen.get(n)
            if old:
                old['channels']=sorted(set(old['channels']+rec['channels']))
                old['signal']=max(old['signal'],rec['signal'])
                old['has_24']|=rec['has_24']
            else:seen[n]=rec
    for line in text.splitlines():
        if m:=re.match(r'^\s*SSID\s+\d+\s*[:：]\s*(.*)$',line,re.I):
            finish();rec={'ssid':m[1],'signal':0,'channels':[],'has_24':False};bss={};continue
        if not rec:continue
        if re.match(r'^\s*BSSID\s+\d+\s*[:：]',line,re.I):
            flush_bss();bss={};continue
        if m:=re.match(r'^\s*(?:Signal|信号)\s*[:：]\s*(\d+)\s*%',line,re.I):
            rec['signal']=max(rec['signal'],int(m[1]))
        if m:=re.match(r'^\s*(?:Band|频段|频带)\s*[:：]\s*(\d+(?:\.\d+)?)',line,re.I):
            bss['band']=m[1]
        if m:=re.match(r'^\s*(?:Channel|信道|频道)\s*[:：]\s*(\d+)',line,re.I):
            bss['channel']=int(m[1])
    finish()
    return sorted(seen.values(),key=lambda x:(not x['has_24'],-x['signal'],x['ssid'].lower()))[:64]

def request_wlan_scan():
    # WlanScan is requested only on click; netsh is used solely to read SSIDs afterwards.
    import ctypes as c
    from ctypes import wintypes as w
    class Guid(c.Structure):
        _fields_=[('d1',c.c_uint32),('d2',c.c_uint16),('d3',c.c_uint16),('d4',c.c_ubyte*8)]
    class Iface(c.Structure):
        _fields_=[('id',Guid),('desc',c.c_wchar*256),('state',c.c_int)]
    api=c.WinDLL('wlanapi');p=c.c_void_p;D=w.DWORD;H=w.HANDLE;R=c.POINTER
    sig={'WlanOpenHandle':[D,p,R(D),R(H)],'WlanEnumInterfaces':[H,p,R(p)],
         'WlanCloseHandle':[H,p],'WlanFreeMemory':[p],'WlanScan':[H,R(Guid),p,p,p]}
    for n,a in sig.items():
        f=getattr(api,n);f.argtypes=a;f.restype=None if n=='WlanFreeMemory' else D
    ver=D();h=H();l=p()
    if api.WlanOpenHandle(2,None,c.byref(ver),c.byref(h)):return False
    try:
        if api.WlanEnumInterfaces(h,None,c.byref(l)):return False
        count=D.from_address(l.value).value
        ok=False
        for i in range(min(count,16)):
            data=Iface.from_address(l.value+8+i*c.sizeof(Iface))
            if api.WlanScan(h,c.byref(data.id),None,None,None)==0:ok=True
        return ok
    finally:
        if l:api.WlanFreeMemory(l)
        api.WlanCloseHandle(h,None)

def wifi():
    if os.name!='nt':raise ValueError('附近 Wi‑Fi 列表仅支持 Windows。')
    try:refresh=request_wlan_scan()
    except (OSError,ValueError):refresh=False
    if refresh:time.sleep(4.05)
    text=hidden(['netsh.exe','wlan','show','networks','mode=bssid'])
    a=parse_wifi(text)
    if not a and re.search(r'location|位置|denied|拒绝',text,re.I):raise ValueError('Windows 限制了 WLAN 扫描。可授权位置访问，或手动输入 Wi-Fi 名称。')
    return a

def local_routes():
    if os.name!='nt':raise ValueError('本地网段自动识别仅支持 Windows。')
    expr=r"""
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);
@(Get-NetIPConfiguration | Where-Object {
  $_.NetAdapter.Status -eq 'Up' -and $_.IPv4DefaultGateway -and $_.IPv4Address
} | ForEach-Object {
  foreach($a in $_.IPv4Address){ [pscustomobject]@{address=$a.IPAddress;prefix=$a.PrefixLength;physical=$_.NetAdapter.HardwareInterface} }
}) | ConvertTo-Json -Compress
"""
    text=hidden(['powershell.exe','-NoProfile','-NonInteractive','-Command',expr])
    if not text.strip():return []
    x=json.loads(text);return x if isinstance(x,list) else [x]

def candidates(rows):
    allowed=[];selfs=set();out=[]
    for row in rows:
        try:
            if row.get('physical') is not True:continue
            address=ip.IPv4Address(row['address']);n=int(row['prefix'])
            if not any(address in block for block in LAN) or not 1<=n<=30:continue
            net=ip.IPv4Network((address,max(n,24)),strict=False)
        except (KeyError,ValueError,TypeError):continue
        selfs.add(str(address))
        if net not in allowed and len(allowed)<2:allowed.append(net)
    for net in allowed:
        out.extend(str(a) for a in net.hosts() if str(a) not in selfs)
    return out

def probe(host,port=8080,timeout=.35,connect=socket.create_connection):
    try:
        with connect((host,port),timeout=timeout) as s:
            s.settimeout(.45)
            s.sendall(b'LIGHT STATUS\r\n')
            buf=b'';caps=False;end=time.monotonic()+1.1
            while len(buf)<900 and time.monotonic()<end:
                b=s.recv(192)
                if not b:break
                buf+=b
                while b'\n' in buf:
                    raw,buf=buf.split(b'\n',1);line=raw.strip().decode('ascii','replace')
                    if re.fullmatch(r'CAPS TOY_SOUND_(?:V2|LAN_V1) count=\d{1,2}',line):caps=True
                    if re.fullmatch(r'(?:OK LIGHT mode=(?:MANUAL|AUTO) color=(?:OFF|RED|YELLOW|GREEN)|OK AI state=(?:IDLE|RUNNING|WAITING|ERROR|OFFLINE|DONE)|OK VOICE hold=1 ai=(?:IDLE|RUNNING|WAITING|ERROR|OFFLINE) color=(?:OFF|RED|YELLOW|GREEN)) R=[01] Y=[01] G=[01](?: level=\d{1,3} lease=15000(?: color=(?:OFF|RED|YELLOW|GREEN) effect=(?:SOLID|BREATHE|SLOW|FAST))?)?',line):
                        return {'ip':host,'port':port,'kind':'ESP32-C3' if caps else 'Nano / 兼容灯板'}
    except OSError:pass
    return None

def lamps(rows,active=None,scanner=probe):
    ips=candidates(rows)
    if not ips and not active:raise ValueError('没有找到带默认网关的私有局域网。可手动输入灯板 IP。')
    found=[dict(active)] if active else []
    if active and active['ip'] in ips:ips.remove(active['ip'])
    with cf.ThreadPoolExecutor(max_workers=16) as t:
        found.extend(p for p in t.map(scanner,ips) if p)
    return sorted(found,key=lambda v:int(ip.IPv4Address(v['ip'])))

class Discovery:
    def __init__(self,server):
        self.server=server;self.lock=threading.Lock();self.state={'busy':False,'kind':'','results':[],'error':'','seq':0}
    def snapshot(self):
        with self.lock:return dict(self.state)
    def start(self,kind):
        if kind not in ('wifi','lamps'):raise ValueError('未知搜索类型。')
        if self.server.controller.demo:raise ValueError('演示模式不扫描真实网络。')
        with self.lock:
            if self.state['busy']:raise ValueError('已有搜索正在进行。')
            self.state.update(busy=True,kind=kind,results=[],error='',seq=self.state['seq']+1)
        def job():
            try:
                if kind=='wifi':result=wifi()
                else:
                    s=self.server.lights.snapshot();active=None
                    if s.get('connected') and s.get('transport')=='tcp':
                        a,sep,p=s.get('endpoint','').partition(':')
                        if sep and p=='8080' and re.fullmatch(r'[0-9.]+',a):
                            active={'ip':a,'port':8080,'kind':'ESP32-C3' if s.get('sound_v2') else 'Nano / 兼容灯板','active':True}
                    result=lamps(local_routes(),active)
            except (ValueError,OSError) as e:
                with self.lock:self.state.update(busy=False,error=str(e))
            except Exception:
                with self.lock:self.state.update(busy=False,error='本机网络搜索没有完成。')
            else:
                with self.lock:self.state.update(busy=False,results=result)
        threading.Thread(target=job,name='on-demand-discovery',daemon=True).start()
