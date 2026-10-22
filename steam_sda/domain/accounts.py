"""Validation and ownership rules for accounts, sessions and maFiles."""
import base64
import copy
import json
import re
import time

from steam_sda.domain.errors import SteamError
from .identity import valid_id
from .device import validate_profile

LIMIT = 2 * 1024 * 1024


def json_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        if len(raw) > LIMIT:
            raise ValueError()
        result = json.loads(raw, object_pairs_hook=pairs)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise SteamError("Некорректный JSON аккаунта или maFile.") from None


def claims(token):
    """Untrusted JWT metadata; authentication is always checked by SteamKit/Steam."""
    try:
        if not isinstance(token, str) or not 20 < len(token) < 32768 or len(token.split('.')) != 3:
            return {}
        payload = token.split('.')[1]
        return json_object(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    except (ValueError, SteamError):
        return {}


def token_valid(token, steam_id, audience, refresh=False, margin=0):
    data = claims(token)
    aud = data.get('aud', [])
    return (str(data.get('sub')) == steam_id and isinstance(aud, list) and audience in aud
            and ('derive' in aud) == refresh and type(data.get('exp')) is int
            and data['exp'] > time.time() + margin)


def metadata(value):
    if not isinstance(value, dict) or not valid_id(value.get('steam_id')):
        raise SteamError("В аккаунте отсутствует корректный SteamID.")
    login = value.get('login', '')
    name = value.get('name', login)
    if (not isinstance(login, str) or not re.fullmatch(r'[a-zA-Z0-9_]{1,64}', login)
            or not isinstance(name, str) or len(name) > 256 or any(ord(c) < 32 for c in name)):
        raise SteamError("Некорректное имя или логин аккаунта.")
    return {'steam_id': value['steam_id'], 'login': login.casefold(), 'name': name or login}


def mafile_record(value, known=()):
    if not isinstance(value, dict):
        raise SteamError("Не удалось прочитать maFile.")
    try:
        secret = base64.b64decode(value.get('shared_secret', ''), validate=True)
        if len(secret) != 20:
            raise ValueError()
    except (ValueError, TypeError):
        raise SteamError("В maFile отсутствует shared_secret Steam Guard. Зашифрованный maFile сначала экспортируйте из SDA.") from None
    session = value.get('Session') or {}
    if not isinstance(session, dict):
        raise SteamError("Некорректная сессия в maFile.")
    login = value.get('account_name', '')
    ids = {str(v) for v in [session.get('SteamID'), value.get('steamid'), value.get('SteamID')] if v not in (None, '', 0, '0')}
    for field in ('AccessToken', 'RefreshToken'):
        token = session.get(field, '')
        if token and not isinstance(token, str):
            raise SteamError("Некорректный токен в maFile.")
        subject = claims(token).get('sub')
        if subject is not None:
            ids.add(str(subject))
    matches = [a for a in known if isinstance(login, str) and a['login'].casefold() == login.casefold()]
    if not ids and len(matches) == 1:
        ids.add(matches[0]['steam_id'])
    if len(ids) != 1 or not valid_id(next(iter(ids))):
        raise SteamError("SteamID maFile отсутствует или не совпадает с владельцем токенов.")
    sid = ids.pop()
    matching = next((a for a in known if a['steam_id'] == sid), None)
    if matching and matching['login'].casefold() != str(login).casefold():
        raise SteamError("Логин maFile не совпадает с сохранённым аккаунтом Steam.")
    row = metadata({'steam_id': sid, 'login': login, 'name': matching['name'] if matching else login})
    # Preserve authenticator fields, never unrelated passwords/proxies/extensions.
    if len(json.dumps(value).encode()) > 1024 * 1024:
        raise SteamError("maFile слишком велик.")
    allowed = {'shared_secret', 'identity_secret', 'secret_1', 'revocation_code', 'serial_number',
               'uri', 'server_time', 'account_name', 'token_gid', 'device_id', 'fully_enrolled',
               'status', 'SteamID', 'steamid'}
    row['mafile'] = {key: copy.deepcopy(item) for key, item in value.items() if key in allowed}
    row['mafile']['Session'] = {key: copy.deepcopy(item) for key, item in session.items()
                             if key in {'SteamID', 'SessionID', 'SteamLogin', 'SteamLoginSecure',
                                        'OAuthToken', 'AccessToken', 'RefreshToken'}}
    row['auto_restore'] = True
    row['session'] = {}
    return row


def validate_record(value, known=()):
    row = metadata(value)
    for other in known:
        same_id = other['steam_id'] == row['steam_id']
        same_login = other['login'].casefold() == row['login'].casefold()
        if same_id != same_login:
            raise SteamError("Логин и SteamID архива не совпадают с сохранённым аккаунтом.")
    if value.get('mafile'):
        imported = mafile_record(value['mafile'], known)
        if imported['steam_id'] != row['steam_id'] or imported['login'].casefold() != row['login'].casefold():
            raise SteamError("maFile в архиве принадлежит другому аккаунту.")
        row['mafile'] = imported['mafile']
    session = value.get('session', {})
    if not isinstance(session, dict):
        raise SteamError("Некорректная сохранённая сессия.")
    row['session'] = {}
    for key in ('refresh_token', 'access_token', 'guard_data'):
        token = session.get(key, '')
        if not isinstance(token, str) or len(token) > 32768:
            raise SteamError("Некорректный токен аккаунта.")
        if token:
            if key != 'guard_data' and str(claims(token).get('sub')) != row['steam_id']:
                raise SteamError("Токен архива принадлежит другому аккаунту.")
            row['session'][key] = token
    password = value.get('password', '')
    if not isinstance(password, str) or len(password) > 1024:
        raise SteamError("Некорректные данные входа.")
    if password:
        row['password'] = password
    row['auto_restore'] = value.get('auto_restore') is True
    for key in ('last_check', 'next_check'):
        number = value.get(key, 0)
        if type(number) in (int, float) and 0 <= number < time.time() + 86400:
            row[key] = number
    error = value.get('auth_error', '')
    if isinstance(error, str):
        row['auth_error'] = error[:500]
    if 'hwid' in value:
        row['hwid'] = validate_profile(value['hwid'], row['steam_id'])
    return row
