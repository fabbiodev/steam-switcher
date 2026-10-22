"""No credentials are sent: GitHub public release metadata and assets only."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import requests
from steam_sda.domain.errors import SteamError
from steam_sda.domain.updates import REPOSITORY, release_from_github
from steam_sda.infrastructure.ipc.processes import start_external

UNINSTALL_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\SteamSDA.Desktop_is1'


def installation_directory(executable=None):
    """Prefer Windows registration; portable copies can be upgraded in place."""
    if os.name != 'nt':
        raise SteamError('Обновление поддерживается в Windows.')
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            value = winreg.QueryValueEx(key, 'InstallLocation')[0]
        path = Path(value).resolve()
        if (path / 'SteamSDA.exe').is_file():
            return path
    except (OSError, TypeError, ValueError):
        pass
    executable = Path(executable or sys.executable).resolve()
    if executable.name.casefold() == 'steamsda.exe' and executable.is_file():
        return executable.parent
    raise SteamError('Для установки обновления запустите готовую сборку Steam SDA, а не исходники.')


class GitHubUpdates:
    def __init__(self, directory=None, session_factory=requests.Session, launcher=start_external):
        base = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir()))
        self.directory = Path(directory) if directory else base / 'SteamSDA/updates'
        self.session_factory, self.launcher = session_factory, launcher

    def latest(self, current, cancelled=lambda: False):
        if cancelled():
            raise SteamError('Проверка обновлений отменена.')
        try:
            with self.session_factory() as session:
                session.trust_env = False
                session.headers.update({'Accept': 'application/vnd.github+json', 'User-Agent': f'SteamSDA/{current}',
                                        'X-GitHub-Api-Version': '2026-03-10'})
                # Requests must not pick up credentials from the user's .netrc.
                response = session.get(f'https://api.github.com/repos/{REPOSITORY}/releases/latest',
                                       timeout=(5, 15), stream=True, auth=())
                with response:
                    response.raise_for_status()
                    raw = bytearray()
                    for chunk in response.iter_content(65536):
                        if cancelled():
                            raise SteamError('Проверка обновлений отменена.')
                        raw.extend(chunk)
                        if len(raw) > 2 * 1024 * 1024:
                            raise SteamError('Слишком большой ответ сервера обновлений.')
                    import json
                    return release_from_github(json.loads(raw), current)
        except (requests.RequestException, ValueError, TypeError):
            raise SteamError('Не удалось проверить обновления. Проверьте интернет и повторите позже.') from None

    def _target(self, release):
        release.validate()
        target = self.directory / release.filename
        if self.directory.is_symlink() or target.is_symlink():
            raise SteamError('Папка обновлений перенаправлена. Обновление остановлено.')
        return target

    @staticmethod
    def _verified(path, release):
        if not path.is_file() or path.stat().st_size != release.size:
            return False
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest() == release.sha256

    def download(self, release, progress=lambda value: None, cancelled=lambda: False):
        target = self._target(release)
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if self._verified(target, release):
                progress(100)
                return str(target)
            with self.session_factory() as session:
                session.trust_env = False
                with session.get(release.url, timeout=(5, 15), stream=True, auth=()) as response:
                    response.raise_for_status()
                    final = urlsplit(response.url)
                    if final.scheme != 'https' or final.hostname not in ('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
                        raise SteamError('Недопустимый адрес загрузки обновления.')
                    started, count, digest = time.monotonic(), 0, hashlib.sha256()
                    with tempfile.NamedTemporaryFile(prefix='download-', suffix='.part', dir=self.directory, delete=False) as stream:
                        temporary = Path(stream.name)
                        for chunk in response.iter_content(256 * 1024):
                            if cancelled():
                                raise SteamError('Загрузка обновления отменена.')
                            if time.monotonic() - started > 600:
                                raise SteamError('Время загрузки обновления истекло.')
                            count += len(chunk)
                            if count > release.size:
                                raise SteamError('Размер установщика не совпадает с выпуском.')
                            stream.write(chunk)
                            digest.update(chunk)
                            progress(int(count * 100 / release.size))
                        stream.flush()
                        os.fsync(stream.fileno())
                    if count != release.size or digest.hexdigest() != release.sha256:
                        raise SteamError('SHA-256 или размер установщика не совпадает. Обновление не будет запущено.')
                    os.replace(temporary, target)
                    return str(target)
        except (requests.RequestException, OSError):
            raise SteamError('Не удалось скачать обновление. Проверьте интернет и свободное место.') from None
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def install(self, release, path):
        target = self._target(release)
        try:
            if Path(path).resolve() != target.resolve() or not self._verified(target, release):
                raise SteamError('Проверка установщика перед запуском не пройдена. Скачайте обновление заново.')
            destination = installation_directory()
            env = {key: value for key, value in os.environ.items() if not key.startswith('_PYI_') and key != '_MEIPASS2'}
            env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
            self.launcher([str(target), '/SILENT', '/SP-', '/NORESTART', '/NOCLOSEAPPLICATIONS',
                           '/RELAUNCH=1', f'/DIR={destination}'], close_fds=True,
                          env=env,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError:
            raise SteamError('Не удалось запустить установщик обновления.') from None
