"""SteamKit2 private-pipe adapter and lossless Windows desktop-token handoff."""
import base64
from pathlib import Path
import os
import re
import subprocess
import sys
import time
import zlib

from .accounts import AccountVault, claims, json_object, metadata, token_valid
from .errors import SteamError
from .storage import atomic_write, protect
from .vdf import parse, read_value, write_value
from .processes import run_external


MESSAGES = {
    'needs_credentials': 'Для восстановления нужен пароль или действующая мобильная сессия из maFile.',
    'needs_email_guard': 'Steam требует код из письма. Завершите вход через клиент Steam.',
    'needs_device_guard': 'Steam отклонил код Guard. Проверьте maFile и время Windows.',
    'mobile_token_rejected': 'Мобильная сессия maFile отозвана. Укажите пароль для повторного входа.',
    'owner_mismatch': 'Steam подтвердил другой аккаунт. Передача сессии остановлена.',
    'token_rejected': 'Steam отклонил сохранённый токен. Нужен повторный вход.',
    'steam_5': 'Steam отклонил пароль. Проверьте данные входа.',
    'steam_84': 'Steam ограничил попытки входа. Повторите позже.',
    'timeout': 'Steam не ответил вовремя. Повторите позже.',
}


class AuthError(SteamError):
    def __init__(self, code):
        super().__init__(MESSAGES.get(code, 'SteamKit не смог восстановить вход. Проверьте сеть и данные аккаунта.'))
        self.requires_input = code in {'needs_credentials', 'needs_email_guard', 'needs_device_guard',
                                      'mobile_token_rejected', 'owner_mismatch', 'token_rejected', 'steam_5'}
        if isinstance(code, str) and code.startswith('steam_') and code not in {'steam_84', 'steam_20', 'steam_16', 'steam_2', 'steam_3'}:
            self.requires_input = True


def helper_path():
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
    return base / ('steamkit' if hasattr(sys, '_MEIPASS') else 'build/steamkit') / 'SteamSDA.Auth.exe'


class SteamKitAuth:
    def __init__(self, vault, runner=run_external, devices=None):
        self.vault, self.runner = vault, runner
        self.devices = devices

    def authenticate(self, sid, force=False, password_override=None):
        row = self.vault.get(sid)
        if not row:
            raise SteamError('Сначала импортируйте maFile или настройте вход аккаунта.')
        if self.devices:
            self.devices.ensure(row)
            row = self.vault.get(sid)
        session = row.get('session', {})
        mafile = row.get('mafile', {})
        mobile = mafile.get('Session') or {}
        refresh = session.get('refresh_token', '')
        expires = claims(refresh).get('exp', 0)
        password = row.get('password', '') if password_override is None else password_override
        can_relogin = bool(password or token_valid(mobile.get('RefreshToken', ''), sid, 'mobile', True)
                           or token_valid(mobile.get('AccessToken', ''), sid, 'mobile'))
        request = {**metadata(row), 'refresh_token': refresh, 'password': password,
                   'shared_secret': mafile.get('shared_secret', ''), 'guard_data': session.get('guard_data', ''),
                   'mobile_refresh_token': mobile.get('RefreshToken', ''), 'mobile_access_token': mobile.get('AccessToken', ''),
                   'force_new': bool(can_relogin and (force or type(expires) is not int or expires < time.time() + 7 * 86400))}
        if row.get('hwid'):
            request['hwid'] = row['hwid']
        import json
        try:
            result = self.runner([str(helper_path())], input=json.dumps(request), text=True, encoding='utf-8',
                                 capture_output=True, timeout=150, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode != 0:
                raise SteamError('Модуль SteamKit не завершил вход. Данные аккаунта сохранены.')
            response = json_object(result.stdout.encode())
        except FileNotFoundError:
            raise SteamError('Модуль SteamKit не найден. Соберите его через tools/build.py.') from None
        except (OSError, subprocess.TimeoutExpired):
            raise SteamError('Не удалось завершить авторизацию SteamKit. Повторите позже.') from None
        if response.get('ok') is not True:
            code = response.get('error', '')
            raise AuthError(code)
        if (response.get('steam_id') != sid or str(response.get('login', '')).casefold() != row['login'].casefold()
                or not token_valid(response.get('refresh_token', ''), sid, 'client', True)
                or not token_valid(response.get('access_token', ''), sid, 'client')):
            raise SteamError('SteamKit вернул сессию другого владельца или неподходящего типа.')
        # Read again so settings edited before this worker started are retained.
        row = self.vault.get(sid)
        row['session'] = {key: response.get(key, '') for key in ('refresh_token', 'access_token', 'guard_data')}
        row['last_check'], row['next_check'], row['auth_error'] = time.time(), time.time() + 3600, ''
        self.vault.save(row)
        return row


class DesktopToken:
    ROOT = 'MachineUserConfigStore'

    def __init__(self, path=None):
        self.path = Path(path) if path else Path(os.environ['LOCALAPPDATA']) / 'Steam/local.vdf'

    def _read(self):
        # Never follow a reparse point into another user's or application's files.
        for part in (self.path.parent, self.path):
            if part.exists() and (part.is_symlink() or getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
                raise SteamError('Кэш Steam перенаправлен. Передача токена остановлена.')
        if not self.path.exists():
            return b'"MachineUserConfigStore"\n{\n}\n'
        if self.path.stat().st_size > 4 * 1024 * 1024:
            raise SteamError('Кэш входа Steam слишком велик.')
        raw = self.path.read_bytes()
        parse(raw, self.ROOT)
        return raw

    @staticmethod
    def key(login):
        return f'{zlib.crc32(login.encode("utf-8")):x}1'

    def field(self, login):
        return ['Software', 'Valve', 'Steam', 'ConnectCache', self.key(login)]

    def snapshot(self, row):
        value = read_value(self._read(), self.ROOT, self.field(row['login']))
        if value is not None and (not re.fullmatch(r'[a-fA-F0-9]{0,65536}', value) or len(value) % 2):
            raise SteamError('Неизвестный формат кэша Steam. Исходная запись сохранена.')
        return {'value': value, 'path': str(self.path)}

    def selected_token(self, row):
        """Called only for the selected, user-configured account; never scan tokens."""
        value = read_value(self._read(), self.ROOT, self.field(row['login']))
        if value is None:
            return ''
        try:
            token = protect(bytes.fromhex(value), True, entropy=row['login'].encode()).rstrip(b'\x00').decode('utf-8')
        except (ValueError, UnicodeError, SteamError):
            return ''
        return token if token_valid(token, row['steam_id'], 'client', True) else ''

    def encoded(self, row):
        token = row.get('session', {}).get('refresh_token', '')
        if not token_valid(token, row['steam_id'], 'client', True):
            raise SteamError('Нет действующего клиентского токена SteamKit для передачи Steam.')
        return protect(token.encode() + b'\x00', entropy=row['login'].encode(), description='BObfuscateBuffer').hex()

    def apply(self, login, value):
        raw = self._read()
        atomic_write(self.path, write_value(raw, self.ROOT, self.field(login), value))

    def rollback(self, login, snapshot, selected):
        if snapshot.get('path') != str(self.path):
            raise SteamError('Путь резервной копии кэша Steam изменился.')
        current = read_value(self._read(), self.ROOT, self.field(login))
        if current == selected:
            self.apply(login, snapshot.get('value'))
        # Steam may already have refreshed this row; preserve its newer token.
