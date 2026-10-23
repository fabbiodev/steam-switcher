"""Compile the real installer script early, using public, inert fixture files."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from steam_sda import __version__
from tools.build_installer import find_compiler


def check():
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='installer-script-check-', dir=build) as directory:
        workspace = Path(directory)
        source, output = workspace / 'source', workspace / 'output'
        (source / 'assets').mkdir(parents=True)
        shutil.copyfile(ROOT / 'assets/app.ico', source / 'assets/app.ico')
        shutil.copyfile(ROOT / 'LICENSE', source / 'LICENSE')
        (source / 'SteamSDA.exe').write_bytes(b'Compile-only fixture; never execute.')
        subprocess.run([find_compiler(), '/Qp', f'/DAppVersion={__version__}',
                        f'/DAppSource={source}', f'/DReleaseOutput={output}',
                        str(ROOT / 'installer/SteamSDA.iss')], check=True)
        expected = output / f'SteamSwitcher-v{__version__}-Setup-x64.exe'
        if not expected.is_file():
            raise RuntimeError('Installer script check did not produce its fixture.')
    print('Installer script compiled successfully; no installer was executed.')


if __name__ == '__main__':
    check()
