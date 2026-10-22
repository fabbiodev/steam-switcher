"""Read/write only the selected desktop token with native DPAPI semantics."""
import os
from pathlib import Path
import re
import zlib
from steam_sda.domain.accounts import token_valid
from steam_sda.domain.errors import SteamError
from steam_sda.infrastructure.persistence.storage import atomic_write, protect
from steam_sda.infrastructure.steam.vdf import parse, read_value, write_value


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
