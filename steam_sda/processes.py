"""Do not pass PyInstaller's DLL search directory to Steam or the .NET helper."""
from contextlib import contextmanager
import ctypes
import os
import subprocess
import sys
import threading

from .errors import SteamError

_launch_lock = threading.RLock()


@contextmanager
def system_dlls():
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        yield
        return
    with _launch_lock:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetDllDirectoryW.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p]
        kernel.GetDllDirectoryW.restype = ctypes.c_uint32
        kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
        kernel.SetDllDirectoryW.restype = ctypes.c_int
        size = kernel.GetDllDirectoryW(0, None)
        previous = ctypes.create_unicode_buffer(size + 1)
        kernel.GetDllDirectoryW(len(previous), previous)
        if not kernel.SetDllDirectoryW(None):
            raise SteamError('Не удалось подготовить запуск внешнего клиента.')
        try:
            yield
        finally:
            kernel.SetDllDirectoryW(previous.value or None)


def start_external(args, **kwargs):
    with system_dlls():
        return subprocess.Popen(args, **kwargs)


def run_external(args, input=None, text=True, encoding='utf-8', capture_output=True, timeout=150, creationflags=0):
    process = start_external(args, stdin=subprocess.PIPE if input is not None else None,
                             stdout=subprocess.PIPE if capture_output else None,
                             stderr=subprocess.PIPE if capture_output else None,
                             text=text, encoding=encoding, creationflags=creationflags)
    try:
        stdout, stderr = process.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
