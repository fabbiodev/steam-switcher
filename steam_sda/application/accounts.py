"""Account catalog, import/export and login settings without Qt or file APIs."""
from pathlib import Path
import time
from steam_sda.domain.accounts import metadata, token_valid, session_status
from steam_sda.domain.errors import SteamError
from .ports import AccountRepository, AccountArchivesPort


def capture_native_session(row, native, token_bridge):
    """Refresh only this owner's token after Steam may have rotated it."""
    try:
        accounts = native.account_list() if native else []
        if any(a['steam_id'] == row['steam_id'] and a['login'].casefold() == row['login'].casefold() for a in accounts):
            token = token_bridge.selected_token(row)
            if token_valid(token, row['steam_id'], 'client', True):
                if token != row.get('session', {}).get('refresh_token'):
                    row['session'] = {'refresh_token': token}
                    for key in ('last_check', 'next_check', 'auth_error'):
                        row.pop(key, None)
                native_row = next(a for a in accounts if a['steam_id'] == row['steam_id'])
                row['session_origin'] = 'steam_temporary' if native_row.get('remembered') is False else 'steam_cache'
    except (SteamError, OSError):
        pass


class AccountService:
    def __init__(self, vault: AccountRepository, archives: AccountArchivesPort, native, token_bridge, devices=None):
        self.vault, self.archives = vault, archives
        self.native, self.token_bridge, self.devices = native, token_bridge, devices

    def managed(self):
        return self.vault.all()

    def catalog(self):
        rows = self.native.account_list() if self.native else []
        excluded = self.vault.excluded()
        rows = [row for row in rows if row['steam_id'] not in excluded]
        managed = self.vault.all()
        native_ids = {a['steam_id'] for a in rows}
        rows.extend({**metadata(a), 'remembered': False, 'imported': True}
                    for a in managed.values() if a['steam_id'] not in native_ids)
        active = self.native.active_id() if self.native else None
        for row in rows:
            saved = managed.get(row['steam_id'], {})
            label, portable, temporary = session_status(row, saved, row['steam_id'] == active)
            row['temporary_session'] = temporary
            row['session_saved'] = portable
            row['session_label'] = label
        return rows, managed

    def bind_discovered(self, accounts):
        excluded = self.vault.excluded()
        if self.devices:
            self.devices.bind_many([row for row in accounts if row['steam_id'] not in excluded])
        active = self.native.active_id() if self.native else None
        row = next((row for row in accounts if row['steam_id'] == active and row.get('remembered') is False
                    and row['steam_id'] not in excluded), None)
        if row:
            self.save_session(row, required=False)
        return self.vault.all()

    def save_session(self, account, required=True):
        if account['steam_id'] in self.vault.excluded():
            raise SteamError('Этот аккаунт удалён из базы. Сначала импортируйте его явно.')
        row = {**self.vault.get(account['steam_id']), **metadata(account)}
        before = {**row, 'session': dict(row.get('session', {}))}
        capture_native_session(row, self.native, self.token_bridge)
        available = token_valid(row.get('session', {}).get('refresh_token', ''), row['steam_id'], 'client', True)
        if not available:
            if required:
                raise SteamError('Steam не сохранил переносимый токен этого входа. Для сохранения сессии используйте «Настроить автовход»: пароль или действующую сессию maFile.')
            return None
        if self.devices:
            self.devices.ensure(row)
        if row != before:
            self.vault.save(row)
        return self.vault.get(row['steam_id'])

    def delete(self, sid):
        """Forget local credentials and suppress automatic Steam rediscovery."""
        self.vault.delete(sid)
        if self.devices:
            self.devices.forget(sid)

    def capture_selected_token(self, row):
        # Steam may rotate its durable token while the vault's old token still
        # has an unexpired JWT timestamp. Prefer the selected native cache.
        capture_native_session(row, self.native, self.token_bridge)

    def import_file(self, path, password='', known=()):
        row = self.archives.read(path, password, known)
        merged = self.vault.import_record(row)
        if self.devices:
            self.devices.ensure(merged)
            merged = self.vault.get(row['steam_id'])
        if Path(path).suffix.casefold() == '.mafile':
            merged['auto_restore'] = True
            self.capture_selected_token(merged)
            self.vault.save(merged)
        return merged

    def prepare_export(self, account):
        row = {**self.vault.get(account['steam_id']), **metadata(account)}
        if self.devices:
            self.devices.ensure(row)
            row = self.vault.get(row['steam_id'])
        self.capture_selected_token(row)
        return row

    def export_file(self, path, row, password='', include_secrets=True):
        self.archives.write(path, row, password, include_secrets)

    def import_all_file(self, path, password='', known=()):
        if Path(path).suffix.casefold() == '.mafile':
            return [self.import_file(path, password, known)]
        rows = self.archives.read_all(path, password, known)
        if self.devices:
            rows = self.devices.prepare_import(rows)
        return self.vault.import_records(rows)

    def prepare_all_export(self):
        rows, _ = self.catalog()
        if not rows:
            raise SteamError('В базе нет аккаунтов для экспорта.')
        if self.devices:
            self.devices.bind_many(rows)
        return [self.prepare_export(row) for row in rows]

    def export_all_file(self, path, rows, password='', include_secrets=True):
        self.archives.write_all(path, rows, password, include_secrets)

    def configure_login(self, account, auto_restore, password=None, remember=False, clear_password=False):
        row = {**self.vault.get(account['steam_id']), **metadata(account)}
        row['auto_restore'] = auto_restore
        if clear_password:
            row.pop('password', None)
        if password and remember:
            row['password'] = password
        self.capture_selected_token(row)
        self.vault.save(row)
        return row

    def record_auth_failure(self, sid, error, requires_input=False):
        row = self.vault.get(sid)
        if row:
            row['auth_error'], row['next_check'] = error, time.time() + 900
            if requires_input:
                row['auto_restore'] = False
            self.vault.save(row)
