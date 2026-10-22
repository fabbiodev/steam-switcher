"""Validate the portable distribution before it can become a release."""
import json
from pathlib import Path


def validate_layout(directory, version):
    directory = Path(directory)
    expected = {'version': version, 'entrypoint': 'SteamSDA.exe',
                'app': 'app/steam_sda', 'runtime': 'runtime', 'assets': 'assets',
                'steamkit': 'integrations/steamkit/SteamSDA.Auth.exe',
                'device': 'integrations/device/SteamSDADevice.dll'}
    manifest = json.loads((directory / 'layout.json').read_text(encoding='utf-8'))
    if manifest != expected:
        raise ValueError('Distribution manifest differs from the expected layout.')
    required = [expected[key] for key in ('entrypoint', 'steamkit', 'device')]
    required += ['runtime/base_library.zip', 'assets/style.qss', 'assets/fonts/Inter-V.ttf',
                 'app/steam_sda/__init__.pyc']
    required += [f'app/steam_sda/{layer}/__init__.pyc'
                 for layer in ('domain', 'application', 'infrastructure', 'presentation')]
    if any(not (directory / path).is_file() for path in required):
        raise ValueError('Distribution is missing application code or runtime resources.')
    entries = list(directory.rglob('*'))
    if any(path.is_symlink() or path.name == '_internal' for path in entries):
        raise ValueError('Legacy or redirected distribution layout is not allowed.')
    return manifest
