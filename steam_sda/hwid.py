"""Account-bound native device profiles. No passwords or tokens in this protocol."""
import copy
import hashlib
import json
import re
import subprocess
import threading
import time
import psutil

from .errors import SteamError
from .processes import run_external


def validate_profile(value, sid):
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value.get('version') != 1
            or value.get('steam_id') != sid or value.get('source') != 'native_collectors'):
        raise SteamError('HWID-профиль принадлежит другому аккаунту или имеет неизвестный формат.')
    result = {'version': 1, 'steam_id': sid, 'source': 'native_collectors'}
    for key, limit in (('machine_guid', 128), ('disk_serial', 512), ('pc_name', 256), ('fqdn', 512)):
        text = value.get(key)
        try:
            length = len(text.encode('utf-8')) if isinstance(text, str) else limit
        except UnicodeError:
            raise SteamError('Некорректная кодировка HWID-профиля.') from None
        if (not isinstance(text, str) or (not text and key != 'fqdn')
                or any(ord(c) < 32 for c in text) or length >= limit):
            raise SteamError('Некорректные компоненты HWID-профиля.')
        result[key] = text
    network = value.get('network_id')
    if not isinstance(network, str) or not re.fullmatch('[a-fA-F0-9]{32}', network):
        raise SteamError('В HWID-профиле должны сохраняться все 16 байт сетевого компонента.')
    result['network_id'] = network.lower()
    stamp = value.get('captured_at')
    if type(stamp) not in (int, float) or not 0 < stamp < 4102444800:
        raise SteamError('Некорректная дата первой привязки HWID.')
    result['captured_at'] = stamp
    return result


def machine_id(profile):
    values = (('BB3', profile['machine_guid'].encode('utf-8')),
              ('FF2', bytes.fromhex(profile['network_id'])),
              ('3B3', profile['disk_serial'].encode('utf-8')))
    return b'\0MessageObject\0' + b''.join(
        b'\1' + name.encode() + b'\0' + hashlib.sha1(raw).hexdigest().encode() + b'\0'
        for name, raw in values) + b'\x08\x08'


def fingerprint(profile):
    return hashlib.sha256(machine_id(profile)).hexdigest()


class DeviceProfiles:
    def __init__(self, vault, native, runner=run_external):
        self.vault, self.native, self.runner = vault, native, runner
        self._lock = threading.RLock()
        self._launches = {}

    def _call(self, mode, profile=None):
        from .steamkit import helper_path
        request = {'steam_path': str(self.native.root)}
        if profile is not None:
            request['hwid'] = profile
        try:
            result = self.runner([str(helper_path()), mode], input=json.dumps(request), text=True,
                                 encoding='utf-8', capture_output=True, timeout=65,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            response = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise SteamError('Модуль HWID не ответил. Соберите программу через tools/build.py; вход остановлен.') from None
        if result.returncode or not isinstance(response, dict) or response.get('ok') is not True:
            code = response.get('error', '') if isinstance(response, dict) else ''
            messages = {
                'device_version': 'Эта версия Steam пока не поддерживается модулем HWID. Привязка сохранена; вход остановлен.',
                'device_capture': 'Не удалось прочитать все компоненты HWID текущего компьютера. Привязка не создана.',
                'device_running': 'Steam уже запущен. Сначала переключите аккаунт через Steam SDA.',
                'device_late': 'Steam загрузил сборщик HWID до установки профиля. Запуск остановлен.',
            }
            raise SteamError(messages.get(code, 'Не удалось применить сохранённый HWID. Запуск остановлен.'))
        return response

    def ensure(self, account):
        return self._ensure(account)

    def bind_many(self, accounts):
        """One hardware snapshot for accounts first discovered in the same scan."""
        with self._lock:
            missing = [a for a in accounts if not self.vault.get(a['steam_id']).get('hwid')]
            if not missing:
                return
            snapshot = self._call('--capture-device').get('hwid', {})
            for account in missing:
                self._ensure(account, snapshot)

    def _ensure(self, account, snapshot=None):
        """First association wins, regardless of maFile/session import order."""
        from .accounts import metadata
        sid = account['steam_id']
        with self._lock:
            row = self.vault.get(sid)
            if row and row['login'].casefold() != account['login'].casefold():
                raise SteamError('HWID нельзя привязать к аккаунту с другим логином.')
            row = row or metadata(account)
            if row.get('hwid'):
                return validate_profile(row['hwid'], sid)
            value = snapshot if snapshot is not None else self._call('--capture-device').get('hwid', {})
            if not isinstance(value, dict):
                raise SteamError('Модуль устройства вернул некорректный HWID-профиль.')
            profile = validate_profile({**value, 'steam_id': sid, 'captured_at': time.time()}, sid)
            row['hwid'] = profile
            self.vault.save(row)
            return copy.deepcopy(profile)

    def preflight(self):
        self._call('--check-device')

    def launch(self, account):
        profile = self.ensure(account)
        response = self._call('--launch-device', profile)
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
        try:
            return any(p.pid == pid and launched - 65 <= p.create_time() <= launched
                       for p in self.native.processes())
        except (OSError, psutil.Error):
            return False
