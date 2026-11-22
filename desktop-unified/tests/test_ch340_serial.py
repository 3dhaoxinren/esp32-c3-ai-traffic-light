import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import serial

from ch340_serial import CachedCH340Serial, can_use_cached_state, is_setcommstate_31
from core import DeviceError


def profile(**kwargs):
    d = dict(BaudRate=115200, ByteSize=8, Parity=0, StopBits=0, fBinary=1,
             fParity=0, fOutxCtsFlow=0, fOutxDsrFlow=0, fDsrSensitivity=0, fOutX=0, fInX=0,
             fErrorChar=0, fNull=0, fAbortOnError=0, fDtrControl=1, fRtsControl=1)
    return SimpleNamespace(**(d | kwargs))


class CachedPortTests(unittest.TestCase):
    def pattern(self, code):
        return serial.SerialException(f"Cannot configure port, something went wrong. Original message: PermissionError(13, 'device', None, {code})")

    def test_requires_matching_cached_config(self):
        self.assertTrue(can_use_cached_state(profile()))
        for modifier in (dict(BaudRate=9600),dict(ByteSize=7),dict(Parity=1),dict(StopBits=2),
                         dict(fOutxCtsFlow=1),dict(fInX=1),dict(fDtrControl=2),dict(fRtsControl=3)):
            self.assertFalse(can_use_cached_state(profile(**modifier)))

    def test_only_configuration_stage_win32_31(self):
        self.assertTrue(is_setcommstate_31(self.pattern(31)))
        for error in (self.pattern(87),self.pattern(5),serial.SerialException("could not open port 'COM3': PermissionError(13, 'occupied', None, 31)")):
            self.assertFalse(is_setcommstate_31(error))

    @unittest.skipUnless(os.name == "nt","Windows implementation")
    def test_initial_configuration_failure_can_use_verified_cache(self):
        link=CachedCH340Serial(port=None,baudrate=115200)
        link._port_handle=9
        with (patch.object(serial.Serial,"_reconfigure_port",side_effect=self.pattern(31)),
              patch("ch340_serial._has_compatible_cache",return_value=True) as cache):
            link._reconfigure_port()
            cache.assert_called_once_with(9)
            self.assertTrue(link.reused_config)

    @unittest.skipUnless(os.name == "nt","Windows implementation")
    def test_incompatible_cache_does_not_mark_success(self):
        link=CachedCH340Serial(port=None,baudrate=115200)
        link._port_handle=9
        with (patch.object(serial.Serial,"_reconfigure_port",side_effect=self.pattern(31)),
              patch("ch340_serial._has_compatible_cache",return_value=False)):
            with self.assertRaises(DeviceError):link._reconfigure_port()
            self.assertFalse(link.reused_config)
