"""Account catalog, import/export and login settings without Qt or file APIs."""
from pathlib import Path
import time
from steam_sda.domain.accounts import metadata, token_valid
from steam_sda.domain.errors import SteamError
from .ports import AccountRepository, AccountArchivesPort


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
        return rows, managed

    def bind_discovered(self, accounts):
        if self.devices:
            excluded = self.vault.excluded()
            self.devices.bind_many([row for row in accounts if row['steam_id'] not in excluded])
        return self.vault.all()

    def delete(self, sid):
        """Forget local credentials and suppress automatic Steam rediscovery."""
        self.vault.delete(sid)
        if self.devices:
            self.devices.forget(sid)

    def capture_selected_token(self, row):
        if token_valid(row.get('session', {}).get('refresh_token', ''), row['steam_id'], 'client', True):
            return
        try:
            native = self.native.account_list() if self.native else []
            if any(a['steam_id'] == row['steam_id'] and a['login'].casefold() == row['login'].casefold() for a in native):
                token = self.token_bridge.selected_token(row)
                if token:
                    row['session'] = {'refresh_token': token}
        except (SteamError, OSError):
            pass

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
