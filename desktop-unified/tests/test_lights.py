import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from core import DeviceError
from light_control import LightController
from server import AppServer


class LightTests(unittest.TestCase):
    def tcp_peer(self, chunks):
        client, peer = socket.socketpair()
        client.settimeout(.02)
        received = []

        def respond():
            try:
                payload = b""
                while not payload.endswith(b"\r\n"):
                    payload += peer.recv(1024)
                received.append(payload)
                for chunk in chunks:
                    peer.sendall(chunk)
                    time.sleep(.005)
                # Keep the link alive as a real persistent lamp connection.
                while peer.recv(1024):
                    pass
            finally:
                peer.close()

        worker = threading.Thread(target=respond, daemon=True)
        worker.start()
        ctrl = LightController(lambda *args: None)
        ctrl.link = client
        ctrl._set(connected=True, transport="tcp")
        self.addCleanup(ctrl.shutdown)
        return ctrl, received

    def run_command(self, ctrl, command):
        ctrl.start("light_send", {"command": command})
        ctrl.worker.join(5)
        self.assertFalse(ctrl.snapshot()["busy"])
        return ctrl.snapshot()

    def test_fragmented_reply_and_unrelated_boot_text(self):
        c, writes = self.tcp_peer([b"boot ready\r\nOK LI", b"GHT mode=MANUAL color=RED R=1 ", b"Y=0 G=0\r", b"\n"])
        s = self.run_command(c, "RED")
        self.assertEqual(s["result"], "success")
        self.assertEqual(s["pins"], [1, 0, 0])
        self.assertEqual(writes, [b"LIGHT RED\r\n"])
        self.assertGreater(s["updated"], 0)

    def test_echo_is_not_a_success_reply(self):
        c, _ = self.tcp_peer([b"LIGHT RED\r\nERR LIGHT unsupported\r\n"])
        s = self.run_command(c, "RED")
        self.assertEqual(s["result"], "error")
        self.assertFalse(s["connected"])
        self.assertEqual(s["updated"], 0)

    def test_mismatched_pins_do_not_report_success(self):
        c, _ = self.tcp_peer([b"OK LIGHT mode=MANUAL color=RED R=0 Y=0 G=1\r\n"])
        s = self.run_command(c, "RED")
        self.assertEqual(s["result"], "error")
        self.assertIsNone(s["pins"])

    def test_wrong_command_ack_is_not_success(self):
        c, _ = self.tcp_peer([b"OK LIGHT mode=MANUAL color=GREEN R=0 Y=0 G=1\r\n"])
        self.assertEqual(self.run_command(c, "RED")["result"], "error")

    def test_connect_requires_nano_reply(self):
        c, writes = self.tcp_peer([b"OK LIGHT mode=MANUAL color=OFF R=0 Y=0 G=0\r\n"])
        link = c.link
        c.link = None
        c._set(connected=False)
        with patch("light_control.socket.create_connection", return_value=link):
            c.start("light_connect", {"transport": "tcp", "host": "192.168.1.100", "tcp_port": 8080})
            c.worker.join(5)
        self.assertTrue(c.snapshot()["connected"])
        self.assertEqual(writes, [b"LIGHT STATUS\r\n"])

    def test_injection_rejected_before_any_write(self):
        c = LightController(lambda *args: None)
        c._set(connected=True)
        for command in ("RED\rLIGHT AUTO", "at+Df=1", "BLUE", None, []):
            with self.assertRaises(DeviceError):
                c.start("light_send", {"command": command})
        with self.assertRaises(DeviceError):
            c.start("light_connect", {"transport": "tcp", "host": "8.8.8.8", "tcp_port": 8080})

    def test_demo_never_opens_hardware(self):
        c = LightController(lambda *args: None, demo=True)
        with self.assertRaises(DeviceError):
            c.start("light_connect", {"transport": "serial", "port": "COM3"})

    def test_at_and_lamp_connections_are_mutually_exclusive(self):
        server = AppServer()
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def request(action, data):
            req = urllib.request.Request(server.origin + "/api/" + action,
                data=json.dumps(data).encode(), headers={"Content-Type": "application/json", "X-Session-Token": server.token})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(req, timeout=2)
            self.assertEqual(error.exception.code, 409)
        try:
            server.controller._set(connected=True)
            request("light_connect", {"transport": "tcp", "host": "192.168.1.100", "tcp_port": 8080})
            server.controller._set(connected=False)
            server.lights._set(connected=True)
            request("connect", {"transport": "udp", "host": "192.168.1.100"})
        finally:
            server.shutdown()
            server.lights.shutdown()
            server.controller.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
