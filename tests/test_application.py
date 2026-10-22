"""Application workflows tested without a QApplication or a real Steam client."""
import copy
import unittest
from unittest.mock import Mock
from steam_sda.application.accounts import AccountService
from steam_sda.application.authentication import AuthenticationService
from steam_sda.application.sessions import SessionRecovery, next_due_auth
from steam_sda.application.preferences import PreferenceService
from steam_sda.domain.errors import SteamError
from steam_sda.infrastructure.persistence.vault import AccountVault
from test_accounts_auth import record, jwt
from test_core import MemoryStore, ONE, TWO
from test_hwid import profile


class AccountWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.vault = AccountVault(MemoryStore())
        self.archives, self.native, self.tokens = Mock(), Mock(), Mock()
        self.native.account_list.return_value = []
        self.service = AccountService(self.vault, self.archives, self.native, self.tokens)

    def test_session_import_keeps_original_profile_and_does_not_enable_automation(self):
        original = {**record(), 'hwid': profile(), 'auto_restore': False}
        self.vault.save(original)
        self.archives.read.return_value = {**record(), 'hwid': profile(marker='other-computer')}
        imported = self.service.import_file('account.zip', 'x')
        self.assertEqual(imported['hwid'], original['hwid'])
        self.assertFalse(imported['auto_restore'])
        self.archives.read.assert_called_once_with('account.zip', 'x', ())

    def test_mafile_import_captures_only_the_selected_native_owner(self):
        self.archives.read.return_value = record()
        self.archives.read.return_value['session'] = {}
        self.native.account_list.return_value = [record(), record(TWO, 'second_login')]
        self.tokens.selected_token.return_value = jwt(refresh=True)
        result = self.service.import_file('first.maFile')
        self.assertTrue(result['auto_restore'])
        self.tokens.selected_token.assert_called_once()
        self.assertEqual(self.tokens.selected_token.call_args.args[0]['steam_id'], ONE)

    def test_one_time_password_is_not_persisted_and_profile_survives_configuration(self):
        original = {**record(), 'hwid': profile()}
        self.vault.save(original)
        result = self.service.configure_login(original, True, 'one-time', remember=False, clear_password=True)
        self.assertNotIn('password', result)
        self.assertNotIn('password', self.vault.get(ONE))
        self.assertEqual(self.vault.get(ONE)['hwid'], original['hwid'])

    def test_auth_service_checks_owner_before_saving_gateway_result(self):
        original = record()
        self.vault.save(original)
        gateway = Mock()
        gateway.authenticate.return_value = {'ok': True, **record(TWO, 'second_login'), **record(TWO, 'second_login')['session']}
        with self.assertRaises(SteamError): AuthenticationService(self.vault, gateway).authenticate(ONE)
        self.assertEqual(self.vault.get(ONE)['session'], original['session'])

    def test_catalog_can_be_displayed_before_an_unsupported_device_binding_fails(self):
        self.vault.save(record())
        self.service.devices = Mock()
        self.service.devices.bind_many.side_effect = SteamError('unsupported')
        rows, managed = self.service.catalog()
        self.assertEqual(rows[0]['steam_id'], ONE)
        self.assertIn(ONE, managed)
        with self.assertRaises(SteamError): self.service.bind_discovered(rows)

    def test_existing_encrypted_vault_keeps_session_and_hwid_after_reorganization(self):
        import tempfile
        from steam_sda.infrastructure.persistence.storage import Store
        original = {**record(), 'hwid': profile()}
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            # The 0.3 format: a SteamID-indexed dict in accounts-vault.dpapi.
            store.write('accounts-vault.dpapi', {ONE: original}, encrypted=True)
            restored = AccountVault(store).get(ONE)
            self.assertEqual(restored['session'], original['session'])
            self.assertEqual(restored['hwid'], original['hwid'])
            self.assertEqual(restored['mafile'], original['mafile'])

    def test_delete_removes_credentials_and_does_not_rediscover_native_account(self):
        self.vault.save({**record(), 'hwid': profile(), 'password': 'synthetic-secret'})
        self.vault.save(record(TWO, 'second_login'))
        self.native.account_list.return_value = [record(), record(TWO, 'second_login')]
        self.service.delete(ONE)
        self.assertEqual(self.vault.get(ONE), {})
        self.assertNotIn('synthetic-secret', str(self.vault.store.values))
        self.assertEqual([row['steam_id'] for row in self.service.catalog()[0]], [TWO])
        self.assertEqual(AccountVault(self.vault.store).excluded(), {ONE})
        self.assertIn(TWO, self.vault.all())
        self.native.stop.assert_not_called()

    def test_deleted_account_is_restored_only_by_explicit_import(self):
        self.service.delete(ONE)
        self.service.devices = Mock()
        self.service.bind_discovered([record()])
        self.service.devices.bind_many.assert_called_once_with([])
        self.service.devices = None
        self.archives.read.return_value = record()
        self.service.import_file('account.zip')
        self.assertIn(ONE, self.vault.all())
        self.assertNotIn(ONE, self.vault.excluded())

    def test_deletion_validates_id_and_preserves_record_when_write_fails(self):
        from unittest.mock import patch
        self.vault.save(record())
        with self.assertRaises(SteamError): self.service.delete('not-a-steam-id')
        with patch.object(self.vault.store, 'write', side_effect=SteamError('synthetic write failure')):
            with self.assertRaises(SteamError): self.service.delete(ONE)
        self.assertIn(ONE, self.vault.all())
        self.assertNotIn(ONE, self.vault.excluded())


class SessionPolicyTests(unittest.TestCase):
    def test_recovery_follows_lost_owner_and_waits_fifteen_seconds(self):
        from unittest.mock import patch
        recovery = SessionRecovery()
        managed = {ONE: record()}
        with patch('steam_sda.application.sessions.time.monotonic', return_value=100):
            self.assertIsNone(recovery.observe(ONE, None, managed, running=True, pending_switch=False, games_running=False))
        with patch('steam_sda.application.sessions.time.monotonic', return_value=114):
            self.assertIsNone(recovery.observe(None, None, managed, running=True, pending_switch=False, games_running=False))
        with patch('steam_sda.application.sessions.time.monotonic', return_value=116):
            self.assertEqual(recovery.observe(None, None, managed, running=True, pending_switch=False, games_running=False), ONE)

    def test_closed_client_and_active_game_never_trigger_recovery(self):
        from unittest.mock import patch
        for running, games in ((False, False), (True, True)):
            recovery = SessionRecovery()
            with patch('steam_sda.application.sessions.time.monotonic', return_value=100):
                recovery.observe(ONE, None, {ONE: record()}, running=running, pending_switch=False, games_running=games)
            with patch('steam_sda.application.sessions.time.monotonic', return_value=116):
                self.assertIsNone(recovery.observe(None, None, {ONE: record()}, running=running, pending_switch=False, games_running=games))

    def test_due_renewal_obeys_backoff_and_disabled_accounts(self):
        managed = {ONE: {**record(), 'next_check': 101}, TWO: {**record(TWO, 'second_login'), 'auto_restore': False}}
        self.assertIsNone(next_due_auth(managed, 0, now=100))
        self.assertIsNone(next_due_auth(managed, 90, now=102))
        self.assertEqual(next_due_auth(managed, 0, now=102), ONE)

    def test_settings_load_preserves_existing_store_names_and_protected_api_key(self):
        store = MemoryStore()
        store.write('settings.json', {'steam_path': 'C:/SyntheticSteam'})
        store.write('api-key.dpapi', {'key': 'a' * 32}, encrypted=True)
        state = PreferenceService(store).load()
        self.assertEqual(state.settings['steam_path'], 'C:/SyntheticSteam')
        self.assertEqual(state.api_key, 'a' * 32)
