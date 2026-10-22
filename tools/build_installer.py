"""Compile the installer from the same validated portable ZIP as the release."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from steam_sda import __version__
from tools.distribution import validate_layout
from tools.package_release import write_checksums


def find_compiler():
    command = shutil.which('ISCC.exe')
    if command:
        return command
    for base in ('ProgramFiles(x86)', 'ProgramFiles'):
        for version in (6, 7):
            candidate = Path(os.environ.get(base, 'C:/Program Files (x86)')) / f'Inno Setup {version}/ISCC.exe'
            if candidate.is_file():
                return str(candidate)
    raise RuntimeError('Inno Setup 6 or later is required. Pass --iscc with the compiler path.')


def build_installer(root=ROOT, compiler=None):
    root = Path(root).resolve()
    output = root / 'build/release'
    archive = output / f'SteamSwitcher-v{__version__}-win-x64.zip'
    with tempfile.TemporaryDirectory(prefix='installer-source-', dir=root / 'build') as temporary:
        stage = Path(temporary).resolve()
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                target = (stage / member.filename).resolve()
                if not target.is_relative_to(stage) or not member.filename.startswith('SteamSwitcher/'):
                    raise ValueError('Unexpected installer source path.')
            bundle.extractall(stage)
        source = stage / 'SteamSwitcher'
        validate_layout(source, __version__)
        subprocess.run([str(compiler or find_compiler()), '/Qp', f'/DAppVersion={__version__}',
                        f'/DAppSource={source}', f'/DReleaseOutput={output}',
                        str(root / 'installer/SteamSDA.iss')], check=True)
    installer = output / f'SteamSwitcher-v{__version__}-Setup-x64.exe'
    write_checksums([archive, installer], output / 'SHA256SUMS.txt')
    return installer


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--iscc')
    args = parser.parse_args()
    print(build_installer(compiler=args.iscc).name)
