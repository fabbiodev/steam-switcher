"""Only stable, newer releases from the application's own public repository."""
from dataclasses import dataclass
import re
from steam_sda.domain.errors import SteamError

REPOSITORY = 'fabbiodev/steam-switcher'
RELEASES_URL = f'https://github.com/{REPOSITORY}/releases'
MAX_INSTALLER_SIZE = 200 * 1024 * 1024


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,5}\.\d{1,5}\.\d{1,5}', value):
        raise SteamError('Некорректная версия обновления.')
    return tuple(map(int, value.split('.')))


@dataclass(frozen=True)
class UpdateRelease:
    version: str
    url: str
    sha256: str
    size: int

    @property
    def filename(self):
        return f'SteamSwitcher-v{self.version}-Setup-x64.exe'

    def validate(self):
        version_tuple(self.version)
        expected = f'{RELEASES_URL}/download/v{self.version}/{self.filename}'
        if (self.url != expected or not re.fullmatch(r'[a-f0-9]{64}', self.sha256)
                or type(self.size) is not int or not 0 < self.size <= MAX_INSTALLER_SIZE):
            raise SteamError('Некорректные данные установщика обновления.')
        return self


def release_from_github(data, current):
    if not isinstance(data, dict) or data.get('draft') is not False or data.get('prerelease') is not False:
        raise SteamError('GitHub вернул неподходящий выпуск.')
    tag = data.get('tag_name', '')
    if not isinstance(tag, str) or not tag.startswith('v'):
        raise SteamError('Некорректный тег обновления.')
    version = tag[1:]
    if version_tuple(version) <= version_tuple(current):
        return None
    name = f'SteamSwitcher-v{version}-Setup-x64.exe'
    assets = data.get('assets', [])
    if not isinstance(assets, list):
        raise SteamError('Некорректный список файлов выпуска.')
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get('name') == name and asset.get('state') == 'uploaded']
    if len(matches) != 1:
        raise SteamError('В новом выпуске пока нет готового установщика. Повторите проверку позже.')
    asset = matches[0]
    digest = asset.get('digest', '')
    if not isinstance(digest, str) or not digest.startswith('sha256:'):
        raise SteamError('GitHub не опубликовал SHA-256 установщика. Обновление не будет запущено.')
    return UpdateRelease(version, asset.get('browser_download_url', ''), digest[7:], asset.get('size')).validate()
