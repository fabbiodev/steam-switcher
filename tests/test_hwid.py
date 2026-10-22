from steam_sda.infrastructure.ipc.steamkit import SteamKitGateway
from steam_sda.infrastructure.ipc.device import NativeDeviceGateway
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from steam_sda.infrastructure.persistence.vault import AccountVault
from steam_sda.infrastructure.persistence.archives import export_account, import_account
from steam_sda.domain.accounts import mafile_record, validate_record
from steam_sda.domain.errors import SteamError
from steam_sda.application.devices import DeviceProfiles
from steam_sda.domain.device import fingerprint, machine_id, validate_profile
from steam_sda.application.switching import Switcher
from steam_sda.application.authentication import AuthenticationService
from steam_sda.infrastructure.resources import helper_path
from test_accounts_auth import mafile, jwt, record
from test_core import FakeNative, MemoryStore, ONE, TWO, THREE


def profile(sid=ONE, marker='first'):
    return dict(version=1, source='native_collectors', steam_id=sid, captured_at=1700000000,
                machine_guid=marker + '-guid', network_id=bytes(range(16)).hex(),
                disk_serial=marker + '-disk', pc_name='LAB-PC', fqdn='LAB-PC.lab.invalid')


def capture_response(p):
    value = {k: v for k, v in p.items() if k not in ('steam_id', 'captured_at')}
    return subprocess.CompletedProcess([], 0, json.dumps({'ok': True, 'hwid': value}), '')


class ProfileStorageTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.vault = AccountVault(self.store)
        self.native = Mock(root=Path('C:/SyntheticSteam'))

    def test_first_machine_is_captured_once_and_mafile_cannot_replace_it(self):
        runner = Mock(return_value=capture_response(profile()))
        devices = DeviceProfiles(self.vault, NativeDeviceGateway(self.native, runner))
        imported = self.vault.import_record(mafile_record(mafile()))
        first = devices.ensure(imported)
        again = self.vault.import_record({**record(), 'hwid': profile(marker='another-computer')})
        self.assertEqual(again['hwid'], first)
        self.assertEqual(devices.ensure(again), first)
        runner.assert_called_once()
        self.assertIn('mafile', self.vault.get(ONE))

    def test_native_session_only_account_is_also_bound_without_enabling_automation(self):
        devices = DeviceProfiles(self.vault, NativeDeviceGateway(self.native, Mock(return_value=capture_response(profile()))))
        devices.ensure({'steam_id': ONE, 'login': 'first_login', 'name': 'Native session'})
        row = self.vault.get(ONE)
        self.assertEqual(row['hwid']['steam_id'], ONE)
        self.assertFalse(row['auto_restore'])
        self.assertNotIn('mafile', row)

    def test_first_discovery_scan_captures_one_snapshot_for_all_accounts(self):
        runner = Mock(return_value=capture_response(profile()))
        devices = DeviceProfiles(self.vault, NativeDeviceGateway(self.native, runner))
        rows = [record(), record(TWO, 'second_login')]
        devices.bind_many(rows)
        devices.bind_many(rows)
        runner.assert_called_once()
        self.assertEqual(self.vault.get(ONE)['hwid']['steam_id'], ONE)
        self.assertEqual(self.vault.get(TWO)['hwid']['steam_id'], TWO)

    def test_profiles_are_isolated_by_steam_id_and_survive_stale_session_updates(self):
        runner = Mock(side_effect=[capture_response(profile()), capture_response(profile(TWO, 'second'))])
        devices = DeviceProfiles(self.vault, NativeDeviceGateway(self.native, runner))
        first, second = record(), record(TWO, 'second_login')
        self.vault.save(first)
        self.vault.save(second)
        a, b = devices.ensure(first), devices.ensure(second)
        self.vault.save(first)  # Auth worker read before the profile was saved.
        self.assertEqual(self.vault.get(ONE)['hwid'], a)
        self.assertEqual(self.vault.get(TWO)['hwid'], b)
        self.assertNotEqual(fingerprint(a), fingerprint(b))
        with self.assertRaises(SteamError):
            self.vault.save({**first, 'hwid': profile(marker='replacement')})

    def test_export_preserves_profile_and_old_archives_bind_on_destination(self):
        row = {**record(), 'hwid': profile()}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'account.zip'
            export_account(path, row, 'x')
            restored = import_account(path, 'x')
            self.assertEqual(restored['hwid'], row['hwid'])
            other = AccountVault(MemoryStore())
            other.import_record(restored)
            runner = Mock()
            self.assertEqual(DeviceProfiles(other, NativeDeviceGateway(self.native, runner)).ensure(restored), row['hwid'])
            runner.assert_not_called()
            export_account(path, record())
            self.assertNotIn('hwid', import_account(path))

    def test_malformed_cross_owner_truncated_network_and_control_characters_fail(self):
        for patch in ({'steam_id': TWO}, {'network_id': '00' * 8}, {'pc_name': 'bad\0name'},
                      {'version': 2}, {'version': True}, {'pc_name': '\ud800'},
                      {'captured_at': float('nan')}, {'disk_serial': 'я' * 256}):
            with self.subTest(patch=list(patch)):
                with self.assertRaises(SteamError):
                    validate_record({**record(), 'hwid': {**profile(), **patch}})

    def test_failed_capture_does_not_save_partial_profile_or_leak_helper_output(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({'ok': False, 'error': 'device_capture', 'details': 'private-id'}), ''))
        with self.assertRaises(SteamError) as caught:
            DeviceProfiles(self.vault, NativeDeviceGateway(self.native, runner)).ensure(record())
        self.assertNotIn('private-id', str(caught.exception))
        self.assertFalse(self.vault.get(ONE))

    def test_launch_requires_exact_profile_confirmation_and_uses_private_pipe(self):
        row = {**record(), 'hwid': profile()}
        self.vault.save(row)
        response = {'ok': True, 'hooks_ready': True, 'before_main': True, 'machine_id_sha256': fingerprint(row['hwid']), 'steam_pid': 123}
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(response), ''))
        devices = DeviceProfiles(self.vault, NativeDeviceGateway(self.native, runner))
        devices.launch(row)
        args, kwargs = runner.call_args
        self.assertNotIn(row['hwid']['machine_guid'], str(args))
        request = json.loads(kwargs['input'])
        self.assertEqual(request['hwid'], row['hwid'])
        self.assertNotIn('session', request)
        response['machine_id_sha256'] = '0' * 64
        runner.return_value.stdout = json.dumps(response)
        with self.assertRaises(SteamError): devices.launch(row)

    def test_steam_self_restart_is_not_treated_as_a_verified_hwid_launch(self):
        import time
        row = {**record(), 'hwid': profile()}
        self.vault.save(row)
        response = {'ok': True, 'hooks_ready': True, 'before_main': True,
                    'machine_id_sha256': fingerprint(row['hwid']), 'steam_pid': 123}
        devices = DeviceProfiles(self.vault, NativeDeviceGateway(self.native, Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(response), ''))))
        devices.launch(row)
        self.native.processes.return_value = [Mock(pid=123, create_time=Mock(return_value=time.time() - 1))]
        self.assertTrue(devices.is_applied(ONE))
        self.native.processes.return_value = [Mock(pid=456, create_time=Mock(return_value=time.time()))]
        self.assertFalse(devices.is_applied(ONE))
        self.assertFalse(devices.is_applied(TWO))

    def test_steamkit_refresh_and_mafile_credentials_use_same_saved_profile(self):
        for material in ('session', 'mafile'):
            with self.subTest(material=material):
                row = {**record(), 'hwid': profile()}
                if material == 'mafile': row['session'] = {}
                self.vault.save(row)
                response = {'ok': True, 'steam_id': ONE, 'login': row['login'],
                            'refresh_token': jwt(refresh=True), 'access_token': jwt()}
                runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(response), ''))
                AuthenticationService(self.vault, SteamKitGateway(runner)).authenticate(ONE)
                self.assertEqual(json.loads(runner.call_args.kwargs['input'])['hwid'], row['hwid'])
                self.assertEqual(self.vault.get(ONE)['hwid'], row['hwid'])


class ProfileSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.native, self.store = FakeNative(self.temp.name), MemoryStore()
        self.vault, self.devices = AccountVault(self.store), Mock()
        for row in self.native.account_list():
            self.vault.save({**row, 'hwid': profile(row['steam_id'])})
        self.switcher = Switcher(self.native, self.store, timeout=0, vault=self.vault, devices=self.devices)

    def tearDown(self): self.temp.cleanup()

    def test_native_session_switch_uses_target_profile(self):
        def launch(row): self.native.launch()
        self.devices.launch.side_effect = launch
        self.assertEqual(self.switcher.switch(TWO)['state'], 'ready')
        self.assertEqual(self.devices.launch.call_args.args[0]['hwid']['steam_id'], TWO)

    def test_unknown_version_fails_before_stopping_steam(self):
        self.devices.preflight.side_effect = SteamError('unsupported')
        before = self.native.read_logins()
        with self.assertRaises(SteamError): self.switcher.switch(TWO)
        self.assertEqual(self.native.stops, 0)
        self.assertEqual(self.native.read_logins(), before)
        self.devices.launch.assert_not_called()

    def test_current_account_can_be_restarted_when_profile_was_not_applied(self):
        self.devices.is_applied.return_value = False
        def launch(row):
            self.native.launch()
            self.devices.is_applied.return_value = True
        self.devices.launch.side_effect = launch
        self.assertEqual(self.switcher.switch(ONE)['state'], 'ready')
        self.assertEqual(self.native.stops, 1)
        self.assertEqual(self.devices.launch.call_args.args[0]['steam_id'], ONE)

    def test_restarted_client_does_not_clear_recovery_as_a_success(self):
        self.devices.is_applied.return_value = False
        self.devices.launch.side_effect = lambda row: self.native.launch()
        self.assertEqual(self.switcher.switch(TWO)['state'], 'hwid_lost')
        self.assertFalse(self.switcher.complete_if_active())
        self.assertTrue(self.switcher.has_recovery())

    def test_failed_target_launch_restores_previous_accounts_own_profile(self):
        calls = []
        def launch(row):
            calls.append(row['steam_id'])
            if row['steam_id'] == TWO: raise SteamError('fixture failure')
            self.native.launch()
        self.devices.launch.side_effect = launch
        with self.assertRaises(SteamError): self.switcher.switch(TWO)
        self.assertEqual(calls, [TWO, ONE])
        self.assertEqual(self.native.active, ONE)
        self.assertFalse(self.switcher.has_recovery())

    def test_manual_recovery_restores_previous_profile(self):
        self.native.auto_login = False
        self.devices.launch.side_effect = lambda row: self.native.launch()
        self.assertEqual(self.switcher.switch(TWO)['state'], 'login')
        self.switcher.restore()
        self.assertEqual([c.args[0]['steam_id'] for c in self.devices.launch.call_args_list], [TWO, ONE])


class NativeProfileTests(unittest.TestCase):
    def test_binary_kv_layout_contains_all_components_and_full_network_hash(self):
        import hashlib
        p = profile()
        data = machine_id(p)
        self.assertEqual(len(data), 155)
        self.assertEqual(data[:15], b'\0MessageObject\0')
        self.assertIn(hashlib.sha1(bytes(range(16))).hexdigest().encode(), data)
        self.assertNotIn(hashlib.sha1(bytes(range(8))).hexdigest().encode(), data)

    def test_compiled_hooks_suspended_process_and_steamkit_serializer_without_login(self):
        helper = helper_path()
        if not helper.exists() or not (helper.parent / 'SteamSDADevice.dll').exists():
            self.skipTest('Native device helper not built')
        for patch in ({}, {'fqdn': ''}, {'pc_name': 'Лаборатория', 'disk_serial': 'я' * 250}):
            p = {**profile(), **patch}
            result = subprocess.run([str(helper), '--device-self-test'], input=json.dumps({'steam_path': 'C:/SyntheticSteam', 'hwid': p}),
                                    text=True, capture_output=True, timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            response = json.loads(result.stdout)
            self.assertTrue(response.get('ok'), response.get('error'))
            self.assertEqual(response['machine_id_sha256'], fingerprint(p))
            if (helper.parent / 'SteamSDA.DeviceFixture.exe').exists():
                self.assertTrue(response['suspended_process_test'])

    def test_changed_dll_is_rejected_before_native_execution(self):
        helper = helper_path()
        if not helper.exists(): self.skipTest('Helper not built')
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'steamclient64.dll').write_bytes(b'not-a-supported-steam-module')
            response = subprocess.run([str(helper), '--check-device'], input=json.dumps({'steam_path': directory}),
                                      text=True, capture_output=True, timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(json.loads(response.stdout)['error'], 'device_version')
