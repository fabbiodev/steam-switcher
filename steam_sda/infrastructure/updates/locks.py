"""Non-destructive Windows write probes for modules retained by Steam."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path

import psutil

from steam_sda.domain.errors import SteamError


def write_error(path):
    """Request write access to an existing file, without writing or truncating it."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x40000000, 7, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        return ctypes.get_last_error()
    kernel.CloseHandle(handle)
    return 0


def module_users(path):
    """Best-effort names only; inaccessible processes must not hide a lock."""
    target = os.path.normcase(os.path.abspath(path))
    names = set()
    for process in psutil.process_iter(['name']):
        try:
            if any(os.path.normcase(os.path.abspath(mapping.path)) == target
                   for mapping in process.memory_maps()):
                names.add(process.info['name'] or f'PID {process.pid}')
        except (psutil.Error, OSError):
            continue
    return ', '.join(sorted(names))


def ensure_device_available(directory):
    # The running Qt/Python files are expected to be locked until our process exits.
    # Device DLLs can remain loaded in another process after Steam SDA has exited.
    device = Path(directory) / 'integrations/device'
    for path in sorted(device.glob('*.dll')):
        error = write_error(path)
        if error in (0, 2, 3):
            continue
        users = module_users(path) if error in (5, 32, 33) else ''
        if users or error in (32, 33):
            users = users or 'Steam или другую использующую файл программу'
            raise SteamError(f'Обновление остановлено: файл {path.name} занят. '
                             f'Закройте {users} и повторите установку. Steam SDA остаётся открытой.')
        raise SteamError(f'Обновление остановлено: нет доступа для замены {path.name} '
                         f'(ошибка Windows {error}). Закройте Steam и проверьте права на папку установки.')
