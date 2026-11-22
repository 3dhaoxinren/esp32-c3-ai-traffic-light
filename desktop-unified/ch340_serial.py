"""Windows CH340 recovery for SetCommState error 31 at a verified cached 115200/8N1."""
import ctypes
import os
import re

import serial

from core import DeviceError


def can_use_cached_state(config):
    return (
        (config.BaudRate, config.ByteSize, config.Parity, config.StopBits, config.fBinary)
        == (115200, 8, 0, 0, 1)
        and not any(getattr(config, flag) for flag in (
            "fParity", "fOutxCtsFlow", "fOutxDsrFlow", "fDsrSensitivity", "fOutX", "fInX",
            "fErrorChar", "fNull", "fAbortOnError",
        ))
        and config.fDtrControl in (0, 1) and config.fRtsControl in (0, 1)
    )


def _has_compatible_cache(handle):
    from serial import win32
    config = win32.DCB()
    config.DCBlength = ctypes.sizeof(config)
    return bool(win32.GetCommState(handle, ctypes.byref(config))) and can_use_cached_state(config)


def is_setcommstate_31(error):
    message = str(error)
    # pySerial 3.5 embeds the structured Windows error repr rather than keeping
    # a .winerror attribute. Check the specific stage and numerical Win32 code.
    return message.startswith("Cannot configure port,") and bool(re.search(r",\s*31\)\s*$", message))


class CachedCH340Serial(serial.Serial):
    reused_config = False

    def _reconfigure_port(self):
        try:
            super()._reconfigure_port()
        except serial.SerialException as exc:
            if os.name != "nt" or not is_setcommstate_31(exc):
                raise
            if ((self.baudrate, self.bytesize, self.parity, self.stopbits) != (115200, 8, "N", 1)
                    or self.rtscts or self.dsrdtr or self.xonxoff or self.rs485_mode is not None
                    or not _has_compatible_cache(self._port_handle)):
                raise DeviceError("CH340 驱动拒绝配置串口（Win32 错误 31），现有参数不符合 115200/8N1，无法兼容连接。") from exc
            # The normal path already set the IO timeouts. Only DCB programming
            # failed. Keep the open handle/cached settings and require LIGHT STATUS.
            self.reused_config = True
