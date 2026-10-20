"""Package only the built app and public documentation, never local account data."""
import argparse
import hashlib
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from steam_sda import __version__


def package(root, tag):
    root = Path(root)
    if not re.fullmatch(r'v\d+\.\d+\.\d+', tag) or tag != f'v{__version__}':
        raise ValueError('Release tag must match the application version.')
    source = root / 'dist/SteamSDA'
    if not (source / 'SteamSDA.exe').is_file():
        raise ValueError('Build SteamSDA.exe before packaging.')
    output = root / 'build/release'
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'SteamSwitcher-{tag}-win-x64.zip'
    public_docs = ('README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md')
    for name in public_docs:
        if not (root / name).is_file():
            raise ValueError(f'Missing release documentation: {name}')
    files = sorted(path for path in source.rglob('*') if path.is_file())
    for path in files:
        runtime_zip = path.relative_to(source).as_posix() == '_internal/base_library.zip'
        if path.is_symlink() or path.suffix.casefold() in {'.mafile', '.dpapi'} or (path.suffix.casefold() == '.zip' and not runtime_zip):
            raise ValueError('Unexpected private or redirected file in the build.')
        if path.name.casefold() in {'accounts.json', 'levels.json', 'settings.json', '.env', 'loginusers.vdf', 'local.vdf'}:
            raise ValueError('Local account data cannot be included in a release.')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in files:
            bundle.write(path, Path('SteamSwitcher') / path.relative_to(source))
        for name in public_docs:
            bundle.write(root / name, Path('SteamSwitcher') / name)
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    checksum = output / 'SHA256SUMS.txt'
    checksum.write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    return archive, checksum


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', required=True)
    args = parser.parse_args()
    archive, checksum = package(ROOT, args.tag)
    print(archive.name)
    print(checksum.name)
