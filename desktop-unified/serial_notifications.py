"""Windows COM-interface arrival/removal notifications; no device enumeration."""
import ctypes as c
from ctypes import wintypes as w
import re
from uuid import UUID


COM_INTERFACE = UUID("86e0d1e0-8089-11d0-9ce4-08003e301f73")
USB_ID = re.compile(r"VID_([0-9a-f]{4})[&+]PID_([0-9a-f]{4})(?![0-9a-f])", re.I)
ARRIVAL, REMOVAL = 0, 1


class _FilterData(c.Union):
    _fields_ = [("guid", c.c_ubyte * 16), ("handle", w.HANDLE),
                ("instance_id", w.WCHAR * 200)]


class _Filter(c.Structure):
    _fields_ = [("size", w.DWORD), ("flags", w.DWORD), ("type", w.DWORD),
                ("reserved", w.DWORD), ("data", _FilterData)]


class _InterfaceEvent(c.Structure):
    _fields_ = [("type", w.DWORD), ("reserved", w.DWORD), ("guid", c.c_ubyte * 16),
                ("link", w.WCHAR * 1)]


def event_identity(link):
    match = USB_ID.search(link)
    return tuple(int(part, 16) for part in match.groups()) if match else (None, None)


class WindowsSerialNotifications:
    def __init__(self, notify):
        self.notify = notify
        api = c.WinDLL("cfgmgr32")
        signature = c.WINFUNCTYPE(w.DWORD, w.HANDLE, c.c_void_p, w.DWORD, c.c_void_p, w.DWORD)
        self.callback = signature(self._callback)
        api.CM_Register_Notification.argtypes = (c.POINTER(_Filter), c.c_void_p, signature, c.POINTER(w.HANDLE))
        api.CM_Register_Notification.restype = w.DWORD
        self.unregister = api.CM_Unregister_Notification
        self.unregister.argtypes = (w.HANDLE,)
        self.unregister.restype = w.DWORD
        self.handle = w.HANDLE()
        request = _Filter()
        request.size = c.sizeof(request)
        request.data.guid[:] = COM_INTERFACE.bytes_le
        result = api.CM_Register_Notification(c.byref(request), None, self.callback, c.byref(self.handle))
        if result:
            raise OSError(f"CM_Register_Notification: CONFIGRET 0x{result:X}")

    def _callback(self, _handle, _context, action, data, size):
        # Called by PnP on a foreign thread. Only queue metadata; no I/O, no
        # connection_lock, and never unregister or open a port in this callback.
        offset = _InterfaceEvent.link.offset
        length = (size - offset) // c.sizeof(w.WCHAR)
        if action in (ARRIVAL, REMOVAL) and data and 0 < length <= 8192:
            link = c.wstring_at(data + offset, length).partition("\0")[0]
            self.notify(event_identity(link), action == REMOVAL)
        return 0

    def close(self):
        if self.handle:
            result = self.unregister(self.handle)
            if result:
                raise OSError(f"CM_Unregister_Notification: CONFIGRET 0x{result:X}")
            self.handle = w.HANDLE()
