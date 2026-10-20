"""App-owned cache and Windows-user-bound recovery snapshots."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile

from .errors import SteamError


def data_dir():
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "SteamSDA"


def atomic_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="steam-sda-", delete=False) as stream:
            name = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and Path(name).exists():
            Path(name).unlink()


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(payload, decrypt=False, entropy=None, description="Steam SDA"):
    if os.name != "nt":
        raise SteamError("Защищённое хранение доступно в Windows.")
    buffer = ctypes.create_string_buffer(payload)
    source = _Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy_buffer = ctypes.create_string_buffer(entropy) if entropy is not None else None
    entropy_blob = _Blob(len(entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte))) if entropy is not None else None
    entropy_pointer = ctypes.byref(entropy_blob) if entropy_blob is not None else None
    target = _Blob()
    crypt, kernel = ctypes.WinDLL("crypt32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        ok = fn(ctypes.byref(source), None, entropy_pointer, None, None, 1, ctypes.byref(target))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        ok = fn(ctypes.byref(source), description, entropy_pointer, None, None, 1, ctypes.byref(target))
    if not ok:
        raise SteamError("Windows не смог прочитать или сохранить защищённые данные Steam SDA.")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


class Store:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else data_dir()

    def read(self, name, encrypted=False):
        path = self.directory / name
        if not path.exists():
            return {}
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError()
            raw = path.read_bytes()
            data = json.loads(protect(raw, True) if encrypted else raw)
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (OSError, ValueError):
            raise SteamError("Не удалось прочитать данные Steam SDA. Исходные файлы сохранены.") from None

    def write(self, name, value, encrypted=False):
        try:
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            atomic_write(self.directory / name, protect(raw) if encrypted else raw)
        except (OSError, ValueError):
            raise SteamError("Не удалось сохранить данные Steam SDA.") from None

    def remove_journal(self):
        try:
            (self.directory / "switch-recovery.dpapi").unlink(missing_ok=True)
        except OSError:
            raise SteamError("Не удалось завершить запись переключения Steam.") from None
