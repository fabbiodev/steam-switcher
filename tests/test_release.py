import hashlib
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
        (self.dist / '_internal').mkdir()
        (self.dist / '_internal/runtime.dll').write_bytes(b'synthetic-runtime')
        (self.dist / '_internal/base_library.zip').write_bytes(b'synthetic-python-standard-library')
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
            self.assertIn('SteamSwitcher/_internal/runtime.dll', bundle.namelist())
            self.assertIn('SteamSwitcher/_internal/base_library.zip', bundle.namelist())
            self.assertIn('SteamSwitcher/LICENSE', bundle.namelist())
            self.assertIn('SteamSwitcher/docs/architecture.md', bundle.namelist())
        self.assertEqual(checksum.read_text().split()[0], hashlib.sha256(archive.read_bytes()).hexdigest())

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
