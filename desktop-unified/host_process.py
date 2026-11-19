"""Launch only the existing installed 2.7.0 on demand; preserve its files and settings."""
import ctypes as ct
from ctypes import wintypes as wt
import os
from pathlib import Path

def default_path():
    return Path.home()/'Desktop'/'HLK-M30工具'/'AI指示灯.exe'

def running(exe):
    if os.name!='nt':return False
    class Entry(ct.Structure):
        _fields_=[('size',wt.DWORD),('usage',wt.DWORD),('pid',wt.DWORD),('heap',ct.c_size_t),
            ('module',wt.DWORD),('threads',wt.DWORD),('parent',wt.DWORD),('priority',wt.LONG),('flags',wt.DWORD),('exe',wt.WCHAR*260)]
    k=ct.WinDLL('kernel32',use_last_error=True)
    def bind(n,r,t):
        f=getattr(k,n);f.restype=r;f.argtypes=t;return f
    snap=bind('CreateToolhelp32Snapshot',wt.HANDLE,[wt.DWORD,wt.DWORD])
    first=bind('Process32FirstW',wt.BOOL,[wt.HANDLE,ct.POINTER(Entry)])
    next_=bind('Process32NextW',wt.BOOL,[wt.HANDLE,ct.POINTER(Entry)])
    close=bind('CloseHandle',wt.BOOL,[wt.HANDLE])
    h=snap(2,0)
    if h==ct.c_void_p(-1).value or h is None:raise OSError('无法核对旧版是否仍在后台。')
    try:
        e=Entry();e.size=ct.sizeof(e)
        if not first(h,ct.byref(e)):
            raise OSError('无法核对旧版是否仍在后台。')
        while True:
            if e.exe.casefold()==Path(exe).name.casefold():return True
            e.size=ct.sizeof(e)
            if not next_(h,ct.byref(e)):return False
    finally:close(h)
