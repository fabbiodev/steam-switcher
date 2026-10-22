"""Windows integration check: custom directory, detected update and uninstall."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from steam_sda import __version__
from steam_sda.infrastructure.updates.github import UNINSTALL_KEY, installation_directory
from tools.distribution import validate_layout


def registration():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            return {name: winreg.QueryValueEx(key, name)[0] for name in ('InstallLocation', 'DisplayVersion')}
    except FileNotFoundError:
        return None


def run(executable, args, timeout=180):
    result = subprocess.run([str(executable), *args], timeout=timeout, capture_output=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode != 0:
        raise RuntimeError(f'Installer verification process failed (exit {result.returncode}).')


def verify(installer, report):
    if os.name != 'nt':
        raise RuntimeError('Installer checks require Windows.')
    if registration() is not None:
        raise RuntimeError('An existing registered installation was found; it will not be modified by this test.')
    installer = Path(installer).resolve()
    workspace = Path(tempfile.mkdtemp(prefix='SteamSDA-installer-check-')).resolve()
    # All installer-owned paths are confined to this new, dedicated directory.
    destination = (workspace / 'custom directory/Steam SDA').resolve()
    if not destination.is_relative_to(workspace):
        raise RuntimeError('Unsafe installer test directory.')
    default = Path(os.environ['LOCALAPPDATA']) / 'Programs/Steam SDA'
    default_existed = default.exists()
    app_data = Path(os.environ['APPDATA']) / 'SteamSDA'
    app_data.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix='installer-check-', suffix='.txt', dir=app_data, delete=False) as stream:
        marker = Path(stream.name)
        stream.write(b'Synthetic preservation check; no account credentials.')
    marker_hash = hashlib.sha256(marker.read_bytes()).hexdigest()
    flags = ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/SP-', '/NORESTART', '/NOCLOSEAPPLICATIONS']
    installed = False
    try:
        run(installer, [*flags, f'/DIR={destination}', f'/LOG={workspace / "install.log"}'])
        installed = True
        entry = registration()
        if not entry or Path(entry['InstallLocation']).resolve() != destination or entry['DisplayVersion'] != __version__:
            raise RuntimeError('Installer did not register its selected directory and version.')
        if installation_directory() != destination:
            raise RuntimeError('The application updater did not detect the installation.')
        validate_layout(destination, __version__)
        style = destination / 'assets/style.qss'
        expected_style = style.read_bytes()
        style.write_bytes(expected_style + b'\n/* installer replacement fixture */\n')
        # No /DIR on the next run: Inno must use its previous installation record.
        run(installer, [*flags, f'/LOG={workspace / "upgrade.log"}'])
        if Path(registration()['InstallLocation']).resolve() != destination or style.read_bytes() != expected_style:
            raise RuntimeError('Reinstallation did not reuse and update the existing installation.')
        if not default_existed and default.exists():
            raise RuntimeError('Reinstallation created an unwanted second installation.')
        env = {**os.environ, 'QT_QPA_PLATFORM': 'offscreen'}
        check = workspace / 'installed-ui.json'
        result = subprocess.run([str(destination / 'SteamSDA.exe'), '--check-ui', str(check)], env=env,
                                cwd=workspace, timeout=120, capture_output=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        ui = json.loads(check.read_text(encoding='utf-8')) if check.exists() else {}
        if result.returncode != 0 or not ui.get('ok') or ui.get('app_version') != __version__:
            raise RuntimeError('Installed executable verification failed.')
        run(destination / 'unins000.exe', flags)
        installed = False
        if registration() is not None or (destination / 'SteamSDA.exe').exists():
            raise RuntimeError('Uninstall did not remove the application and registration.')
        if not marker.exists() or hashlib.sha256(marker.read_bytes()).hexdigest() != marker_hash:
            raise RuntimeError('Installer or uninstaller modified application data.')
        summary = {'ok': True, 'app_version': __version__, 'custom_directory': True,
                   'previous_directory_detected': True, 'application_detects_installation': True,
                   'files_replaced': True, 'no_duplicate_installation': True,
                   'installed_exe': ui['ok'], 'uninstall': True, 'app_data_preserved': True}
        Path(report).parent.mkdir(parents=True, exist_ok=True)
        Path(report).write_text(json.dumps(summary, indent=2), encoding='utf-8')
        print(json.dumps(summary))
    finally:
        # Only invoke this test's uninstaller, never a pre-existing installation.
        if installed and (destination / 'unins000.exe').is_file():
            entry = registration()
            if entry and Path(entry['InstallLocation']).resolve() == destination:
                run(destination / 'unins000.exe', flags)
        marker.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--installer', required=True)
    parser.add_argument('--report', default=str(ROOT / 'build/installer-check.json'))
    args = parser.parse_args()
    verify(args.installer, args.report)
