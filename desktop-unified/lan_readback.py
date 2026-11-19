"""Read-only C3 clip export: physical pairing, single-use challenge, authenticated frames."""
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import tempfile
import time

import device as d

AUTH=b'ailamp-read-auth-v1'
KEY=b'ailamp-read-key-v1'
BLOCK=1024
TAG=16

class PairRequired(RuntimeError):pass

class KeyStore:
    """One protected blob for each device key, independent of DHCP addresses."""
    def __init__(self,root=None):
        self.root=Path(root) if root is not None else Path(os.environ.get('LOCALAPPDATA',Path.home()))/'HLK-M30'/'audio-readback'

    def load(self,fingerprint):
        if not re.fullmatch(r'[A-F0-9]{32}',fingerprint):return None
        p=self.root/(fingerprint+'.dpapi')
        try:
            with p.open('rb') as f:blob=f.read(8193)
            if len(blob)>8192:return None
            import win32crypt
            _,key=win32crypt.CryptUnprotectData(blob,None,None,None,1)
        except (FileNotFoundError,ImportError):
            return None
        except Exception as exc:
            raise PairRequired('无法读取本机配对资料，请通过原生 USB 重新配对。') from exc
        return key if len(key)==32 and key_id(key)==fingerprint else None

    def save(self,key):
        fingerprint=key_id(key)
        tmp=None
        try:
            import win32crypt
            blob=win32crypt.CryptProtectData(key,'AI Lamp paired audio readback',None,None,None,1)
            self.root.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.root,prefix='.pair-',delete=False) as f:
                tmp=Path(f.name);f.write(blob)
            tmp.replace(self.root/(fingerprint+'.dpapi'))
        except Exception as exc:
            raise RuntimeError('无法安全保存配对资料；请使用当前 Windows 账号重新配对。') from exc
        finally:
            if tmp:
                try:tmp.unlink(missing_ok=True)
                except OSError:pass

def key_id(key):
    return hashlib.sha256(key).hexdigest()[:32].upper()

def pair(f,vault=None):
    # The offer is only accepted on a physical USB link; it never reaches a log or web UI.
    d.send(f,'AUDX PAIR '+secrets.token_bytes(32).hex().upper())
    reply=d.response(f)
    m=re.fullmatch(r'OK AUDX key=([A-F0-9]{64})',reply)
    if not m:raise RuntimeError('设备未接受 USB 配对，请检查 C3 固件。')
    (vault or KeyStore()).save(bytes.fromhex(m[1]))

class Wire:
    def __init__(self,s):
        self.s=s;self.buffer=bytearray();self.end=time.monotonic()+60

    def recv(self):
        wait=self.end-time.monotonic()
        if wait<=0:raise TimeoutError('局域网回读超时，请重试。')
        self.s.settimeout(min(wait,4))
        b=self.s.recv(4096)
        if not b:raise OSError('C3 局域网回读提前中断。')
        self.buffer.extend(b)

    def send(self,line):
        self.s.sendall((line+'\n').encode('ascii'))

    def reply(self):
        while True:
            i=self.buffer.find(b'\n')
            if 0<=i<160:
                val=bytes(self.buffer[:i]).decode('ascii','replace').strip()
                del self.buffer[:i+1]
                return val
            if len(self.buffer)>=160:raise RuntimeError('C3 回读头部无效。')
            self.recv()

    def take(self,n):
        while len(self.buffer)<n:self.recv()
        data=bytes(self.buffer[:n]);del self.buffer[:n];return data

def read(peer,slot,size,progress,vault=None):
    if type(slot) is not int or not 1<=slot<=3000 or type(size) is not int or not 0<size<=d.LIMIT or size%2:
        raise ValueError('录音编号或大小无效；请刷新列表。')
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.exceptions import InvalidTag
    except ImportError as exc:
        raise RuntimeError('缺少加密回读组件，请安装新版客户端依赖。') from exc
    with socket.create_connection(peer,timeout=3) as s:
        s.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        w=Wire(s);w.send('AUDX CHALLENGE')
        reply=w.reply()
        if reply=='ERR AUDX pair via USB':raise PairRequired('这块 C3 尚未配对局域网回读。')
        m=re.fullmatch(r'OK AUDX id=([A-F0-9]{32}) nonce=([A-F0-9]{24})',reply)
        if not m:raise RuntimeError('C3 未返回有效的配对挑战。')
        master=(vault or KeyStore()).load(m[1])
        if master is None:raise PairRequired('这台电脑尚未与该 C3 配对，请接原生 USB 配对一次。')
        nonce=bytes.fromhex(m[2]);context=nonce+struct.pack('!HI',slot,size)
        proof=hmac.digest(master,AUTH+context,'sha256')
        w.send(f'AUDX GET {slot} {size} {proof.hex().upper()}')
        reply=w.reply()
        if reply=='ERR AUDX changed':raise ValueError('这段录音已变化，请刷新设备列表再试听。')
        if reply!=f'OK AUDX bytes={size}':raise RuntimeError('C3 未授权本次回读，请重新配对后再试。')
        crypt=AESGCM(hmac.digest(master,KEY+nonce,'sha256'))
        out=bytearray()
        for index,off in enumerate(range(0,size,BLOCK)):
            n=min(BLOCK,size-off);seq=struct.pack('!I',index)
            try:out.extend(crypt.decrypt(b'\x00'*8+seq,w.take(n+TAG),context+seq))
            except InvalidTag as exc:
                raise RuntimeError('局域网回读校验失败，没有播放任何音频。') from exc
            if not index%16:progress(len(out)*100//size)
        progress(100)
        return d.valid(bytes(out))
