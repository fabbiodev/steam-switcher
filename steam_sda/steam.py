"""Select native remembered Steam logins, with an encrypted recovery journal."""

import base64
from contextlib import contextmanager
import ctypes
from datetime import datetime
from ctypes import wintypes
import hashlib
import os
import re
from pathlib import Path
import subprocess
import time

import psutil

from .errors import SteamError
from .storage import Store, atomic_write
from .processes import start_external
from .vdf import accounts, select_account, valid_id, chooser_preference, authenticated_account, STEAM_ID_BASE

if os.name == "nt":
    import winreg

REGISTRY = r"Software\Valve\Steam"
JOURNAL = "switch-recovery.dpapi"


def identity_from_log(text, process_started, now=None):
    """Only consider authenticated events belonging to the current Steam process."""
    current = None
    now = time.time() if now is None else now
    for line in text.splitlines():
        match = re.match(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] \[(Logged On|Logged Off|Logging On),", line)
        if not match:
            continue
        try:
            stamp = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if not process_started - 5 <= stamp <= now + 60:
            continue
        if match[2] in ("Logged Off", "Logging On"):
            current = None
        if "RecvMsgClientLogOnResponse()" in line and ("'OK'" in line or "processing complete" in line):
            ids = re.findall(r"\[U:1:(\d+)\]", line)
            if ids and 0 < int(ids[-1]) <= 0xFFFFFFFF:
                current = str(STEAM_ID_BASE + int(ids[-1]))
    return current


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
        return accounts(self.read_logins())

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


class Switcher:
    def __init__(self, native, store=None, timeout=60, auth=None, vault=None, token_bridge=None, devices=None):
        self.native = native
        self.store = store or Store()
        self.timeout = timeout
        self.auth, self.vault, self.token_bridge = auth, vault, token_bridge
        self.devices = devices

    def _launch(self, sid):
        if self.devices and sid:
            row = self.vault.get(sid)
            if not row:
                row = next((a for a in self.native.account_list() if a['steam_id'] == sid), None)
            if not row:
                raise SteamError('Не найден аккаунт для запуска с сохранённым HWID.')
            self.devices.launch(row)
        else:
            self.native.launch()

    def journal(self):
        data = self.store.read(JOURNAL, encrypted=True)
        if not data:
            return {}
        try:
            if data.get("version") != 1 or data.get("root") != str(self.native.root) or not valid_id(data.get("target")):
                raise ValueError()
            raw = base64.b64decode(data["before"], validate=True)
            rows = accounts(raw)
            row = next((a for a in rows if a["steam_id"] == data["target"]), None)
            if row is None:
                from .accounts import metadata
                row = metadata(data['verified_target'])
                if row['steam_id'] != data['target']:
                    raise ValueError()
            if row["login"] != data["target_login"] or not isinstance(data["registry"], dict):
                raise ValueError()
            for name, kind in (("registry", 1), ("remember", 4)):
                value = data.get(name, {"exists": False})
                if not isinstance(value, dict) or type(value.get("exists")) is not bool:
                    raise ValueError()
                if value["exists"] and (value.get("kind") != kind or
                        (name == "registry" and not isinstance(value.get("value"), str)) or
                        (name == "remember" and (type(value.get("value")) is not int or value["value"] not in (0, 1)))):
                    raise ValueError()
            if data.get("previous") is not None and not any(a["steam_id"] == data["previous"] for a in rows):
                raise ValueError()
            if data.get("chooser") not in (None, "0", "1"):
                raise ValueError()
            desktop = data.get('desktop')
            if desktop is not None:
                if not self.token_bridge or not isinstance(desktop, dict):
                    raise ValueError()
                before = desktop.get('before', {})
                if not isinstance(before, dict) or before.get('path') != str(self.token_bridge.path):
                    raise ValueError()
                for value in (before.get('value'), desktop.get('selected')):
                    if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[a-fA-F0-9]{0,65536}', value) or len(value) % 2):
                        raise ValueError()
                if not desktop.get('selected'):
                    raise ValueError()
            return data
        except (ValueError, KeyError, StopIteration, TypeError):
            raise SteamError("Не удалось прочитать резервную копию переключения. Исходная запись сохранена.") from None

    def has_recovery(self):
        return bool(self.store.read(JOURNAL, encrypted=True))

    def complete_if_active(self):
        data = self.journal()
        if (data and self.native.active_id() == data["target"]
                and (not self.devices or self.devices.is_applied(data['target']))):
            self.store.remove_journal()
            return True
        return False

    def _restore(self, data, use_previous_session=False):
        original = base64.b64decode(data["before"], validate=True)
        current = self.native.read_logins()
        old_rows, rows = accounts(original), accounts(current)
        current_reg = self.native.registry_value()
        if current_reg != data["registry"] and current_reg.get("value") != data["target_login"]:
            raise SteamError("Выбор аккаунта Steam изменён другой программой. Резервная копия сохранена.")
        if current == original or hashlib.sha256(current).hexdigest() == data["selected_hash"]:
            restored = original
        elif data.get("previous"):
            previous = next((a for a in old_rows if a["steam_id"] == data["previous"]), None)
            now = next((a for a in rows if a["steam_id"] == data["previous"]), None)
            if not previous or not now or previous["login"] != now["login"]:
                raise SteamError("Прежний аккаунт изменён или удалён из Steam. Резервная копия сохранена.")
            # Preserve refreshed credentials, new accounts and every unrelated field.
            restored = select_account(current, data["previous"])
        else:
            raise SteamError("Файл аккаунтов Steam изменился. Автоматическое восстановление остановлено.")
        registry = data["registry"]
        remember = data.get("remember", {"exists": False})
        if use_previous_session and data.get("previous"):
            previous = next((a for a in old_rows if a["steam_id"] == data["previous"]), None)
            now = next((a for a in rows if a["steam_id"] == data["previous"]), None)
            if not previous or not now or previous["login"] != now["login"]:
                raise SteamError("Прежний аккаунт изменился. Резервная копия сохранена.")
            restored = select_account(restored, data["previous"])
            registry = {"exists": True, "value": previous["login"], "kind": 1}
            if previous["remembered"]:
                remember = {"exists": True, "value": 1, "kind": 4}
        atomic_write(self.native.login_file, restored)
        self.native.set_registry(registry)
        self.native.set_remember(remember)
        self.native.set_chooser("0" if use_previous_session else data.get("chooser"))
        if data.get('desktop'):
            self.token_bridge.rollback(data['target_login'], data['desktop']['before'], data['desktop']['selected'])

    def switch(self, steam_id, progress=lambda text: None):
        with self.native.exclusive():
            self.complete_if_active()
            if self.journal():
                raise SteamError("Есть незавершённое переключение. Завершите вход в Steam или нажмите «Вернуть прежний вход».")
            before_rows = self.native.account_list()
            target = next((a for a in before_rows if a["steam_id"] == steam_id), None)
            managed = self.vault.get(steam_id) if self.vault else {}
            if not target and managed and self.auth:
                target = managed
            if not target:
                raise SteamError("Аккаунт исчез из списка Steam. Обновите список.")
            previous = self.native.active_id()
            if self.devices:
                progress('Проверяю сохранённый HWID аккаунта…')
                self.devices.preflight()
                self.devices.ensure(target)
                previous_row = next((a for a in before_rows if a['steam_id'] == previous), None)
                if previous_row:
                    self.devices.ensure(previous_row)
                managed = self.vault.get(steam_id)
            if previous == steam_id and (not self.devices or self.devices.is_applied(steam_id)):
                return {"state": "ready", "steam_id": steam_id}
            progress("Закрываю Steam…")
            self.native.stop()
            data = None
            try:
                authenticated = None
                if managed and self.auth and (managed.get('auto_restore') or not any(a['steam_id'] == steam_id for a in before_rows)):
                    progress("Восстанавливаю вход через SteamKit…")
                    authenticated = self.auth.authenticate(steam_id)
                raw = self.native.read_logins()
                rows = accounts(raw)
                selected = next((a for a in rows if a["steam_id"] == steam_id), None)
                if selected is None and authenticated:
                    selected = {**authenticated, 'remembered': True}
                if not selected or selected["login"].casefold() != target["login"].casefold():
                    raise SteamError("Аккаунт изменился во время завершения Steam.")
                old_registry = self.native.registry_value()
                old_remember = self.native.remember_value()
                old_chooser = self.native.chooser_value()
                known_ids = {a["steam_id"] for a in rows}
                if previous not in known_ids:
                    previous = next((a["steam_id"] for a in rows if a["login"] == old_registry.get("value")), None)
                if previous is None:
                    previous = next((a["steam_id"] for a in rows if a["recent"]), None)
                patched = authenticated_account(raw, authenticated) if authenticated else select_account(raw, steam_id)
                data = {"version": 1, "root": str(self.native.root), "target": steam_id,
                        "target_login": selected["login"], "previous": previous,
                        "before": base64.b64encode(raw).decode(), "registry": old_registry,
                        "remember": old_remember,
                        "chooser": old_chooser,
                        "selected_hash": hashlib.sha256(patched).hexdigest(), "created_at": time.time()}
                if authenticated:
                    from .accounts import metadata
                    data['verified_target'] = metadata(authenticated)
                    data['desktop'] = {'before': self.token_bridge.snapshot(authenticated), 'selected': self.token_bridge.encoded(authenticated)}
                self.store.write(JOURNAL, data, encrypted=True)
                if self.native.read_logins() != raw:
                    raise SteamError("Данные Steam изменились. Повторите переключение.")
                atomic_write(self.native.login_file, patched)
                if authenticated:
                    self.token_bridge.apply(authenticated['login'], data['desktop']['selected'])
                self.native.set_registry({"exists": True, "value": selected["login"], "kind": 1})
                if authenticated or selected["remembered"]:
                    self.native.set_remember({"exists": True, "value": 1, "kind": 4})
                self.native.set_chooser("0")
                progress("Открываю выбранный аккаунт с сохранённым HWID…" if self.devices else "Открываю выбранный аккаунт…")
                self._launch(steam_id)
            except Exception as error:
                try:
                    if data:
                        self._restore(data)
                    self._launch(data.get('previous') if data else previous)
                    if data:
                        self.store.remove_journal()
                except Exception:
                    raise SteamError("Переключение не завершено. Резервная копия сохранена: нажмите «Вернуть прежний вход».") from None
                if isinstance(error, SteamError):
                    raise error
                raise SteamError("Не удалось переключить Steam. Прежний выбор восстановлен.") from None
            progress("Ожидаю подтверждения входа Steam…")
            deadline = time.monotonic() + self.timeout
            while True:
                if self.native.active_id() == steam_id:
                    if self.devices and not self.devices.is_applied(steam_id):
                        return {'state': 'hwid_lost', 'steam_id': steam_id}
                    self.store.remove_journal()
                    return {"state": "ready", "steam_id": steam_id}
                if time.monotonic() >= deadline:
                    return {"state": "login", "steam_id": steam_id}
                time.sleep(0.5)

    def restore(self, progress=lambda text: None):
        with self.native.exclusive():
            data = self.journal()
            if not data:
                raise SteamError("Нет незавершённого переключения для восстановления.")
            progress("Восстанавливаю прежний вход…")
            self.native.stop()
            try:
                self._restore(data, use_previous_session=True)
                self._launch(data.get('previous'))
                self.store.remove_journal()
            except SteamError:
                raise
            except Exception:
                raise SteamError("Не удалось восстановить выбор Steam. Резервная копия сохранена.") from None
            return {"state": "restored", "steam_id": data.get("previous")}
