"""DPAPI-backed implementation of the account repository."""
import json
import threading
from steam_sda.domain.accounts import validate_record, metadata
from steam_sda.domain.errors import SteamError

VAULT = "accounts-vault.dpapi"


class AccountVault:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()

    def all(self):
        data = self.store.read(VAULT, encrypted=True)
        result = {}
        for sid, value in data.items():
            row = validate_record(value)
            if row['steam_id'] != sid:
                raise SteamError("Некорректный индекс защищённых аккаунтов.")
            result[sid] = row
        return result

    def get(self, sid):
        return self.all().get(sid, {})

    def save(self, record):
        with self.lock:
            return self._save(record)

    def _save(self, record):
        row = validate_record(record)
        data = self.all()
        previous = data.get(row['steam_id'], {}).get('hwid')
        if previous:
            if row.get('hwid') and row['hwid'] != previous:
                raise SteamError('У аккаунта уже сохранён HWID первой привязки. Он не был заменён.')
            row['hwid'] = previous
        conflict = next((a for a in data.values() if a['steam_id'] != row['steam_id'] and a['login'].casefold() == row['login'].casefold()), None)
        if conflict:
            raise SteamError("Такой логин уже связан с другим SteamID.")
        data[row['steam_id']] = row
        if len(json.dumps(data, ensure_ascii=False).encode()) > 6 * 1024 * 1024:
            raise SteamError('Хранилище аккаунтов слишком велико. Исходные данные сохранены.')
        self.store.write(VAULT, data, encrypted=True)

    def import_record(self, record):
        row = validate_record(record)
        old = self.get(row['steam_id'])
        if old and old['login'].casefold() != row['login'].casefold():
            raise SteamError("Логин импортируемого аккаунта отличается от сохранённого.")
        # Import cannot silently enable automation or replace a working token.
        merged = {**old, **metadata(row), 'auto_restore': old.get('auto_restore', False)}
        if row.get('mafile'):
            merged['mafile'] = row['mafile']
        merged['session'] = old.get('session') or row.get('session', {})
        if old.get('hwid') or row.get('hwid'):
            merged['hwid'] = old.get('hwid') or row['hwid']
        if row.get('password') and not old.get('password'):
            merged['password'] = row['password']
        self.save(merged)
        return merged
