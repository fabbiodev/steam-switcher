import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from steam_sda import __version__
from tools.package_release import package


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dist = self.root / 'dist/SteamSDA'
        self.dist.mkdir(parents=True)
        (self.dist / 'SteamSDA.exe').write_bytes(b'synthetic-executable')
        files = ['runtime/runtime.dll', 'runtime/base_library.zip', 'assets/style.qss',
                 'assets/fonts/Inter-V.ttf', 'integrations/steamkit/SteamSDA.Auth.exe',
                 'integrations/device/SteamSDADevice.dll', 'app/steam_sda/__init__.pyc']
        files += [f'app/steam_sda/{layer}/__init__.pyc' for layer in
                  ('domain', 'application', 'infrastructure', 'presentation')]
        for name in files:
            path = self.dist / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic-runtime')
        manifest = {'version': __version__, 'entrypoint': 'SteamSDA.exe', 'app': 'app/steam_sda',
                    'runtime': 'runtime', 'assets': 'assets', 'steamkit': 'integrations/steamkit/SteamSDA.Auth.exe',
                    'device': 'integrations/device/SteamSDADevice.dll'}
        (self.dist / 'layout.json').write_text(json.dumps(manifest))
        for name in ('README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md'):
            (self.root / name).write_text('Synthetic public documentation', encoding='utf-8')
        (self.root / 'docs').mkdir()
        (self.root / 'docs/architecture.md').write_text('Synthetic architecture documentation', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_archive_contains_runtime_and_docs_and_checksum_matches(self):
        archive, checksum = package(self.root, f'v{__version__}')
        with zipfile.ZipFile(archive) as bundle:
            self.assertIn('SteamSwitcher/SteamSDA.exe', bundle.namelist())
            self.assertIn('SteamSwitcher/runtime/runtime.dll', bundle.namelist())
            self.assertIn('SteamSwitcher/runtime/base_library.zip', bundle.namelist())
            self.assertIn('SteamSwitcher/app/steam_sda/application/__init__.pyc', bundle.namelist())
            self.assertIn('SteamSwitcher/integrations/device/SteamSDADevice.dll', bundle.namelist())
            self.assertFalse(any('_internal' in name for name in bundle.namelist()))
            self.assertEqual(len(bundle.namelist()), len(set(bundle.namelist())))
            self.assertIn('SteamSwitcher/LICENSE', bundle.namelist())
            self.assertIn('SteamSwitcher/docs/architecture.md', bundle.namelist())
        self.assertEqual(checksum.read_text().split()[0], hashlib.sha256(archive.read_bytes()).hexdigest())

    def test_incomplete_or_legacy_layout_cannot_be_released(self):
        helper = self.dist / 'integrations/steamkit/SteamSDA.Auth.exe'
        helper.unlink()
        with self.assertRaises(ValueError): package(self.root, f'v{__version__}')
        helper.write_bytes(b'synthetic')
        (self.dist / '_internal').mkdir()
        with self.assertRaises(ValueError): package(self.root, f'v{__version__}')

    def test_private_account_files_are_never_packaged(self):
        for name in ('account.maFile', 'accounts-vault.dpapi', 'loginusers.vdf', 'accounts.json', '.env', 'account.zip'):
            file = self.dist / name
            file.write_bytes(b'synthetic-private-data')
            with self.assertRaises(ValueError):
                package(self.root, f'v{__version__}')
            file.unlink()

    def test_version_mismatch_and_missing_build_fail_before_release(self):
        with self.assertRaises(ValueError): package(self.root, 'v99.99.99')
        (self.dist / 'SteamSDA.exe').unlink()
        with self.assertRaises(ValueError): package(self.root, f'v{__version__}')
