"""DPAPI-backed implementation of the account repository."""
import json
import threading
from steam_sda.domain.accounts import validate_record, validate_records, metadata, token_valid
from steam_sda.domain.errors import SteamError
from steam_sda.domain.identity import valid_id

VAULT = "accounts-vault.dpapi"


class AccountVault:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()

    def all(self):
        data = self.store.read(VAULT, encrypted=True)
        data.pop('_removed', None)
        result = {}
        for sid, value in data.items():
            row = validate_record(value)
            if row['steam_id'] != sid:
                raise SteamError("Некорректный индекс защищённых аккаунтов.")
            result[sid] = row
        return result

    def excluded(self):
        values = self.store.read(VAULT, encrypted=True).get('_removed', [])
        if not isinstance(values, list) or any(not valid_id(sid) for sid in values):
            raise SteamError('Некорректный список удалённых аккаунтов.')
        return set(values)

    def delete(self, sid):
        if not valid_id(sid):
            raise SteamError('Некорректный SteamID.')
        with self.lock:
            data, removed = self.all(), self.excluded()
            data.pop(sid, None)
            removed.add(sid)
            self.store.write(VAULT, {**data, '_removed': sorted(removed)}, encrypted=True)

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
        removed = self.excluded() - {row['steam_id']}
        if removed:
            data['_removed'] = sorted(removed)
        if len(json.dumps(data, ensure_ascii=False).encode()) > 6 * 1024 * 1024:
            raise SteamError('Хранилище аккаунтов слишком велико. Исходные данные сохранены.')
        self.store.write(VAULT, data, encrypted=True)

    def import_record(self, record):
        return self.import_records([record])[0]

    def import_records(self, records):
        """Validate and merge the entire batch before one encrypted write."""
        with self.lock:
            data, removed = self.all(), self.excluded()
            rows = validate_records(records, list(data.values()))
            merged = []
            for row in rows:
                result = validate_record(self._merge_import(row, data.get(row['steam_id'], {})))
                data[row['steam_id']] = result
                removed.discard(row['steam_id'])
                merged.append(result)
            if removed:
                data['_removed'] = sorted(removed)
            if len(json.dumps(data, ensure_ascii=False).encode()) > 6 * 1024 * 1024:
                raise SteamError('Хранилище аккаунтов слишком велико. Исходные данные сохранены.')
            self.store.write(VAULT, data, encrypted=True)
            return merged

    @staticmethod
    def _merge_import(row, old):
        if old and old['login'].casefold() != row['login'].casefold():
            raise SteamError("Логин импортируемого аккаунта отличается от сохранённого.")
        # Import cannot silently enable automation or replace a working token.
        merged = {**old, **metadata(row), 'auto_restore': old.get('auto_restore', False)}
        if row.get('mafile'):
            merged['mafile'] = row['mafile']
        previous_session = old.get('session', {})
        merged['session'] = (previous_session if token_valid(previous_session.get('refresh_token', ''),
                             row['steam_id'], 'client', True) else row.get('session') or previous_session)
        if old.get('hwid') or row.get('hwid'):
            merged['hwid'] = old.get('hwid') or row['hwid']
        if row.get('password') and not old.get('password'):
            merged['password'] = row['password']
        return merged
