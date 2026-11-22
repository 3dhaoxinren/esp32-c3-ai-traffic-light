import json
import os
from pathlib import Path
import tempfile
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from core import DeviceError
from startup import (CH340, AutoConnector, ConnectionPreferences, WindowsStartup, capture_target,
                     resolve_target, validate_target)


def usb(port="COM3", serial="", location="1-5", vid=0x1A86, pid=0x7523):
    return NS(device=port, vid=vid, pid=pid, serial_number=serial, location=location)


class StartupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "connection.json"
        self.prefs = ConnectionPreferences(self.path)
        self.light = dict(connected=False, busy=False, message="not connected")
        self.module = dict(connected=False, busy=False)
        self.server = NS(connection_lock=threading.RLock(),
                         controller=NS(demo=False, snapshot=lambda: dict(self.module), log=Mock()),
                         lights=NS(snapshot=lambda: dict(self.light), start=Mock(),
                                   _close=Mock(side_effect=lambda:self.light.update(connected=False)),
                                   serial_handle_alive=Mock(return_value=True)),
                         ai=NS(set_enabled=Mock(), reset_link_state=Mock()))
        self.registration = NS(snapshot=lambda: dict(supported=True, enabled=False, warning=""), set_enabled=Mock())
        self.now = 100
        self.ports = [usb()]
        self.scan = Mock(side_effect=lambda: self.ports)
        self.auto = AutoConnector(self.server, self.prefs, self.registration, self.scan, lambda: self.now)
        self.target = capture_target(dict(transport="serial", port="COM3"), self.ports)

    def enable(self, **overrides):
        self.prefs.save(target=self.target, auto_connect=True, **overrides)

    def event(self, identity=CH340, removed=False):
        self.auto.notify_serial_change(identity, removed)
        self.auto.step()
        self.now += .5
        self.auto.step()

    def test_defaults_are_opt_in(self):
        self.auto.step()
        self.server.lights.start.assert_not_called()
        self.assertFalse(self.auto.snapshot()["auto_connect"])

    def test_preferences_round_trip_and_invalid_write_preserves_bytes(self):
        self.enable()
        before = self.path.read_bytes()
        self.assertEqual(ConnectionPreferences(self.path).snapshot(), self.prefs.snapshot())
        for bad in [1, "true", None]:
            with self.assertRaises(DeviceError):
                self.prefs.save(auto_connect=bad)
            self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_profile_is_safely_disabled(self):
        self.path.write_text('{"auto_connect": true}', encoding="utf-8")
        bad = ConnectionPreferences(self.path)
        self.assertFalse(bad.value["auto_connect"])
        self.assertTrue(bad.warning)

    def test_invalid_target_types(self):
        for bad in [1, {}, dict(transport="serial", port=3), dict(transport="tcp", host="127.0.0.1", tcp_port=True)]:
            with self.assertRaises(DeviceError):
                validate_target(bad)

    def test_serial_port_number_change(self):
        self.assertEqual(resolve_target(self.target, [usb("COM13")]), dict(transport="serial", port="COM13"))

    def test_ambiguous_or_wrong_adapter_is_not_probed(self):
        for ports in [[], [usb(), usb("COM13")], [usb(vid=0x303A, pid=0x1001)]]:
            with self.assertRaises(DeviceError):
                resolve_target(self.target, ports)

    def test_unique_serial_number_selects_correct_board(self):
        target = capture_target(dict(transport="serial", port="COM3"), [usb(serial="unique")])
        actual = resolve_target(target, [usb("COM7", serial="other"), usb("COM8", serial="unique")])
        self.assertEqual(actual["port"], "COM8")

    def test_multiple_ch340_selects_remembered_usb_location_not_first_port(self):
        ports = [usb("COM17", location="1-7.2"), usb("COM3", location="1-5")]
        self.assertEqual(resolve_target(self.target, ports), dict(transport="serial", port="COM3"))

    def test_remembered_ch340_interface_zero_can_match_plain_location_on_same_com(self):
        target={**self.target,"location":"1-5:x.0"}
        ports=[usb("COM17",location="1-7.2"),usb("COM3",location="1-5")]
        self.assertEqual(resolve_target(target,ports),dict(transport="serial",port="COM3"))

    def test_plain_location_never_matches_a_different_com_other_device_or_interface(self):
        for target,port in (
            ({**self.target,"location":"1-5:x.0"},usb("COM17",location="1-5")),
            ({**self.target,"location":"1-5:x.0"},usb("COM3",location="1-6")),
            ({**self.target,"location":"1-5:x.1"},usb("COM3",location="1-5")),
            ({**self.target,"vid":0x303A,"pid":0x1001,"location":"1-5:x.0"},
             usb("COM3",location="1-5",vid=0x303A,pid=0x1001)),
        ):
            with self.assertRaises(DeviceError):resolve_target(target,[port])

    def test_multiple_ch340_port_renumber_keeps_physical_binding(self):
        ports = [usb("COM3", location="1-7.2"), usb("COM21", location="1-5")]
        self.assertEqual(resolve_target(self.target, ports)["port"], "COM21")

    def test_other_ch340_alone_is_not_a_replacement_for_absent_bound_board(self):
        with self.assertRaises(DeviceError):
            resolve_target(self.target, [usb("COM17", location="1-7.2")])

    def test_opt_in_roam_selects_only_one_serial_numberless_ch340(self):
        actual=usb("COM17",location="1-7.2")
        self.assertEqual(resolve_target(self.target,[actual],roam_usb=True)["port"],"COM17")
        for ports in ([actual,usb("COM18",location="1-8")],
                      [usb("COM17",serial="other",location="1-7.2")],
                      [usb("COM17",location="",)]):
            with self.assertRaises(DeviceError):resolve_target(self.target,ports,roam_usb=True)

    def test_roaming_target_saved_only_after_successful_lamp_handshake(self):
        self.enable(roam_usb=True)
        desired=usb("COM27",location="1-2.3")
        self.ports=[desired]
        self.auto.step()
        self.server.lights.start.assert_called_once_with("light_connect",dict(transport="serial",port="COM27"))
        self.assertEqual(self.prefs.snapshot()["target"],self.target)
        self.light["connected"]=True;self.auto.step()
        self.assertEqual(self.prefs.snapshot()["target"]["port"],"COM27")
        self.assertEqual(self.prefs.snapshot()["target"]["location"],"1-2.3")

    def test_idle_usb_disconnect_detected_with_ai_follow_off_then_roams(self):
        self.enable(roam_usb=True,auto_follow=False)
        self.light.update(connected=True,transport="serial",endpoint="COM3 · 115200")
        self.ports=[usb("COM27",location="1-2")]
        for i in range(60):
            self.now += 1
            self.auto.step()
        self.scan.assert_not_called()
        self.server.lights._close.assert_not_called()
        self.auto.notify_serial_change(CH340, True)
        self.event()  # Arrival and removal are coalesced into one enumeration.
        self.scan.assert_called_once()
        self.server.lights._close.assert_called_once()
        self.server.lights.start.assert_called_once_with("light_connect",dict(transport="serial",port="COM27"))
        self.server.ai.set_enabled.assert_not_called()

    def test_removal_of_other_adapter_does_not_close_active_port(self):
        self.enable(roam_usb=True)
        self.light.update(connected=True,transport="serial",endpoint="COM3 · 115200")
        self.auto.step()
        self.event((0x303A, 0x1001), removed=True)
        self.scan.assert_not_called()
        self.event(removed=True)  # An identical CH340 was removed but ours is still healthy.
        self.scan.assert_called_once()
        self.server.lights._close.assert_not_called()
        self.server.lights.start.assert_not_called()

    def test_same_com_fast_replug_with_invalid_handle_reopens(self):
        self.enable()
        self.light.update(connected=True,transport="serial",endpoint="COM3 · 115200")
        self.server.lights.serial_handle_alive.return_value = False
        self.auto.step()
        self.event(removed=True)
        self.server.lights._close.assert_called_once()
        self.server.lights.start.assert_called_once_with("light_connect",dict(transport="serial",port="COM3"))

    def test_unplug_during_audio_waits_then_checks_once(self):
        self.enable(roam_usb=True)
        self.light.update(connected=True,transport="serial",endpoint="COM3 · 115200")
        self.server.audio = NS(snapshot=Mock(return_value={"busy": True}))
        self.auto.step()
        self.ports = []
        self.event(removed=True)
        self.scan.assert_not_called()
        self.server.audio.snapshot.return_value = {"busy": False}
        self.auto.step()
        self.scan.assert_called_once()
        self.server.lights._close.assert_called_once()
        self.now += 60
        self.auto.step()
        self.scan.assert_called_once()

    def test_lost_link_without_hotplug_triggers_one_check(self):
        self.enable()
        self.light.update(connected=True,transport="serial",endpoint="COM3 · 115200")
        self.auto.step()
        self.scan.assert_not_called()
        self.light["connected"] = False
        self.auto.step()
        self.scan.assert_called_once()
        self.now += 60
        self.auto.step()
        self.scan.assert_called_once()

    def test_old_ui_settings_call_preserves_opt_in(self):
        self.enable(roam_usb=True)
        self.auto.configure(dict(autostart=True,auto_connect=True,auto_follow=True))
        self.assertTrue(self.prefs.snapshot()["roam_usb"])

    def test_reused_com_name_at_wrong_usb_location_is_not_trusted(self):
        with self.assertRaises(DeviceError):
            resolve_target(self.target, [usb("COM3", location="1-7.2"), usb("COM21", location="1-7.3")])

    def test_unique_serial_has_priority_over_old_physical_location(self):
        target = capture_target(dict(transport="serial", port="COM3"), [usb(serial="unique")])
        ports = [usb("COM3", serial="different", location="1-5"), usb("COM21", serial="unique", location="1-7.2")]
        self.assertEqual(resolve_target(target, ports)["port"], "COM21")

    def test_duplicate_serial_is_disambiguated_by_saved_location(self):
        target = capture_target(dict(transport="serial", port="COM3"), [usb(serial="0001")])
        ports = [usb("COM17", serial="0001", location="1-7.2"), usb("COM3", serial="0001", location="1-5")]
        self.assertEqual(resolve_target(target, ports)["port"], "COM3")

    def test_legacy_target_without_usb_location_uses_saved_port_only(self):
        target = {**self.target, "location": ""}
        ports = [usb("COM17", location="1-7.2"), usb("COM3", location="1-5")]
        self.assertEqual(resolve_target(target, ports)["port"], "COM3")
        with self.assertRaises(DeviceError):
            resolve_target(target, [usb("COM17", location="1-7.2")])

    def test_multiple_ch340_auto_start_opens_only_bound_port(self):
        self.enable()
        self.ports = [usb("COM17", location="1-7.2"), usb("COM3", location="1-5")]
        self.auto.step()
        self.server.lights.start.assert_called_once_with("light_connect", dict(transport="serial", port="COM3"))

    def test_absent_bound_board_does_not_open_another_port_during_retry(self):
        self.enable()
        self.ports = [usb("COM17", location="1-7.2")]
        self.auto.step()
        self.now += 31
        self.auto.step()
        self.server.lights.start.assert_not_called()

    def test_unrelated_serial_adapters_do_not_interfere(self):
        ports = [usb("COM12", vid=0x303A, pid=0x1001, location="1-3"), usb()]
        self.assertEqual(resolve_target(self.target, ports)["port"], "COM3")

    def test_manual_target_only_remembered_after_handshake(self):
        self.auto.manual_started("light_connect", dict(transport="serial", port="COM3"))
        self.assertIsNone(self.prefs.value["target"])
        self.light.update(busy=True, connected=True)
        self.auto.step()
        self.assertIsNone(self.prefs.value["target"])
        self.light["busy"] = False
        self.auto.step()
        self.assertEqual(self.prefs.value["target"], self.target)
        self.server.ai.set_enabled.assert_not_called()

    def test_failed_manual_switch_does_not_reconnect_old_target(self):
        self.enable()
        self.auto.manual_started("light_connect", dict(transport="tcp", host="127.0.0.1", tcp_port=8080))
        self.auto.step()
        self.assertEqual(self.prefs.value["target"], self.target)
        self.assertTrue(self.auto.suspended)
        self.server.lights.start.assert_not_called()

    def test_no_ai_follow_until_successful_auto_handshake(self):
        self.enable()
        self.auto.step()
        self.server.ai.set_enabled.assert_not_called()
        self.light.update(connected=True, busy=True)
        self.auto.step()
        self.server.ai.set_enabled.assert_not_called()
        # Missing optional OLED is not a failed lamp handshake.
        self.light.update(busy=False, result="error", screen_error="OLED not attached")
        self.auto.step()
        self.server.ai.set_enabled.assert_called_once_with(True)

    def test_follow_can_be_disabled_independently(self):
        self.enable(auto_follow=False)
        self.auto.step()
        self.light["connected"] = True
        self.auto.step()
        self.server.ai.set_enabled.assert_not_called()

    def test_waits_for_usb_arrival_without_polling_then_recovers(self):
        self.enable()
        self.ports = []
        self.auto.step()
        self.assertEqual(self.auto.attempts, 1)
        self.ports = [usb("COM17")]
        for _ in range(40):
            self.now += 2
            self.auto.step()
        self.scan.assert_called_once()
        self.server.lights.start.assert_not_called()
        self.event()
        self.assertEqual(self.scan.call_count, 2)
        self.server.lights.start.assert_called_once_with("light_connect", dict(transport="serial", port="COM17"))

    def test_manual_disconnect_and_at_do_not_fight_reconnect(self):
        self.enable()
        for action in ["light_disconnect", "connect"]:
            self.auto.manual_started(action, {})
            self.auto.step()
            self.server.lights.start.assert_not_called()
        self.auto.resume()
        self.auto.step()
        self.server.lights.start.assert_called_once()

    def test_module_connection_has_priority(self):
        self.enable()
        self.module["connected"] = True
        self.auto.step()
        self.server.lights.start.assert_not_called()

    def test_failed_serial_auto_connection_waits_for_explicit_resume(self):
        self.enable()
        self.auto.step()
        self.auto.step()
        self.assertEqual(self.server.lights.start.call_count, 1)
        self.now += 2
        self.auto.step()
        self.server.lights.start.assert_called_once()
        self.auto.resume()
        self.auto.step()
        self.assertEqual(self.server.lights.start.call_count, 2)
        self.server.ai.set_enabled.assert_not_called()

    def test_tcp_endpoint_retry_uses_backoff_and_never_enumerates_serial(self):
        target = dict(transport="tcp", host="192.168.1.100", tcp_port=8080)
        self.prefs.save(auto_connect=True, target=target)
        self.auto.step()
        self.server.lights.start.assert_called_once_with("light_connect", target)
        self.auto.step()
        self.server.lights.start.assert_called_once()
        self.now += 2
        self.auto.step()
        self.assertEqual(self.server.lights.start.call_count, 2)
        self.scan.assert_not_called()

    def test_first_follow_can_stay_paused_without_changing_preferences(self):
        self.enable()
        self.auto.skip_first_auto_follow = True
        self.auto.step()
        self.light.update(connected=True,transport="serial",endpoint="COM3 · 115200")
        self.auto.step()
        self.server.ai.set_enabled.assert_not_called()
        self.assertTrue(self.prefs.value["auto_follow"])
        self.light["connected"] = False
        self.auto.step()
        self.light["connected"] = True
        self.auto.step()
        self.server.ai.set_enabled.assert_called_once_with(True)

    def test_demo_and_shutdown_never_connect(self):
        self.enable()
        self.server.controller.demo = True
        self.auto.step()
        self.server.lights.start.assert_not_called()
        with self.assertRaises(DeviceError):
            self.auto.configure(dict(autostart=True, auto_connect=True, auto_follow=True))
        self.server.controller.demo = False
        self.auto.shutdown()
        self.auto.step()
        self.server.lights.start.assert_not_called()

    def test_registry_failure_restores_previous_preferences(self):
        before = self.prefs.snapshot()
        self.registration.set_enabled.side_effect = DeviceError("registry denied")
        with self.assertRaises(DeviceError):
            self.auto.configure(dict(autostart=True, auto_connect=True, auto_follow=True))
        self.assertEqual(self.prefs.snapshot(), before)

    def test_settings_validation(self):
        for bad in [{}, dict(autostart="yes", auto_connect=True, auto_follow=True)]:
            with self.assertRaises(DeviceError):
                self.auto.configure(bad)
        self.registration.set_enabled.assert_not_called()

    def test_startup_command_quotes_path_and_quiet_flags(self):
        with patch("startup.sys.executable", "C:/Folder With Spaces/AI.exe"), patch("startup.sys.frozen", True, create=True):
            reg = WindowsStartup()
        self.assertTrue(reg.command.startswith('"'))
        self.assertTrue(reg.command.endswith("--autostart --no-browser"))

    def test_unsupported_startup_cannot_modify_registry(self):
        reg = WindowsStartup(allowed=False)
        self.assertFalse(reg.snapshot()["enabled"])
        with self.assertRaises(DeviceError):
            reg.set_enabled(True)


class StartupApiTests(unittest.TestCase):
    def setUp(self):
        from server import AppServer
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.dict(os.environ, LOCALAPPDATA=temp.name):
            self.server = AppServer()
        self.server.startup.registration = NS(snapshot=lambda: dict(supported=True, enabled=False, warning=""), set_enabled=Mock())
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, action, data, token=None, origin=None):
        headers = {"Content-Type": "application/json", "X-Session-Token": token or self.server.token}
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(self.server.origin + "/api/" + action,
                                         data=json.dumps(data).encode(), headers=headers)
        with self.opener.open(request, timeout=3) as response:
            return response.status

    def test_only_authenticated_local_session_changes_settings(self):
        data = dict(autostart=True, auto_connect=True, auto_follow=True)
        for kwargs in [dict(token="wrong"), dict(origin="https://example.com")]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.call("startup_settings", data, **kwargs)
            self.assertEqual(error.exception.code, 403)
        self.server.startup.registration.set_enabled.assert_not_called()
        self.assertEqual(self.call("startup_settings", data), 202)
        self.assertTrue(self.server.startup.preferences.value["auto_connect"])
        self.server.startup.registration.set_enabled.assert_called_once_with(True)

    def test_demo_and_resume_without_saved_device_are_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("startup_resume", {})
        self.assertEqual(error.exception.code, 409)
        self.server.controller.demo = True
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("startup_settings", dict(autostart=True, auto_connect=True, auto_follow=True))
        self.assertEqual(error.exception.code, 409)
        self.server.startup.registration.set_enabled.assert_not_called()


if __name__ == "__main__":
    unittest.main()
