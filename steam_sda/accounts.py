"""Validated account vault and portable AES ZIP backups; no native cache export."""
import base64
import copy
import io
import json
from pathlib import Path
import re
import time
import zipfile

import pyzipper

from .errors import SteamError
from .storage import atomic_write
from .vdf import valid_id

VAULT = "accounts-vault.dpapi"
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
    return row


class AccountVault:
    def __init__(self, store):
        self.store = store

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
        row = validate_record(record)
        data = self.all()
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
        if row.get('password') and not old.get('password'):
            merged['password'] = row['password']
        self.save(merged)
        return merged


def export_account(path, record, password='', include_secrets=True):
    row = validate_record(record)
    if include_secrets:
        # Login password is deliberately excluded; tokens + maFile are portable.
        row.pop('password', None)
    else:
        row = metadata(row)
    row['auto_restore'] = False
    payload = json.dumps({'format': 'steam-sda-account', 'version': 1, 'account': row}, ensure_ascii=False).encode()
    memory = io.BytesIO()
    with pyzipper.AESZipFile(memory, 'w', compression=pyzipper.ZIP_DEFLATED) as archive:
        if password:
            archive.setpassword(password.encode('utf-8'))
            archive.setencryption(pyzipper.WZ_AES, nbits=256)
        archive.writestr('account.json', payload)
    try:
        atomic_write(path, memory.getvalue())
    except OSError:
        raise SteamError("Не удалось записать архив аккаунта.") from None


def import_account(path, password='', known=()):
    try:
        path = Path(path)
        if path.stat().st_size > LIMIT:
            raise SteamError("Файл импорта слишком велик.")
        raw = path.read_bytes()
        if path.suffix.casefold() == '.mafile':
            return mafile_record(json_object(raw), known)
        with pyzipper.AESZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename != 'account.json' or members[0].file_size > LIMIT:
                raise SteamError("Архив должен содержать только account.json допустимого размера.")
            if password:
                archive.setpassword(password.encode('utf-8'))
            data = json_object(archive.read('account.json'))
        if data.get('format') != 'steam-sda-account' or data.get('version') != 1:
            raise SteamError("Неизвестный формат архива аккаунта.")
        row = validate_record(data.get('account'), known)
        row['auto_restore'] = False
        return row
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError, EOFError):
        raise SteamError("Не удалось открыть архив: проверьте пароль и целостность файла.") from None
