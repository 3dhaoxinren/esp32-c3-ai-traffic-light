import hashlib
import hmac
import os
from pathlib import Path
import socketserver
import struct
import tempfile
import threading
import unittest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import lan_readback as lr

MASTER=bytes(range(32))
NONCE=bytes(range(12))
SLOT=3
PCM=bytes(range(256))*8+b'\0\1'

class Vault:
    def __init__(self,key=MASTER):
        self.key=key
    def load(self,fingerprint):
        return self.key if fingerprint==lr.key_id(MASTER) else None

class Readback(unittest.TestCase):
    def loopback(self,variant):
        record=[]
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                record.append(self.rfile.readline().strip())
                if variant=='unpaired':
                    self.wfile.write(b'ERR AUDX pair via USB\n');return
                self.wfile.write(f'OK AUDX id={lr.key_id(MASTER)} nonce={NONCE.hex().upper()}\n'.encode())
                request=self.rfile.readline().strip()
                if not request:return
                record.append(request)
                context=NONCE+struct.pack('!HI',SLOT,len(PCM))
                wanted=hmac.digest(MASTER,b'ailamp-read-auth-v1'+context,'sha256').hex().upper()
                if request!=f'AUDX GET {SLOT} {len(PCM)} {wanted}'.encode():
                    self.wfile.write(b'ERR AUDX auth\n');return
                if variant=='changed':
                    self.wfile.write(b'ERR AUDX changed\n');return
                crypt=AESGCM(hmac.digest(MASTER,b'ailamp-read-key-v1'+NONCE,'sha256'))
                # Coalesce the ASCII header with encrypted binary frames, as TCP may.
                wire=f'OK AUDX bytes={len(PCM)}\n'.encode()
                for index,off in enumerate(range(0,len(PCM),1024)):
                    seq=struct.pack('!I',index)
                    frame=crypt.encrypt(b'\x00'*8+seq,PCM[off:off+1024],context+seq)
                    if variant=='tampered' and index==1:frame=frame[:20]+bytes([frame[20]^1])+frame[21:]
                    wire+=frame
                self.wfile.write(wire if variant!='truncated' else wire[:-8])
        tcp=socketserver.TCPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=tcp.serve_forever,daemon=True);thread.start()
        self.addCleanup(tcp.server_close);self.addCleanup(tcp.shutdown)
        return tcp.server_address,record

    def test_authenticated_export_and_progress(self):
        peer,record=self.loopback('good')
        progress=[]
        self.assertEqual(lr.read(peer,SLOT,len(PCM),progress.append,Vault()),PCM)
        self.assertEqual(len(record),2)
        self.assertEqual(progress[-1],100)
        self.assertNotIn(MASTER.hex().upper().encode(),b' '.join(record))

    def test_unpaired_and_unrecognized_pc_dont_request_recording(self):
        for mode in ('unpaired','good'):
            with self.subTest(mode=mode):
                peer,record=self.loopback(mode)
                with self.assertRaises(lr.PairRequired):
                    lr.read(peer,SLOT,len(PCM),lambda _:None,Vault(None))
                self.assertEqual(record,[b'AUDX CHALLENGE'])

    def test_tampered_and_truncated_are_not_returned(self):
        for mode,kind in [('tampered',RuntimeError),('truncated',OSError),
                          ('changed',ValueError)]:
            with self.subTest(mode=mode):
                peer,_=self.loopback(mode)
                with self.assertRaises(kind):lr.read(peer,SLOT,len(PCM),lambda _:None,Vault())

    def test_wrong_key_refused(self):
        peer,_=self.loopback('good')
        with self.assertRaisesRegex(RuntimeError,'未授权'):
            lr.read(peer,SLOT,len(PCM),lambda _:None,Vault(b'Q'*32))

    @unittest.skipUnless(os.name=='nt','Windows protected storage')
    def test_windows_protected_key_store(self):
        with tempfile.TemporaryDirectory() as dir:
            vault=lr.KeyStore(dir);vault.save(MASTER)
            paths=list(Path(dir).iterdir())
            self.assertEqual(len(paths),1)
            self.assertNotIn(MASTER,paths[0].read_bytes())
            self.assertEqual(lr.KeyStore(dir).load(lr.key_id(MASTER)),MASTER)
            self.assertIsNone(vault.load('../bad'))
