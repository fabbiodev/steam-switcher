from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

from steam_sda.application.avatars import AvatarService
from steam_sda.domain.errors import SteamError, RateLimitError
from steam_sda.infrastructure.community.avatars import SteamAvatars, avatar_url, allowed_url, MAX_IMAGE
from steam_sda.presentation.qt.workers import AvatarWorker
from test_core import ONE, TWO

URL = 'https://avatars.akamai.steamstatic.com/' + 'a' * 40 + '_medium.jpg'
XML = f'<profile><steamID64>{ONE}</steamID64><avatarMedium><![CDATA[{URL}]]></avatarMedium></profile>'.encode()
PNG = b'\x89PNG\r\n\x1a\nsynthetic-image-payload'


def response(data=b'', status=200, headers=None):
    result = MagicMock(status_code=status, headers=headers or {})
    result.__enter__.return_value = result
    result.iter_content.side_effect = lambda size: iter([data[:10], data[10:]])
    return result


def session(*responses):
    result = MagicMock()
    result.__enter__.return_value = result
    result.get.side_effect = responses
    return result


class AvatarTests(unittest.TestCase):
    def test_profile_owner_and_cdn_are_verified_and_xml_entities_and_duplicate_fields_are_rejected(self):
        self.assertEqual(avatar_url(XML, ONE), URL)
        for data in (XML.replace(ONE.encode(), TWO.encode()), XML.replace(URL.encode(), b'https://example.org/a.jpg'),
                     XML.replace(b'</profile>', f'<steamID64>{ONE}</steamID64></profile>'.encode()),
                     XML.replace(b'</profile>', f'<avatarMedium>{URL}</avatarMedium></profile>'.encode()),
                     b'<!DOCTYPE profile [<!ENTITY x "unsafe">]>' + XML, b'not-xml'):
            with self.subTest(data=data[:30]), self.assertRaises(SteamError):
                avatar_url(data, ONE)
        for url in ('http://avatars.steamstatic.com/a.jpg', 'https://evil.steamstatic.com/a.jpg',
                    'https://avatars.steamstatic.com.example.org/a.jpg', 'https://user:pass@avatars.steamstatic.com/a.jpg',
                    'https://avatars.steamstatic.com:444/a.jpg', 'https://steamcdn-a.akamaihd.net/unrelated/a.jpg'):
            self.assertFalse(allowed_url(url))

    def test_download_uses_only_public_profile_and_image_requests_without_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            http = session(response(XML), response(PNG))
            gateway = SteamAvatars(directory, lambda: http)
            self.assertEqual(gateway.fetch(ONE), PNG)
            self.assertFalse(http.trust_env)
            calls = http.get.call_args_list
            self.assertEqual(calls[0].args[0], f'https://steamcommunity.com/profiles/{ONE}/?xml=1')
            self.assertEqual(calls[1].args[0], URL)
            self.assertTrue(all(call.kwargs['allow_redirects'] is False for call in calls))
            self.assertTrue(all('params' not in call.kwargs and 'auth' not in call.kwargs for call in calls))

    def test_external_redirect_oversize_wrong_content_and_cancelled_download_never_get_saved(self):
        for replies, cancel in (([response(status=302, headers={'Location': 'https://example.org/'})], False),
                                ([response(XML), response(b'x' * (MAX_IMAGE + 1))], False),
                                ([response(XML), response(b'<html>error</html>')], False),
                                ([], True)):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as directory:
                http = session(*replies)
                gateway = SteamAvatars(directory, lambda: http)
                with self.assertRaises(SteamError): gateway.fetch(ONE, lambda: cancel)
                self.assertEqual(list(Path(directory).iterdir()), [])
        http = session(response(status=429, headers={'Retry-After': '120'}))
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(RateLimitError):
            SteamAvatars(directory, lambda: http).fetch(ONE)

    def test_cache_survives_new_service_and_invalid_or_oversized_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = SteamAvatars(directory)
            gateway.save(ONE, PNG)
            with patch.object(gateway, 'fetch') as fetch:
                self.assertEqual(AvatarService(gateway).refresh(ONE), PNG)
                fetch.assert_not_called()
            cache = Path(directory) / f'{ONE}.img'
            cache.write_bytes(b'not-an-image')
            self.assertEqual(gateway.read(ONE), {})
            cache.write_bytes(b'x' * (MAX_IMAGE + 1))
            self.assertEqual(gateway.read(ONE), {})
            with self.assertRaises(SteamError): gateway.save('../outside', PNG)

    def test_refresh_updates_stale_cache_and_network_failure_retains_previous_image_with_backoff(self):
        gateway = Mock()
        gateway.read.return_value = {'data': PNG, 'updated_at': time.time() - 90000}
        gateway.fetch.return_value = PNG + b'new'
        service = AvatarService(gateway)
        self.assertEqual(service.refresh(ONE), PNG + b'new')
        gateway.save.assert_called_once_with(ONE, PNG + b'new')
        gateway.fetch.side_effect = SteamError('offline')
        gateway.fetch.reset_mock()
        self.assertEqual(service.refresh(TWO), PNG)
        self.assertEqual(service.refresh(TWO), PNG)
        gateway.fetch.assert_called_once()

    def test_rate_limit_blocks_requests_for_other_accounts_and_cancelled_refresh_does_not_write(self):
        gateway = Mock()
        gateway.read.return_value = {}
        gateway.fetch.side_effect = RateLimitError(300)
        service = AvatarService(gateway)
        self.assertEqual(service.refresh(ONE), b'')
        self.assertEqual(service.refresh(TWO), b'')
        gateway.fetch.assert_called_once()
        gateway.save.assert_not_called()
        gateway.fetch.side_effect = None
        other = AvatarService(gateway)
        self.assertEqual(other.refresh(ONE, lambda: True), b'')
        gateway.save.assert_not_called()

    def test_worker_emits_all_cached_avatars_before_refresh_and_does_not_repeat_unchanged_images(self):
        service = Mock()
        service.cached.side_effect = lambda sid: {'data': PNG + sid.encode()}
        service.refresh.side_effect = lambda sid, cancelled: PNG + sid.encode()
        events = []
        worker = AvatarWorker(service, [ONE, TWO], 4)
        worker.result.connect(lambda generation, sid, data: events.append((generation, sid)))
        service.refresh.side_effect = lambda sid, cancelled: (self.assertEqual(events, [(4, ONE), (4, TWO)]) or PNG + sid.encode())
        worker.run()
        self.assertEqual(events, [(4, ONE), (4, TWO)])
