import os
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from core import DeviceError
from desktop_tray import DesktopTray, FIRST_SERIAL_COMMAND
from server import AppServer
from startup import capture_target


def port(device, location):
    return NS(device=device, location=location, vid=0x1A86, pid=0x7523, serial_number="")


class TrayPortTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with patch.dict(os.environ, LOCALAPPDATA=folder.name):
            self.server = AppServer()
        self.addCleanup(self.server.server_close)
        self.tray = DesktopTray(self.server)
        self.light = self.server.lights
        self.old = Mock()
        self.light.link = self.old
        self.light._set(connected=True, transport="serial", endpoint="COM3 · 115200")
        self.ports = [port("COM3", "1-5"), port("COM10", "1-6")]
        self.server.startup.ports = lambda: self.ports
        self.target = capture_target({"transport": "serial", "port": "COM3"}, self.ports)
        self.server.startup.preferences.save(target=self.target, auto_connect=True, roam_usb=True)
        self.server.ai.enabled = True

    def test_connected_port_is_noop(self):
        with patch.object(self.server.startup, "manual_serial_switch") as switch:
            self.tray._switch_serial("COM3")
        switch.assert_not_called()
        self.old.close.assert_not_called()
        self.assertTrue(self.server.ai.enabled)

    def test_busy_at_audio_or_lights_does_not_switch(self):
        for module, state in (
            (self.server.controller, {"connected": True, "busy": False}),
            (self.server.controller, {"connected": False, "busy": True}),
            (self.light, {"connected": False, "busy": True}),
            (self.server.audio, {"busy": True}),
        ):
            with self.subTest(module=type(module).__name__), patch.object(module, "snapshot", return_value=state):
                with self.assertRaises(DeviceError):
                    self.tray._switch_serial("COM10")
        self.old.close.assert_not_called()

    def test_port_removed_after_menu_does_not_close_current_board(self):
        self.ports.pop()
        with self.assertRaises(DeviceError):
            self.tray._switch_serial("COM10")
        self.old.close.assert_not_called()
        self.assertTrue(self.light.snapshot()["connected"])
        self.assertIsNone(self.server.startup.pending)

    def switch(self, reply):
        active = Mock()
        active.write.side_effect = len
        self.server.ai.applied = "running"
        self.server.ai.preview = {"state": "waiting", "until": 9999999999}
        with (patch("light_control.list_ports.comports", return_value=self.ports),
              patch("light_control.CachedCH340Serial" if sys.platform == "win32" else "light_control.serial.Serial", return_value=active),
              patch.object(self.light.cancelled, "wait", return_value=False),
              patch.object(self.light, "_lines", return_value=iter(reply))):
            self.tray._switch_serial("COM10")
            self.light.worker.join(3)
            self.assertFalse(self.light.snapshot()["busy"])
            self.assertEqual(self.server.startup.preferences.snapshot()["target"], self.target)
            self.server.startup.step()
        self.assertTrue(self.server.ai.enabled)
        self.assertFalse(self.server.ai.applied)
        self.assertIsNone(self.server.ai.preview)
        active.write.assert_called_once_with(b"LIGHT STATUS\r\n")
        self.old.close.assert_called_once()
        return active

    def test_switch_with_ack_remembers_only_selected_port(self):
        active = self.switch(["OK LIGHT mode=MANUAL color=GREEN R=0 Y=0 G=1"])
        self.assertTrue(self.light.snapshot()["connected"])
        self.assertIs(self.light.link, active)
        self.assertEqual(self.light.snapshot()["endpoint"], "COM10 · 115200")
        self.assertEqual(self.server.startup.preferences.snapshot()["target"]["port"], "COM10")
        self.assertTrue(self.server.startup.preferences.snapshot()["roam_usb"])

    def test_bad_reply_does_not_overwrite_last_confirmed_port(self):
        active = self.switch(["unrelated device"])
        active.close.assert_called_once()
        self.assertFalse(self.light.snapshot()["connected"])
        self.assertEqual(self.server.startup.preferences.snapshot()["target"], self.target)
        self.assertTrue(self.server.startup.suspended)

    @unittest.skipUnless(sys.platform == "win32", "native Windows menu")
    def test_native_menu_dispatch_order_checked_state_and_refresh(self):
        import win32con as c
        import win32gui as g
        ports = [
            {"device": "COM10", "description": "Another adapter"},
            {"device": "COM3", "description": "USB & adapter (COM3)"},
            {"device": "DEMO", "description": "Ignored"},
        ]

        def choose(menu, *_):
            child = g.GetSubMenu(menu, 4)
            self.assertEqual(g.GetMenuItemCount(child), 2)
            self.assertEqual(g.GetMenuItemID(child, 0), FIRST_SERIAL_COMMAND)
            checked = g.GetMenuState(child, 0, c.MF_BYPOSITION)
            self.assertTrue(checked & c.MF_CHECKED)
            self.assertFalse(checked & c.MF_GRAYED)
            return g.GetMenuItemID(child, 1)

        def no_ports(menu, *_):
            child = g.GetSubMenu(menu, 4)
            self.assertEqual(g.GetMenuItemCount(child), 1)
            self.assertTrue(g.GetMenuState(child, 0, c.MF_BYPOSITION) & c.MF_GRAYED)
            return 0

        with (patch.object(self.server.controller, "ports", side_effect=[ports, []]) as enumerate_ports,
              patch.object(g, "TrackPopupMenu", side_effect=[0, 0]) as track,
              patch.object(g, "GetCursorPos", return_value=(10, 10)),
              patch.object(g, "SetForegroundWindow"), patch.object(g, "PostMessage"),
              patch.object(self.tray, "_update"), patch.object(self.tray, "_switch_serial") as switch):
            track.side_effect = choose
            self.tray._menu()
            switch.assert_called_once_with("COM10")
            track.side_effect = no_ports
            self.tray._menu()
            switch.assert_called_once()
            self.assertEqual(enumerate_ports.call_count, 2)
