"""Windows Steam adapter: processes, registry and native configuration files."""

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import subprocess
import time

import psutil

from steam_sda.domain.errors import SteamError
from steam_sda.domain.identity import identity_from_log
from steam_sda.infrastructure.persistence.storage import atomic_write
from steam_sda.infrastructure.ipc.processes import start_external
from steam_sda.infrastructure.steam.vdf import accounts, chooser_preference
from steam_sda.infrastructure.steam.vdf import read_value, write_value, parse
from steam_sda.domain.accounts import metadata
from steam_sda.domain.identity import valid_id
from steam_sda.domain.identity import STEAM_ID_BASE

if os.name == "nt":
    import winreg

REGISTRY = r"Software\Valve\Steam"




def discover_steam():
    if os.name != "nt":
        raise SteamError("Переключение Steam поддерживается в Windows.")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY) as key:
            path = winreg.QueryValueEx(key, "SteamPath")[0]
        if isinstance(path, str) and (Path(path) / "steam.exe").is_file():
            return str(Path(path).resolve())
    except OSError:
        pass
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base and (Path(base) / "Steam" / "steam.exe").is_file():
            return str(Path(base) / "Steam")
    raise SteamError("Steam не найден. Укажите его папку в настройках.")


class NativeSteam:
    from . import vdf as codec

    def login_revision(self):
        return self.login_file.stat().st_mtime_ns if self.login_file.exists() else None

    def write_logins(self, raw):
        atomic_write(self.login_file, raw)

    def __init__(self, path=None):
        self.root = Path(path or discover_steam()).resolve()
        self.exe = self.root / "steam.exe"
        self.login_file = self.root / "config" / "loginusers.vdf"
        if not self.exe.is_file() or self.exe.is_symlink():
            raise SteamError("В выбранной папке не найден steam.exe.")

    def read_logins(self):
        try:
            if not self.login_file.exists():
                return b'"users"\n{\n}\n'
            for path in (self.root / "config", self.login_file):
                if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                    raise SteamError("Файл аккаунтов Steam перенаправлен. Выберите обычную папку Steam.")
            if self.login_file.stat().st_size > 4 * 1024 * 1024:
                raise SteamError("Файл аккаунтов Steam слишком велик.")
            return self.login_file.read_bytes()
        except OSError:
            raise SteamError("Не удалось прочитать аккаунты Steam. Проверьте доступ к его папке.") from None

    def account_list(self):
        rows = accounts(self.read_logins())
        active = self.active_id()
        if not active or any(row['steam_id'] == active for row in rows):
            return rows
        raw = self._read_config()
        if raw is None:
            return rows
        _, node = parse(raw, 'InstallConfigStore')
        for key in ('Software', 'Valve', 'Steam', 'Accounts'):
            node = node.get(key)
            if node is None:
                return rows
        candidates = [entry for entry in node.children if entry.value is None and entry.text('SteamID') == active]
        if len(candidates) == 1:
            login = candidates[0].key.value
            row = metadata({'steam_id': active, 'login': login, 'name': login})
            rows.append({**row, 'remembered': False, 'recent': True, 'last_login': 0, 'temporary_session': True})
        return rows

    def processes(self):
        found = []
        for process in psutil.process_iter(["pid", "name", "exe"]):
            try:
                if (process.info["name"] or "").casefold() == "steam.exe" and process.info["exe"] and Path(process.info["exe"]).resolve() == self.exe.resolve():
                    found.append(process)
            except (psutil.Error, OSError):
                continue
        return found

    def active_id(self):
        processes = self.processes()
        if not processes:
            return None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY + r"\ActiveProcess") as key:
                pid = winreg.QueryValueEx(key, "pid")[0]
                user = winreg.QueryValueEx(key, "ActiveUser")[0]
                universe = winreg.QueryValueEx(key, "Universe")[0]
            if (type(user) is int and 0 < user <= 0xFFFFFFFF and universe in (1, "Public", "public")
                    and any(p.pid == pid for p in processes)):
                return str(STEAM_ID_BASE + user)
        except (OSError, ValueError, NameError):
            pass
        # Other Steam installations can overwrite HKCU\ActiveProcess. The
        # installed client's own authenticated connection log remains scoped
        # to this install and is accepted only after its current process began.
        log = self.root / "logs" / "connection_log.txt"
        try:
            if log.is_symlink() or log.stat().st_size > 100 * 1024 * 1024:
                return None
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 256 * 1024))
                text = stream.read(256 * 1024).decode("utf-8", errors="replace")
            return identity_from_log(text, min(p.create_time() for p in processes))
        except (OSError, psutil.Error):
            return None

    def registry_value(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY) as key:
                value, kind = winreg.QueryValueEx(key, "AutoLoginUser")
            if kind != winreg.REG_SZ or not isinstance(value, str):
                raise SteamError("Steam сохранил неизвестный формат имени входа.")
            return {"exists": True, "value": value, "kind": kind}
        except FileNotFoundError:
            return {"exists": False}
        except OSError:
            raise SteamError("Не удалось прочитать настройки входа Steam.") from None

    def set_registry(self, value):
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY, 0, winreg.KEY_SET_VALUE) as key:
                if value.get("exists"):
                    if value.get("kind") != winreg.REG_SZ or not isinstance(value.get("value"), str):
                        raise SteamError("Некорректный формат сохранённых настроек Steam.")
                    winreg.SetValueEx(key, "AutoLoginUser", 0, winreg.REG_SZ, value["value"])
                else:
                    try:
                        winreg.DeleteValue(key, "AutoLoginUser")
                    except FileNotFoundError:
                        pass
        except OSError:
            raise SteamError("Не удалось сохранить выбор аккаунта Steam.") from None

    def remember_value(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY) as key:
                value, kind = winreg.QueryValueEx(key, "RememberPassword")
            if kind != winreg.REG_DWORD or type(value) is not int or value not in (0, 1):
                raise SteamError("Steam сохранил неизвестный формат настройки запоминания входа.")
            return {"exists": True, "value": value, "kind": kind}
        except FileNotFoundError:
            return {"exists": False}
        except OSError:
            raise SteamError("Не удалось прочитать настройку запоминания входа Steam.") from None

    def set_remember(self, value):
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY, 0, winreg.KEY_SET_VALUE) as key:
                if value.get("exists"):
                    if value.get("kind") != winreg.REG_DWORD or value.get("value") not in (0, 1):
                        raise SteamError("Некорректный формат настройки запоминания Steam.")
                    winreg.SetValueEx(key, "RememberPassword", 0, winreg.REG_DWORD, value["value"])
                else:
                    try:
                        winreg.DeleteValue(key, "RememberPassword")
                    except FileNotFoundError:
                        pass
        except OSError:
            raise SteamError("Не удалось сохранить настройку запоминания входа Steam.") from None

    def games_running(self):
        # Steam's registry Running flags can survive a crash for months. Check
        # live processes; never terminate a game on the basis of stale flags.
        for process in psutil.process_iter(["name", "exe"]):
            name, exe = process.info["name"] or "", process.info["exe"] or ""
            if name.casefold() == "cs2.exe" or "\\steamapps\\common\\" in exe.casefold():
                return True
        helpers = {"steamwebhelper.exe", "gameoverlayui.exe", "gameoverlayui64.exe", "steamerrorreporter.exe",
                   "steamerrorreporter64.exe", "crashhandler.exe", "steamservice.exe", "steam_monitor.exe"}
        for steam in self.processes():
            try:
                for child in steam.children(recursive=True):
                    if child.name().casefold() not in helpers:
                        return True
            except psutil.Error:
                raise SteamError("Не удалось проверить запущенные игры. Закройте их и повторите.") from None
        return False

    def _read_config(self):
        path = self.root / "config" / "config.vdf"
        if not path.exists():
            return None
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400 or path.stat().st_size > 4 * 1024 * 1024:
            raise SteamError("Не удалось безопасно прочитать настройки Steam.")
        return path.read_bytes()

    def chooser_value(self):
        raw = self._read_config()
        return chooser_preference(raw) if raw is not None else None

    def set_chooser(self, value):
        if value is None:
            return
        raw = self._read_config()
        if raw is None:
            raise SteamError("Файл настроек Steam исчез во время переключения.")
        patched = chooser_preference(raw, value)
        if patched != raw:
            atomic_write(self.root / "config" / "config.vdf", patched)

    def account_mapping(self, login):
        raw = self._read_config()
        if raw is None:
            return None
        return read_value(raw, 'InstallConfigStore',
                          ['Software', 'Valve', 'Steam', 'Accounts', login.casefold(), 'SteamID'])

    def set_account_mapping(self, login, sid):
        if not isinstance(login, str) or not login or (sid is not None and not valid_id(sid)):
            raise SteamError('Некорректная запись аккаунта в настройках Steam.')
        raw = self._read_config()
        if raw is None:
            if sid is None:
                return
            raw = b'"InstallConfigStore"\n{\n}\n'
        patched = write_value(raw, 'InstallConfigStore',
                              ['Software', 'Valve', 'Steam', 'Accounts', login.casefold(), 'SteamID'], sid)
        if patched != raw:
            atomic_write(self.root / 'config/config.vdf', patched)

    def stop(self):
        if self.games_running():
            raise SteamError("Сначала закройте запущенные игры Steam, затем повторите переключение.")
        if not self.processes():
            return
        try:
            start_external([str(self.exe), "-shutdown"], cwd=self.root, creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            raise SteamError("Не удалось закрыть Steam. Закройте его через меню и повторите.") from None
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if not self.processes():
                time.sleep(0.8)  # Let Steam finish writing its account metadata.
                return
            time.sleep(0.3)
        raise SteamError("Steam не завершил работу. Закройте игры и Steam через его меню, затем повторите.")

    def launch(self):
        try:
            start_external([str(self.exe)], cwd=self.root, creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            raise SteamError("Не удалось запустить Steam.") from None

    @contextmanager
    def exclusive(self):
        if os.name != "nt":
            raise SteamError("Переключение Steam поддерживается в Windows.")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        name = "Local\\SteamSDA-Switch-" + hashlib.sha256(str(self.root).casefold().encode()).hexdigest()[:24]
        handle = kernel.CreateMutexW(None, True, name)
        error = ctypes.get_last_error()
        if not handle:
            raise SteamError("Не удалось заблокировать переключение Steam.")
        if error == 183:
            kernel.CloseHandle(handle)
            raise SteamError("Steam SDA уже переключает аккаунт в другом окне.")
        try:
            yield
        finally:
            kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
