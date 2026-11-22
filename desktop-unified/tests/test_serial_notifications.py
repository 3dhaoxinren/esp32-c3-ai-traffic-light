import ctypes as c
import sys
import unittest
from unittest.mock import Mock

from serial_notifications import (ARRIVAL, REMOVAL, WindowsSerialNotifications,
                                  _InterfaceEvent, event_identity)


class SerialNotificationTests(unittest.TestCase):
    def test_extracts_usb_identity_for_ch340_cdc_and_ftdi(self):
        for link, pair in [
            (r"\\?\USB#VID_1A86&PID_7523#5&1234ABCD&0&5#{guid}", (0x1A86, 0x7523)),
            (r"\\?\USB#vid_303a&pid_1001&mi_00#serial#{guid}", (0x303A, 0x1001)),
            (r"\\?\FTDIBUS#VID_0403+PID_6001+serialA#{guid}", (0x0403, 0x6001)),
            (r"\\?\ACPI#PNP0501#1#{guid}", (None, None)),
        ]:
            with self.subTest(link=link):
                self.assertEqual(event_identity(link), pair)

    @unittest.skipUnless(sys.platform == "win32", "Windows Configuration Manager")
    def test_native_subscription_and_callback_buffer(self):
        deliver = Mock()
        subscription = WindowsSerialNotifications(deliver)
        self.assertTrue(subscription.handle)
        name = c.create_unicode_buffer(r"\\?\USB#VID_1A86&PID_7523#test")
        offset = _InterfaceEvent.link.offset
        raw = c.create_string_buffer(offset + c.sizeof(name))
        c.memmove(c.addressof(raw) + offset, name, c.sizeof(name))
        try:
            for action in (ARRIVAL, REMOVAL):
                self.assertEqual(subscription.callback(None, None, action, c.addressof(raw), len(raw)), 0)
            self.assertEqual(subscription.callback(None, None, REMOVAL, c.addressof(raw), offset), 0)
            self.assertEqual(subscription.callback(None, None, 2, c.addressof(raw), len(raw)), 0)
            self.assertEqual([x.args for x in deliver.call_args_list],
                             [((0x1A86, 0x7523), False), ((0x1A86, 0x7523), True)])
        finally:
            subscription.close()
        self.assertFalse(subscription.handle)
