"""Bounded ZIP/maFile reader and portable account archive writer."""
import io
import json
from pathlib import Path
import zipfile
import pyzipper

from steam_sda.domain.accounts import LIMIT, validate_record, metadata, mafile_record, json_object
from steam_sda.domain.errors import SteamError
from steam_sda.infrastructure.persistence.storage import atomic_write


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


class AccountArchives:
    read = staticmethod(import_account)
    write = staticmethod(export_account)
