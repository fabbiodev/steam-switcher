"""Validation and serialization of an account-bound device identity."""
import hashlib
import re

from steam_sda.domain.errors import SteamError


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
