"""Bounded public Steam profile XML and avatar CDN downloads; no cookies or API key."""
from pathlib import Path
import time
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET
import requests
from steam_sda.domain.errors import SteamError, RateLimitError
from steam_sda.domain.identity import valid_id
from steam_sda.infrastructure.persistence.storage import atomic_write

MAX_IMAGE = 512 * 1024
AVATAR_HOSTS = {'avatars.steamstatic.com', 'avatars.akamai.steamstatic.com',
                'avatars.fastly.steamstatic.com', 'avatars.cloudflare.steamstatic.com'}


def allowed_url(url, profile=False):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.fragment):
            return False
        if profile:
            return parsed.hostname == 'steamcommunity.com' and parsed.path.startswith(('/profiles/', '/id/'))
        return (parsed.hostname in AVATAR_HOSTS or parsed.hostname == 'steamcdn-a.akamaihd.net'
                and parsed.path.startswith('/steamcommunity/public/images/avatars/'))
    except (ValueError, TypeError):
        return False


def avatar_url(xml, sid):
    if (not valid_id(sid) or not isinstance(xml, bytes) or len(xml) > 2 * 1024 * 1024
            or b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper()):
        raise SteamError('Некорректный ответ профиля Steam.')
    try:
        root = ET.fromstring(xml)
        owners = root.findall('steamID64')
        if root.tag != 'profile' or len(owners) != 1 or (owners[0].text or '').strip() != sid:
            raise SteamError('Аватар принадлежит другому профилю Steam.')
        urls = root.findall('avatarMedium') or root.findall('avatarFull')
        if len(urls) != 1 or not allowed_url((urls[0].text or '').strip()):
            raise SteamError('Steam не предоставил допустимый адрес аватара.')
        return urls[0].text.strip()
    except (ET.ParseError, ValueError, TypeError):
        raise SteamError('Не удалось прочитать аватар Steam.') from None


def image_bytes(data):
    return (isinstance(data, bytes) and 0 < len(data) <= MAX_IMAGE
            and (data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8\xff')))


class SteamAvatars:
    def __init__(self, directory, session_factory=requests.Session):
        self.directory, self.session_factory = Path(directory), session_factory

    def _path(self, sid):
        if not valid_id(sid):
            raise SteamError('Некорректный SteamID аватара.')
        path = self.directory / f'{sid}.img'
        for item in (self.directory, path):
            if item.is_symlink() or item.exists() and getattr(item.lstat(), 'st_file_attributes', 0) & 0x400:
                raise SteamError('Кэш аватаров перенаправлен.')
        return path

    def read(self, sid):
        try:
            path = self._path(sid)
            stat = path.stat()
            if stat.st_size > MAX_IMAGE:
                return {}
            data = path.read_bytes()
            return {'data': data, 'updated_at': stat.st_mtime} if image_bytes(data) else {}
        except (OSError, SteamError):
            return {}

    def save(self, sid, data):
        if not image_bytes(data):
            raise SteamError('Некорректное изображение аватара.')
        try:
            atomic_write(self._path(sid), data)
        except OSError:
            raise SteamError('Не удалось сохранить аватар в кэш.') from None

    def _download(self, session, url, limit, profile, cancelled):
        for _ in range(5):
            if cancelled():
                raise SteamError('Загрузка аватара отменена.')
            if not allowed_url(url, profile):
                raise SteamError('Недопустимый адрес аватара Steam.')
            with session.get(url, timeout=(5, 10), allow_redirects=False, stream=True) as response:
                if response.status_code == 429:
                    try:
                        delay = int(response.headers.get('Retry-After', '300'))
                    except (TypeError, ValueError):
                        delay = 300
                    raise RateLimitError(delay)
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('Location')
                    if not location:
                        raise SteamError('Steam не предоставил адрес аватара.')
                    url = urljoin(url, location)
                    continue
                if response.status_code != 200:
                    raise SteamError('Аватар Steam временно недоступен.')
                data, started = bytearray(), time.monotonic()
                for chunk in response.iter_content(65536):
                    if cancelled() or time.monotonic() - started > 30:
                        raise SteamError('Загрузка аватара отменена или превысила время ожидания.')
                    data.extend(chunk)
                    if len(data) > limit:
                        raise SteamError('Изображение аватара слишком велико.')
                return bytes(data)
        raise SteamError('Слишком много перенаправлений аватара Steam.')

    def fetch(self, sid, cancelled=lambda: False):
        if not valid_id(sid):
            raise SteamError('Некорректный SteamID аватара.')
        try:
            with self.session_factory() as session:
                session.trust_env = False
                xml = self._download(session, f'https://steamcommunity.com/profiles/{sid}/?xml=1',
                                     2 * 1024 * 1024, True, cancelled)
                data = self._download(session, avatar_url(xml, sid), MAX_IMAGE, False, cancelled)
                if not image_bytes(data):
                    raise SteamError('Steam вернул неподдерживаемое изображение аватара.')
                return data
        except requests.RequestException:
            raise SteamError('Не удалось загрузить аватар Steam.') from None
