"""First-computer binding and per-account device lifecycle."""
import copy
import threading
import time
from steam_sda.domain.device import validate_profile, fingerprint
from steam_sda.domain.errors import SteamError
from .ports import AccountRepository, DeviceGateway


class DeviceProfiles:
    def __init__(self, vault: AccountRepository, gateway: DeviceGateway):
        self.vault, self.gateway = vault, gateway
        self._lock = threading.RLock()
        self._launches = {}

    def ensure(self, account):
        return self._ensure(account)

    def bind_many(self, accounts):
        """One hardware snapshot for accounts first discovered in the same scan."""
        with self._lock:
            missing = [a for a in accounts if not self.vault.get(a['steam_id']).get('hwid')]
            if not missing:
                return
            snapshot = self.gateway.call('--capture-device').get('hwid', {})
            for account in missing:
                self._ensure(account, snapshot)

    def _ensure(self, account, snapshot=None):
        """First association wins, regardless of maFile/session import order."""
        from steam_sda.domain.accounts import metadata
        sid = account['steam_id']
        with self._lock:
            row = self.vault.get(sid)
            if row and row['login'].casefold() != account['login'].casefold():
                raise SteamError('HWID нельзя привязать к аккаунту с другим логином.')
            row = row or metadata(account)
            if row.get('hwid'):
                return validate_profile(row['hwid'], sid)
            value = snapshot if snapshot is not None else self.gateway.call('--capture-device').get('hwid', {})
            if not isinstance(value, dict):
                raise SteamError('Модуль устройства вернул некорректный HWID-профиль.')
            profile = validate_profile({**value, 'steam_id': sid, 'captured_at': time.time()}, sid)
            row['hwid'] = profile
            self.vault.save(row)
            return copy.deepcopy(profile)

    def preflight(self):
        self.gateway.call('--check-device')

    def launch(self, account):
        profile = self.ensure(account)
        response = self.gateway.call('--launch-device', profile)
        if (response.get('hooks_ready') is not True or response.get('before_main') is not True
                or response.get('machine_id_sha256') != fingerprint(profile)
                or type(response.get('steam_pid')) is not int or response['steam_pid'] <= 0):
            raise SteamError('Модуль запуска не подтвердил сохранённый HWID.')
        self._launches[account['steam_id']] = (response['steam_pid'], time.time())
        return response

    def is_applied(self, sid):
        """A Steam self-restart does not inherit an injected module."""
        ticket = self._launches.get(sid)
        if not ticket:
            return False
        pid, launched = ticket
        return self.gateway.is_applied(pid, launched)
