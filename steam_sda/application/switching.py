"""Account switching and recovery transactions over injected adapters."""
import base64
import hashlib
import re
import time
from steam_sda.domain.errors import SteamError
from steam_sda.domain.identity import valid_id
from .ports import SteamPort, StorePort
from .accounts import capture_native_session

JOURNAL = "switch-recovery.dpapi"


class Switcher:
    def __init__(self, native: SteamPort, store: StorePort, timeout=60, auth=None, vault=None, token_bridge=None, devices=None):
        self.native = native
        self.store = store
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
            rows = self.native.codec.accounts(raw)
            row = next((a for a in rows if a["steam_id"] == data["target"]), None)
            if row is None:
                from steam_sda.domain.accounts import metadata
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
            mapping = data.get('config_account')
            if mapping is not None and (not isinstance(mapping, dict)
                    or 'before' not in mapping
                    or mapping.get('selected') != data['target']
                    or (mapping.get('before') is not None and not valid_id(mapping['before']))):
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
        old_rows, rows = self.native.codec.accounts(original), self.native.codec.accounts(current)
        current_reg = self.native.registry_value()
        if current_reg != data["registry"] and current_reg.get("value") != data["target_login"]:
            raise SteamError("Выбор аккаунта Steam изменён другой программой. Резервная копия сохранена.")
        mapping = data.get('config_account')
        current_mapping = self.native.account_mapping(data['target_login']) if mapping else None
        if mapping and current_mapping not in (mapping['before'], mapping['selected']):
            raise SteamError('Запись аккаунта в config.vdf изменена другой программой. Резервная копия сохранена.')
        if current == original or hashlib.sha256(current).hexdigest() == data["selected_hash"]:
            restored = original
        elif data.get("previous"):
            previous = next((a for a in old_rows if a["steam_id"] == data["previous"]), None)
            now = next((a for a in rows if a["steam_id"] == data["previous"]), None)
            if not previous or not now or previous["login"] != now["login"]:
                raise SteamError("Прежний аккаунт изменён или удалён из Steam. Резервная копия сохранена.")
            # Preserve refreshed credentials, new accounts and every unrelated field.
            restored = self.native.codec.select_account(current, data["previous"])
        else:
            raise SteamError("Файл аккаунтов Steam изменился. Автоматическое восстановление остановлено.")
        registry = data["registry"]
        remember = data.get("remember", {"exists": False})
        if use_previous_session and data.get("previous"):
            previous = next((a for a in old_rows if a["steam_id"] == data["previous"]), None)
            now = next((a for a in rows if a["steam_id"] == data["previous"]), None)
            if not previous or not now or previous["login"] != now["login"]:
                raise SteamError("Прежний аккаунт изменился. Резервная копия сохранена.")
            restored = self.native.codec.select_account(restored, data["previous"])
            registry = {"exists": True, "value": previous["login"], "kind": 1}
            if previous["remembered"]:
                remember = {"exists": True, "value": 1, "kind": 4}
        self.native.write_logins(restored)
        self.native.set_registry(registry)
        self.native.set_remember(remember)
        self.native.set_chooser("0" if use_previous_session else data.get("chooser"))
        if data.get('desktop'):
            self.token_bridge.rollback(data['target_login'], data['desktop']['before'], data['desktop']['selected'])
        if mapping and current_mapping == mapping['selected']:
            self.native.set_account_mapping(data['target_login'], mapping['before'])

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
            # An unchecked "remember me" session can disappear on shutdown.
            # Save only the selected target and current owner before closing it.
            if self.vault and self.token_bridge:
                for sid in dict.fromkeys((steam_id, previous)):
                    native_row = next((a for a in before_rows if a['steam_id'] == sid), None)
                    if not native_row:
                        continue
                    if sid != steam_id and native_row.get('remembered') is not False:
                        continue
                    record = {**self.vault.get(sid), **{key: native_row[key] for key in ('steam_id', 'login', 'name')}}
                    before = {**record, 'session': dict(record.get('session', {}))}
                    capture_native_session(record, self.native, self.token_bridge)
                    if record != before:
                        self.vault.save(record)
                managed = self.vault.get(steam_id)
            progress("Закрываю Steam…")
            self.native.stop()
            data = None
            try:
                authenticated = None
                # A manual switch must hand off the imported session even when
                # background recovery is disabled or Steam remembers the name.
                has_session = bool(managed.get('session', {}).get('refresh_token'))
                if managed and self.auth and (has_session or managed.get('auto_restore')
                        or not any(a['steam_id'] == steam_id for a in before_rows)):
                    progress("Восстанавливаю вход через SteamKit…")
                    authenticated = self.auth.authenticate(steam_id)
                raw = self.native.read_logins()
                rows = self.native.codec.accounts(raw)
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
                patched = self.native.codec.authenticated_account(raw, authenticated) if authenticated else self.native.codec.select_account(raw, steam_id)
                data = {"version": 1, "root": str(self.native.root), "target": steam_id,
                        "target_login": selected["login"], "previous": previous,
                        "before": base64.b64encode(raw).decode(), "registry": old_registry,
                        "remember": old_remember,
                        "chooser": old_chooser,
                        "selected_hash": hashlib.sha256(patched).hexdigest(), "created_at": time.time()}
                if authenticated:
                    from steam_sda.domain.accounts import metadata
                    data['verified_target'] = metadata(authenticated)
                    data['desktop'] = {'before': self.token_bridge.snapshot(authenticated), 'selected': self.token_bridge.encoded(authenticated)}
                    old_mapping = self.native.account_mapping(authenticated['login'])
                    if old_mapping not in (None, steam_id):
                        data = None
                        raise SteamError('Логин в config.vdf связан с другим SteamID. Передача сессии остановлена.')
                    data['config_account'] = {'before': old_mapping, 'selected': steam_id}
                self.store.write(JOURNAL, data, encrypted=True)
                if self.native.read_logins() != raw:
                    raise SteamError("Данные Steam изменились. Повторите переключение.")
                self.native.write_logins(patched)
                if authenticated:
                    self.token_bridge.apply(authenticated['login'], data['desktop']['selected'])
                    self.native.set_account_mapping(authenticated['login'], steam_id)
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
