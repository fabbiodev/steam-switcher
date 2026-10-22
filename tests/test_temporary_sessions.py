from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from steam_sda.application.accounts import AccountService
from steam_sda.application.switching import Switcher
from steam_sda.domain.accounts import metadata, session_status
from steam_sda.domain.errors import SteamError
from steam_sda.infrastructure.persistence.archives import AccountArchives, export_accounts, import_accounts
from steam_sda.infrastructure.persistence.vault import AccountVault
from steam_sda.infrastructure.steam.client import NativeSteam
from test_accounts_auth import record, jwt, FakeBridge
from test_core import ONE, TWO, MemoryStore, FakeNative, RAW


class TemporarySessionTests(unittest.TestCase):
    def service(self, token=''):
        native, bridge = Mock(), Mock()
        native.account_list.return_value = [{**metadata(record()), 'remembered': False}]
        native.active_id.return_value = ONE
        bridge.selected_token.return_value = token
        vault = AccountVault(MemoryStore())
        return AccountService(vault, AccountArchives(), native, bridge), native, bridge

    def test_discovery_saves_available_temporary_session_and_retains_it_after_client_cache_disappears(self):
        token = jwt(refresh=True)
        service, native, bridge = self.service(token)
        rows, _ = service.catalog()
        self.assertIn('Разовый вход', rows[0]['session_label'])
        service.bind_discovered(rows)
        saved = service.vault.get(ONE)
        self.assertEqual(saved['session']['refresh_token'], token)
        self.assertEqual(saved['session_origin'], 'steam_temporary')
        self.assertFalse(saved['auto_restore'])
        bridge.selected_token.return_value = ''
        native.account_list.return_value = []
        native.active_id.return_value = None
        rows, _ = service.catalog()
        self.assertEqual(rows[0]['steam_id'], ONE)
        self.assertIn('сессия в базе', rows[0]['session_label'])
        self.assertTrue(rows[0]['temporary_session'])
        native.stop.assert_not_called()

    def test_missing_or_wrong_owner_token_is_visible_but_never_claimed_portable(self):
        for token in ('', jwt(TWO, refresh=True)):
            service, native, _ = self.service(token)
            rows, _ = service.catalog()
            service.bind_discovered(rows)
            self.assertFalse(service.vault.all())
            self.assertIn('не сохранён', rows[0]['session_label'])
            with self.assertRaises(SteamError) as caught:
                service.save_session(rows[0])
            self.assertIn('переносимый токен', str(caught.exception))
            self.assertNotIn(token if token else 'private-token', str(caught.exception))
            native.stop.assert_not_called()

    def test_temporary_session_roundtrip_keeps_origin_and_owner_in_whole_database_archive(self):
        service, _, _ = self.service(jwt(refresh=True))
        service.save_session(metadata(record()))
        row = service.vault.get(ONE)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'accounts.zip'
            export_accounts(path, [row], 'password')
            imported = import_accounts(path, 'password')
            other = AccountVault(MemoryStore())
            other.import_records(imported)
            restored = other.get(ONE)
            self.assertEqual(restored['session'], row['session'])
            self.assertEqual(restored['session_origin'], 'steam_temporary')
            self.assertIn('сессия в базе', session_status(restored, restored)[0])

    def test_active_owner_missing_from_loginusers_is_recovered_only_from_matching_install_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'steam.exe').write_bytes(b'fixture')
            (root / 'config').mkdir()
            (root / 'config/loginusers.vdf').write_bytes(b'"users" {}')
            config = root / 'config/config.vdf'
            config.write_text('"InstallConfigStore" { "Software" { "Valve" { "Steam" { "Accounts" { '
                              '"first_login" { "SteamID" "' + ONE + '" } } } } } }')
            native = NativeSteam(root)
            native.active_id = Mock(return_value=ONE)
            rows = native.account_list()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['steam_id'], ONE)
            self.assertEqual(rows[0]['login'], 'first_login')
            self.assertFalse(rows[0]['remembered'])
            native.active_id.return_value = TWO
            self.assertEqual(native.account_list(), [])
            native.active_id.return_value = None
            self.assertEqual(native.account_list(), [])

    def test_selected_token_is_saved_before_stop_erases_the_native_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            native, vault = FakeNative(directory), AccountVault(MemoryStore())
            row = {**metadata(record(TWO, 'second_login')), 'auto_restore': False}
            vault.save(row)
            token = jwt(TWO, refresh=True)
            bridge = FakeBridge()
            bridge.selected_token = Mock(side_effect=lambda account: token if native.active_id() is not None else '')
            auth = Mock()
            auth.authenticate.side_effect = lambda sid: vault.get(sid)
            switcher = Switcher(native, MemoryStore(), timeout=0, auth=auth, vault=vault, token_bridge=bridge)
            self.assertEqual(switcher.switch(TWO)['state'], 'ready')
            self.assertEqual(vault.get(TWO)['session']['refresh_token'], token)
            self.assertEqual(vault.get(TWO)['session_origin'], 'steam_temporary')
            auth.authenticate.assert_called_once_with(TWO)

    def test_current_temporary_owner_is_saved_when_switching_to_another_account(self):
        with tempfile.TemporaryDirectory() as directory:
            native, vault = FakeNative(directory), AccountVault(MemoryStore())
            native.login_file.write_bytes(RAW.replace(b'"RememberPassword" "1"', b'"RememberPassword" "0"').replace(b'"AllowAutoLogin" "1"', b'"AllowAutoLogin" "0"'))
            vault.save(record(TWO, 'second_login'))
            token = jwt(refresh=True)
            bridge = FakeBridge()
            bridge.selected_token = Mock(side_effect=lambda account: token if account['steam_id'] == ONE else '')
            auth = Mock()
            auth.authenticate.side_effect = lambda sid: vault.get(sid)
            switcher = Switcher(native, MemoryStore(), timeout=0, auth=auth, vault=vault, token_bridge=bridge)
            self.assertEqual(switcher.switch(TWO)['state'], 'ready')
            self.assertEqual(vault.get(ONE)['session']['refresh_token'], token)
            self.assertEqual(vault.get(ONE)['session_origin'], 'steam_temporary')
