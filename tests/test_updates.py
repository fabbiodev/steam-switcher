import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import requests

from steam_sda.application.updates import UpdateService
from steam_sda.domain.errors import SteamError
from steam_sda.domain.updates import UpdateRelease, release_from_github, RELEASES_URL
from steam_sda.infrastructure.updates.github import GitHubUpdates, installation_directory, UNINSTALL_KEY
from test_core import MemoryStore


PAYLOAD = b'MZ-synthetic-installer-not-executable'


def release(version='0.7.3', payload=PAYLOAD):
    name = f'SteamSwitcher-v{version}-Setup-x64.exe'
    return UpdateRelease(version, f'{RELEASES_URL}/download/v{version}/{name}',
                         hashlib.sha256(payload).hexdigest(), len(payload))


def metadata(value=None):
    value = value or release()
    return {'tag_name': f'v{value.version}', 'draft': False, 'prerelease': False,
            'assets': [{'name': value.filename, 'state': 'uploaded', 'size': value.size,
                        'digest': f'sha256:{value.sha256}', 'browser_download_url': value.url}]}


def session_for(data, url=None):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.side_effect = lambda size: iter([data[:5], data[5:]])
    response.url = url or release().url
    session = Mock(headers={})
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.get.return_value = response
    return session


class UpdateTests(unittest.TestCase):
    def test_only_newer_stable_release_and_exact_installer_are_accepted(self):
        self.assertEqual(release_from_github(metadata(), '0.7.2'), release())
        self.assertIsNone(release_from_github(metadata(), '0.7.3'))
        self.assertIsNone(release_from_github(metadata(), '0.8.0'))
        self.assertEqual(release_from_github(metadata(release('0.10.0')), '0.9.0').version, '0.10.0')
        for field, value in [('draft', True), ('prerelease', True), ('tag_name', 'v0.7.3-beta'), ('assets', [])]:
            data = metadata()
            data[field] = value
            with self.subTest(field=field), self.assertRaises(SteamError):
                release_from_github(data, '0.7.2')

    def test_wrong_repository_url_hash_size_and_duplicate_assets_fail_closed(self):
        for key, value in [('browser_download_url', 'https://example.org/update.exe'), ('digest', ''),
                           ('digest', 'sha256:wrong'), ('digest', None), ('size', 201 * 1024 * 1024), ('size', True)]:
            data = metadata()
            data['assets'][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(SteamError):
                release_from_github(data, '0.7.2')
        data = metadata()
        data['assets'] *= 2
        with self.assertRaises(SteamError): release_from_github(data, '0.7.2')

    def test_automatic_checks_are_daily_can_be_disabled_and_manual_check_bypasses_backoff(self):
        store, gateway = MemoryStore(), Mock()
        gateway.latest.return_value = release()
        service = UpdateService(store, gateway, '0.7.2')
        self.assertEqual(service.check(), release())
        self.assertEqual(service.check(), release())
        gateway.latest.assert_called_once()
        reopened = UpdateService(store, gateway, '0.7.2')
        self.assertEqual(reopened.check(), release())
        gateway.latest.assert_called_once()
        self.assertIsNone(UpdateService(store, gateway, '0.7.3').check())
        self.assertEqual(service.check(force=True), release())
        service.set_automatic(False)
        self.assertFalse(service.automatic())
        self.assertIsNone(service.check())
        self.assertEqual(service.check(force=True), release())

    def test_failed_check_does_not_hide_retry_and_downgrades_never_download_or_launch(self):
        store, gateway = MemoryStore(), Mock()
        gateway.latest.side_effect = SteamError('offline')
        service = UpdateService(store, gateway, '0.7.3')
        with self.assertRaises(SteamError): service.check()
        self.assertNotIn('checked_at', store.read('updates.json'))
        with self.assertRaises(SteamError): service.download(release())
        with self.assertRaises(SteamError): service.install(release(), 'ignored')
        gateway.download.assert_not_called()
        gateway.install.assert_not_called()

    def test_metadata_network_is_public_bounded_and_errors_are_readable(self):
        session = session_for(json.dumps(metadata()).encode())
        gateway = GitHubUpdates(session_factory=lambda: session)
        self.assertEqual(gateway.latest('0.7.2'), release())
        self.assertFalse(session.trust_env)
        self.assertNotIn('Authorization', session.headers)
        self.assertIn('api.github.com/repos/fabbiodev/steam-switcher/releases/latest', session.get.call_args.args[0])
        session.get.side_effect = requests.Timeout()
        with self.assertRaises(SteamError): gateway.latest('0.7.2')

    def test_streamed_installer_hash_and_size_are_verified_before_atomic_publish_and_cache_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            session = session_for(PAYLOAD)
            gateway = GitHubUpdates(directory, lambda: session)
            progress = []
            target = Path(gateway.download(release(), progress.append))
            self.assertEqual(target.read_bytes(), PAYLOAD)
            self.assertEqual(progress[-1], 100)
            self.assertEqual(list(Path(directory).glob('*.part')), [])
            self.assertEqual(gateway.download(release()), str(target))
            session.get.assert_called_once()

    def test_corruption_truncation_oversize_untrusted_redirect_and_cancellation_never_leave_a_runnable_update(self):
        cases = [(PAYLOAD[:-1], release().url, False), (PAYLOAD + b'x', release().url, False),
                 (b'x' * len(PAYLOAD), release().url, False), (PAYLOAD, 'https://example.org/a.exe', False),
                 (PAYLOAD, release().url, True)]
        for data, url, cancel in cases:
            with self.subTest(url=url, cancel=cancel), tempfile.TemporaryDirectory() as directory:
                gateway = GitHubUpdates(directory, lambda: session_for(data, url))
                with self.assertRaises(SteamError): gateway.download(release(), cancelled=lambda: cancel)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_install_rechecks_hash_detects_directory_and_launches_without_forced_application_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = Mock()
            gateway = GitHubUpdates(directory, lambda: session_for(PAYLOAD), launcher)
            path = gateway.download(release())
            destination = Path(directory) / 'installed elsewhere'
            with patch('steam_sda.infrastructure.updates.github.installation_directory', return_value=destination):
                gateway.install(release(), path)
            args = launcher.call_args.args[0]
            self.assertIn(f'/DIR={destination}', args)
            self.assertIn('/NOCLOSEAPPLICATIONS', args)
            self.assertIn('/NORESTART', args)
            self.assertEqual(launcher.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'], '1')
            Path(path).write_bytes(b'changed')
            launcher.reset_mock()
            with self.assertRaises(SteamError): gateway.install(release(), path)
            launcher.assert_not_called()

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows registry discovery')
    def test_registered_installation_wins_over_portable_copy_and_stale_registration_falls_back(self):
        import winreg
        with tempfile.TemporaryDirectory() as directory:
            registered = Path(directory) / 'registered'
            portable = Path(directory) / 'portable'
            registered.mkdir(); portable.mkdir()
            for path in (registered, portable): (path / 'SteamSDA.exe').write_bytes(b'fixture')
            with patch.object(winreg, 'OpenKey') as opened, patch.object(winreg, 'QueryValueEx', return_value=(str(registered), winreg.REG_SZ)):
                self.assertEqual(installation_directory(portable / 'SteamSDA.exe'), registered.resolve())
                opened.assert_called_with(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
            with patch.object(winreg, 'OpenKey', side_effect=FileNotFoundError):
                self.assertEqual(installation_directory(portable / 'SteamSDA.exe'), portable.resolve())
