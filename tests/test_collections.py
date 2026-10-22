import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import zipfile

from steam_sda.application.accounts import AccountService
from steam_sda.application.devices import DeviceProfiles
from steam_sda.domain.accounts import validate_records
from steam_sda.domain.errors import SteamError
from steam_sda.infrastructure.persistence.archives import AccountArchives, export_account, export_accounts, import_accounts
from steam_sda.infrastructure.persistence.vault import AccountVault
from test_accounts_auth import record
from test_core import MemoryStore, ONE, TWO, THREE
from test_hwid import profile


class CollectionTests(unittest.TestCase):
    def rows(self):
        return [{**record(), 'hwid': profile()},
                {**record(TWO, 'second_login'), 'hwid': profile(TWO, 'second')}]

    def test_encrypted_whole_database_roundtrip_keeps_sessions_and_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'steam-accounts.zip'
            original = self.rows()
            export_accounts(path, original, 'collection-password')
            restored = import_accounts(path, 'collection-password')
            self.assertNotIn(b'first_login', path.read_bytes())
            self.assertEqual([r['steam_id'] for r in restored], [ONE, TWO])
            for before, after in zip(original, restored):
                self.assertEqual(after['session'], before['session'])
                self.assertEqual(after['hwid'], before['hwid'])
                self.assertEqual(after['mafile'], before['mafile'])
                self.assertNotIn('password', after)
                self.assertFalse(after['auto_restore'])
            with self.assertRaises(SteamError):
                import_accounts(path, 'wrong')
            self.assertIn('password', original[0])

    def test_metadata_collection_and_legacy_single_account_are_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'accounts.zip'
            export_accounts(path, self.rows(), include_secrets=False)
            for row in import_accounts(path):
                self.assertFalse(row['session'])
                self.assertNotIn('mafile', row)
                self.assertNotIn('password', row)
            export_account(path, self.rows()[0])
            self.assertEqual(import_accounts(path)[0]['hwid'], profile())

    def test_whole_database_is_not_limited_to_old_single_account_size(self):
        rows = []
        for index in range(120):
            row = record(str(int(ONE) + index), f'login_{index}')
            row.pop('mafile', None)
            row['session']['guard_data'] = 'x' * 20000
            rows.append(row)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'accounts.zip'
            export_accounts(path, rows)
            with zipfile.ZipFile(path) as archive:
                self.assertGreater(archive.getinfo('accounts.json').file_size, 2 * 1024 * 1024)
            self.assertEqual(len(import_accounts(path)), 120)

    def test_duplicate_ids_logins_and_hostile_member_names_are_rejected(self):
        for rows in ([record(), record()], [record(), record(TWO, 'FIRST_LOGIN')]):
            with self.assertRaises(SteamError):
                validate_records(rows)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'accounts.zip'
            for name in ('../accounts.json', 'accounts.json/extra'):
                with zipfile.ZipFile(path, 'w') as archive:
                    archive.writestr(name, '{}')
                with self.assertRaises(SteamError):
                    import_accounts(path)

    def test_batch_import_is_one_write_and_invalid_late_owner_leaves_vault_unchanged(self):
        store = MemoryStore()
        vault = AccountVault(store)
        vault.save(record())
        before = copy.deepcopy(store.values)
        store.write = Mock(wraps=store.write)
        with self.assertRaises(SteamError):
            vault.import_records([record(THREE, 'third_login'), record(ONE, 'different_login')])
        self.assertEqual(store.values, before)
        store.write.assert_not_called()
        vault.import_records([record(TWO, 'second_login'), record(THREE, 'third_login')])
        store.write.assert_called_once()
        self.assertEqual(set(vault.all()), {ONE, TWO, THREE})

    def test_batch_preserves_first_bound_profile_and_unrelated_deleted_marker(self):
        vault = AccountVault(MemoryStore())
        vault.save(self.rows()[0])
        vault.delete(THREE)
        incoming = [{**record(), 'hwid': profile(marker='other-pc')}, self.rows()[1]]
        imported = vault.import_records(incoming)
        self.assertEqual(imported[0]['hwid'], profile())
        self.assertIn(THREE, vault.excluded())

    def test_prepare_import_uses_one_snapshot_and_has_no_partial_vault_writes(self):
        vault = AccountVault(MemoryStore())
        gateway = Mock()
        gateway.call.return_value = {'hwid': profile()}
        devices = DeviceProfiles(vault, gateway)
        prepared = devices.prepare_import([record(), record(TWO, 'second_login')])
        self.assertEqual([r['hwid']['steam_id'] for r in prepared], [ONE, TWO])
        gateway.call.assert_called_once_with('--capture-device')
        self.assertFalse(vault.all())

    def test_capture_failure_during_collection_import_does_not_add_any_account(self):
        vault = AccountVault(MemoryStore())
        archives, devices = Mock(), Mock()
        archives.read_all.return_value = self.rows()
        devices.prepare_import.side_effect = SteamError('capture failed')
        service = AccountService(vault, archives, None, Mock(), devices)
        with self.assertRaises(SteamError):
            service.import_all_file('accounts.zip')
        self.assertFalse(vault.all())

    def test_export_includes_native_and_managed_accounts_and_excludes_deleted_records(self):
        vault = AccountVault(MemoryStore())
        vault.save(record(THREE, 'third_login'))
        vault.delete(TWO)
        native, tokens = Mock(), Mock()
        native.account_list.return_value = [record(), record(TWO, 'second_login')]
        tokens.selected_token.return_value = ''
        service = AccountService(vault, AccountArchives(), native, tokens)
        rows = service.prepare_all_export()
        self.assertEqual({r['steam_id'] for r in rows}, {ONE, THREE})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'all.zip'
            service.export_all_file(path, rows)
            other = AccountService(AccountVault(MemoryStore()), AccountArchives(), None, Mock())
            restored = other.import_all_file(path)
            self.assertEqual({r['steam_id'] for r in restored}, {ONE, THREE})
