from steam_sda.infrastructure.ipc.steamkit import SteamKitGateway
import base64
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock

import pyzipper

from steam_sda.infrastructure.persistence.vault import AccountVault, VAULT
from steam_sda.domain.accounts import claims, json_object, mafile_record, metadata, token_valid
from steam_sda.infrastructure.persistence.archives import export_account, import_account
from steam_sda.domain.errors import SteamError
from steam_sda.application.switching import Switcher, JOURNAL
from steam_sda.application.authentication import AuthenticationService
from steam_sda.infrastructure.steam.token_cache import DesktopToken
from steam_sda.domain.errors import AuthError
from steam_sda.infrastructure.resources import helper_path
from steam_sda.infrastructure.persistence.storage import Store
from steam_sda.infrastructure.steam.vdf import accounts, authenticated_account, read_value, write_value
from test_core import MemoryStore, FakeNative, ONE, TWO, THREE, RAW


def jwt(sid=ONE, audience='client', refresh=False, expires=None):
    data = {'sub': sid, 'aud': ['web', audience] + (['derive'] if refresh else []),
            'exp': int(time.time()) + 90 * 86400 if expires is None else expires}
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
    return 'eyJhbGciOiJub25lIn0.' + payload + '.synthetic-not-a-signature'


def mafile(sid=ONE, login='first_login'):
    return {'account_name': login, 'shared_secret': base64.b64encode(bytes(range(20))).decode(),
            'identity_secret': base64.b64encode(bytes(range(20))).decode(), 'revocation_code': 'synthetic-recovery',
            'Session': {'SteamID': int(sid), 'RefreshToken': jwt(sid, 'mobile', True), 'AccessToken': jwt(sid, 'mobile')}}


def record(sid=ONE, login='first_login'):
    return {'steam_id': sid, 'login': login, 'name': 'Synthetic name', 'mafile': mafile(sid, login),
            'auto_restore': True, 'session': {'refresh_token': jwt(sid, refresh=True), 'access_token': jwt(sid)},
            'password': 'synthetic-private-password'}


class ImportExportTests(unittest.TestCase):
    def test_mafile_owner_must_match_all_ids_and_both_tokens(self):
        row = mafile_record(mafile())
        self.assertEqual(row['steam_id'], ONE)
        broken = mafile()
        broken['Session']['AccessToken'] = jwt(TWO, 'mobile')
        with self.assertRaises(SteamError): mafile_record(broken)
        broken = mafile()
        broken['shared_secret'] = 'not-base64'
        with self.assertRaises(SteamError): mafile_record(broken)
        with self.assertRaises(SteamError):
            mafile_record(mafile(), [{'steam_id': ONE, 'login': 'wrong_login', 'name': 'x'}])

    def test_duplicate_json_keys_and_no_identity_are_rejected(self):
        with self.assertRaises(SteamError): json_object(b'{"shared_secret":"one","shared_secret":"two"}')
        data = mafile()
        data.pop('Session')
        with self.assertRaises(SteamError): mafile_record(data)

    def test_mafile_password_extensions_are_not_imported_or_exported(self):
        data = mafile()
        data['password'] = 'unrelated-private-password'
        data['Session']['Password'] = 'unrelated-private-password'
        data['proxy'] = 'private-proxy'
        imported = mafile_record(data)
        self.assertNotIn('unrelated-private-password', json.dumps(imported))
        self.assertNotIn('private-proxy', json.dumps(imported))

    def test_expired_or_wrong_audience_jwt_is_never_usable(self):
        self.assertTrue(token_valid(jwt(refresh=True), ONE, 'client', True))
        self.assertFalse(token_valid(jwt(TWO, refresh=True), ONE, 'client', True))
        self.assertFalse(token_valid(jwt(audience='mobile', refresh=True), ONE, 'client', True))
        self.assertFalse(token_valid(jwt(expires=1), ONE, 'client'))
        self.assertFalse(token_valid('garbage', ONE, 'client'))

    def test_encrypted_zip_roundtrip_excludes_password_and_disables_automation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'account.zip'
            export_account(path, record(), 'strong-archive-password')
            self.assertNotIn(b'first_login', path.read_bytes())
            self.assertNotIn(b'synthetic-recovery', path.read_bytes())
            imported = import_account(path, 'strong-archive-password')
            self.assertEqual(imported['mafile'], mafile())
            self.assertEqual(imported['session']['refresh_token'], record()['session']['refresh_token'])
            self.assertNotIn('password', imported)
            self.assertFalse(imported['auto_restore'])
            with self.assertRaises(SteamError): import_account(path, 'wrong-password')

    def test_unencrypted_export_only_contains_account_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'account.zip'
            export_account(path, record(), include_secrets=False)
            imported = import_account(path)
            self.assertNotIn('mafile', imported)
            self.assertFalse(imported['session'])
            self.assertNotIn('password', imported)

    def test_unencrypted_export_can_include_mafile_and_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'account.zip'
            original = record()
            export_account(path, original)
            with pyzipper.AESZipFile(path) as archive:
                self.assertFalse(archive.getinfo('account.json').flag_bits & 1)
                self.assertIn(b'synthetic-recovery', archive.read('account.json'))
            restored = import_account(path)
            self.assertEqual(restored['mafile'], original['mafile'])
            self.assertEqual(restored['session'], original['session'])
            self.assertNotIn('password', restored)
            self.assertFalse(restored['auto_restore'])

    def test_archive_accepts_short_unicode_whitespace_and_long_passwords(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'account.zip'
            original = record()
            for password in ('x', 'я', ' ', 'x' * 40000):
                with self.subTest(length=len(password)):
                    export_account(path, original, password)
                    self.assertEqual(import_account(path, password)['session'], original['session'])
                    with self.assertRaises(SteamError): import_account(path)

    def test_hostile_zip_members_and_wrong_native_owner_are_rejected_without_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'hostile.zip'
            with pyzipper.AESZipFile(path, 'w') as archive:
                archive.writestr('../account.json', '{}')
            with self.assertRaises(SteamError): import_account(path)
            self.assertFalse((Path(directory).parent / 'account.json').exists())
            export_account(path, record(), include_secrets=False)
            with self.assertRaises(SteamError):
                import_account(path, known=[{'steam_id': ONE, 'login': 'somebody_else'}])

    def test_import_deduplicates_and_retains_good_session_and_disabled_auto_setting(self):
        store, vault = MemoryStore(), AccountVault(MemoryStore())
        first = record()
        first['auto_restore'] = False
        vault.save(first)
        imported = record()
        imported['session']['refresh_token'] = jwt(expires=int(time.time()) + 2)
        merged = vault.import_record(imported)
        self.assertEqual(len(vault.all()), 1)
        self.assertFalse(merged['auto_restore'])
        self.assertEqual(merged['session'], first['session'])

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_vault_has_no_cleartext_secrets_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = AccountVault(Store(directory))
            vault.save(record())
            raw = (Path(directory) / VAULT).read_bytes()
            for secret in ('synthetic-private-password', 'synthetic-recovery', record()['session']['refresh_token']):
                self.assertNotIn(secret.encode(), raw)
            self.assertEqual(vault.get(ONE)['password'], 'synthetic-private-password')


class SteamKitTests(unittest.TestCase):
    def setUp(self):
        self.vault = AccountVault(MemoryStore())
        self.vault.save(record())

    def test_private_ipc_and_server_owner_check_before_token_persistence(self):
        response = {'ok': True, **metadata(record()), **record()['session'], 'guard_data': 'synthetic-guard'}
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(response), ''))
        auth = AuthenticationService(self.vault, SteamKitGateway(runner))
        result = auth.authenticate(ONE)
        args, kwargs = runner.call_args
        self.assertEqual(len(args[0]), 1)
        self.assertNotIn('synthetic-private-password', str(args[0]))
        request = json.loads(kwargs['input'])
        self.assertEqual(request['password'], 'synthetic-private-password')
        self.assertTrue(kwargs['capture_output'])
        self.assertTrue(result['last_check'])
        before = self.vault.get(ONE)
        response['access_token'] = jwt(TWO)
        runner.return_value.stdout = json.dumps(response)
        with self.assertRaises(SteamError): auth.authenticate(ONE)
        self.assertEqual(self.vault.get(ONE), before)

    def test_bad_password_requires_input_and_never_exposes_helper_errors(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok":false,"error":"steam_5"}', 'private internals'))
        with self.assertRaises(AuthError) as caught: AuthenticationService(self.vault, SteamKitGateway(runner)).authenticate(ONE)
        self.assertTrue(caught.exception.requires_input)
        self.assertNotIn('private', str(caught.exception))
        self.assertEqual(self.vault.get(ONE)['last_check'], 0)

    def test_password_override_is_one_time_and_does_not_replace_saved_password(self):
        response = {'ok': True, **metadata(record()), **record()['session']}
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(response), ''))
        AuthenticationService(self.vault, SteamKitGateway(runner)).authenticate(ONE, password_override='one-time')
        self.assertEqual(json.loads(runner.call_args.kwargs['input'])['password'], 'one-time')
        self.assertEqual(self.vault.get(ONE)['password'], 'synthetic-private-password')

    def test_old_durable_token_requests_new_auth_if_recovery_material_is_available(self):
        row = self.vault.get(ONE)
        row['session']['refresh_token'] = jwt(refresh=True, expires=int(time.time()) + 3600)
        self.vault.save(row)
        response = {'ok': True, **metadata(record()), **record()['session']}
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(response), ''))
        AuthenticationService(self.vault, SteamKitGateway(runner)).authenticate(ONE)
        self.assertTrue(json.loads(runner.call_args.kwargs['input'])['force_new'])

    @unittest.skipUnless(os.name == 'nt', 'Windows helper')
    def test_real_steamkit_binary_selftest_and_no_credentials_path_do_not_connect(self):
        if not helper_path().exists(): self.skipTest('SteamKit helper not built')
        flags = subprocess.CREATE_NO_WINDOW
        check = subprocess.run([str(helper_path()), '--self-test'], capture_output=True, text=True, timeout=15, creationflags=flags)
        self.assertTrue(json.loads(check.stdout)['ok'])
        request = json.dumps({'steam_id': ONE, 'login': 'first_login'})
        check = subprocess.run([str(helper_path())], input=request, capture_output=True, text=True, timeout=15, creationflags=flags)
        self.assertEqual(json.loads(check.stdout)['error'], 'needs_credentials')


class DesktopBridgeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_handoff_encrypts_correct_owner_and_rollback_retains_other_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = DesktopToken(Path(directory) / 'local.vdf')
            raw = b'"MachineUserConfigStore" { "Other" "keep" "Software" { "Valve" { "Steam" { "ConnectCache" { "unrelated" "opaque" } } } } }'
            bridge.path.write_bytes(raw)
            row = record()
            before = bridge.snapshot(row)
            encoded = bridge.encoded(row)
            bridge.apply(row['login'], encoded)
            self.assertNotIn(row['session']['refresh_token'].encode(), bridge.path.read_bytes())
            self.assertEqual(bridge.selected_token(row), row['session']['refresh_token'])
            self.assertIn(b'"unrelated" "opaque"', bridge.path.read_bytes())
            bridge.rollback(row['login'], before, encoded)
            self.assertIsNone(read_value(bridge.path.read_bytes(), bridge.ROOT, bridge.field(row['login'])))
            self.assertIn(b'"unrelated" "opaque"', bridge.path.read_bytes())

    def test_lossless_new_authenticated_account_keeps_native_auth_fields_for_others(self):
        patched = authenticated_account(RAW, metadata(record(THREE, 'third_login')))
        rows = accounts(patched)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r['steam_id'] for r in rows if r['recent']], [THREE])
        self.assertFalse(next(r for r in rows if r['steam_id'] == TWO)['remembered'])
        self.assertTrue(next(r for r in rows if r['steam_id'] == THREE)['remembered'])
        self.assertIn(b'opaque-value', patched)

    def test_unknown_native_cache_format_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = DesktopToken(Path(directory) / 'local.vdf')
            raw = b'"MachineUserConfigStore" {}'
            raw = write_value(raw, bridge.ROOT, bridge.field('first_login'), 'unknown-format')
            bridge.path.write_bytes(raw)
            with self.assertRaises(SteamError): bridge.snapshot(record())
            self.assertEqual(bridge.path.read_bytes(), raw)


class FakeBridge:
    path = Path('C:/synthetic-cache/local.vdf')
    value = '11'
    def snapshot(self, row): return {'value': self.value, 'path': str(self.path)}
    def encoded(self, row): return 'aabb'
    def apply(self, login, value): self.value = value
    def rollback(self, login, before, selected):
        if self.value == selected: self.value = before['value']


class ManagedSwitchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.native, self.store = FakeNative(self.directory.name), MemoryStore()
        self.vault = AccountVault(self.store)
        self.row = record(THREE, 'third_login')
        self.vault.save(self.row)
        self.auth, self.bridge = Mock(), FakeBridge()
        self.auth.authenticate.return_value = self.row
        self.switcher = Switcher(self.native, self.store, timeout=0, auth=self.auth, vault=self.vault, token_bridge=self.bridge)

    def tearDown(self): self.directory.cleanup()

    def test_imported_account_handoff_is_verified_and_does_not_remove_existing_users(self):
        result = self.switcher.switch(THREE)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(self.native.active_id(), THREE)
        self.assertEqual(len(self.native.account_list()), 3)
        self.assertEqual(self.bridge.value, 'aabb')

    def test_auth_failure_restores_original_selection_without_creating_native_account(self):
        self.auth.authenticate.side_effect = SteamError('authentication failed')
        with self.assertRaises(SteamError): self.switcher.switch(THREE)
        self.assertEqual(self.native.active_id(), ONE)
        self.assertEqual(self.native.read_logins(), RAW)
        self.assertFalse(self.switcher.has_recovery())

    def test_pending_imported_account_can_roll_back_its_cache_and_native_metadata(self):
        self.native.auto_login = False
        self.assertEqual(self.switcher.switch(THREE)['state'], 'login')
        self.assertTrue(self.switcher.journal()['desktop'])
        self.switcher.restore()
        self.assertEqual(self.native.read_logins(), RAW)
        self.assertEqual(self.bridge.value, '11')

    def test_launch_failure_rolls_back_native_cache_along_with_metadata(self):
        self.native.fail_launch_once = True
        with self.assertRaises(SteamError): self.switcher.switch(THREE)
        self.assertEqual(self.bridge.value, '11')
        self.assertEqual(self.native.active_id(), ONE)
        self.assertEqual(self.native.read_logins(), RAW)
