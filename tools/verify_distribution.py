"""Unpack and run the release outside the checkout with synthetic accounts only."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from steam_sda import __version__
from tools.distribution import validate_layout


def verify(archive, report_path):
    with tempfile.TemporaryDirectory(prefix='SteamSDA-release-') as directory:
        directory = Path(directory)
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                destination = (directory / name).resolve()
                if not destination.is_relative_to(directory.resolve()):
                    raise ValueError('Archive contains a path outside its destination.')
            bundle.extractall(directory)
        application = directory / 'SteamSwitcher'
        validate_layout(application, __version__)
        working = directory / 'unrelated-working-directory'
        working.mkdir()
        report = working / 'ui-check.json'
        env = {key: value for key, value in os.environ.items() if key not in ('PYTHONPATH', 'PYTHONHOME')}
        result = subprocess.run([str(application / 'SteamSDA.exe'), '--check-ui', str(report)],
                                cwd=working, env=env, capture_output=True, timeout=120,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode != 0 or not report.is_file():
            raise RuntimeError(f'Relocated application check failed (exit {result.returncode}).')
        summary = json.loads(report.read_text(encoding='utf-8'))
        if not all(summary.get(key) is True for key in
                   ('ok', 'external_application_code', 'organized_distribution', 'local_account_deletion')):
            raise RuntimeError('Relocated application did not confirm its layout and account workflow.')
        Path(report_path).write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print('Relocated ZIP: external app code, runtime, UI, account deletion and SteamKit passed.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', required=True)
    parser.add_argument('--report', default=str(ROOT / 'build/relocated-ui-check.json'))
    args = parser.parse_args()
    verify(Path(args.archive).resolve(), Path(args.report).resolve())
