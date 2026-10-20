import base64
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch

import requests

from steam_sda.errors import SteamError
from steam_sda.profile import fetch_level, parse_public_level
from steam_sda.steam import Switcher, JOURNAL, identity_from_log
from steam_sda.storage import Store
from steam_sda.vdf import accounts, select_account, valid_id, chooser_preference

ONE, TWO, THREE = "76561198000000001", "76561198000000002", "76561198000000003"
RAW = ('''// original comment\r
"users"\r
{\r
    "%s"\r
    {\r
        "AccountName" "first_login"\r
        "PersonaName" "Первый \\"ник\\""\r
        "RememberPassword" "1"\r
        "AllowAutoLogin" "1"\r
        "MostRecent" "1" // keep this\r
        "Timestamp" "1700000000"\r
        "UnknownField" "opaque-value"\r
    }\r
    "%s"\r
    {\r
        "AccountName" "second_login"\r
        "PersonaName" "Второй"\r
        "RememberPassword" "0"\r
        "AllowAutoLogin" "0"\r
        "MostRecent" "0"\r
    }\r
}\r
''' % (ONE, TWO)).encode()


class MemoryStore:
    def __init__(self): self.values = {}
    def read(self, name, encrypted=False): return copy.deepcopy(self.values.get(name, {}))
    def write(self, name, value, encrypted=False): self.values[name] = copy.deepcopy(value)
    def remove_journal(self): self.values.pop(JOURNAL, None)


class FakeNative:
    def __init__(self, directory):
        self.root = Path(directory)
        self.login_file = self.root / "loginusers.vdf"
        self.login_file.write_bytes(RAW)
        self.registry = {"exists": True, "value": "first_login", "kind": 1}
        self.remember = {"exists": False}
        self.chooser = "1"
        self.active = ONE
        self.auto_login = True
        self.launches = self.stops = 0
        self.stop_error = False
        self.fail_registry_once = False
        self.fail_launch_once = False
        self.after_stop = None

    @contextmanager
    def exclusive(self): yield
    def read_logins(self): return self.login_file.read_bytes()
    def account_list(self): return accounts(self.read_logins())
    def active_id(self): return self.active
    def registry_value(self): return copy.deepcopy(self.registry)
    def remember_value(self): return copy.deepcopy(self.remember)
    def set_remember(self, value): self.remember = copy.deepcopy(value)
    def chooser_value(self): return self.chooser
    def set_chooser(self, value):
        if value is not None: self.chooser = value
    def set_registry(self, value):
        if self.fail_registry_once:
            self.fail_registry_once = False
            raise SteamError("registry unavailable")
        self.registry = copy.deepcopy(value)
    def stop(self):
        self.stops += 1
        if self.stop_error: raise SteamError("running game")
        self.active = None
        if self.after_stop: self.after_stop()
    def launch(self):
        self.launches += 1
        if self.fail_launch_once:
            self.fail_launch_once = False
            raise SteamError('launch failed')
        if self.auto_login:
            row = next((a for a in self.account_list() if a["login"] == self.registry.get("value")), None)
            self.active = row["steam_id"] if row else None


class VdfTests(unittest.TestCase):
    def test_autologin_moves_to_selected_without_forgetting_other_accounts(self):
        raw = RAW.replace(b'"Timestamp"', b'"AutoLogin" "1"\n        "Timestamp"').replace(b'"PersonaName" "\xd0\x92', b'"AutoLogin" "0"\n        "PersonaName" "\xd0\x92')
        result = select_account(raw, TWO)
        self.assertIn(b'"AutoLogin" "0"\n        "Timestamp"', result)
        self.assertIn(b'"AutoLogin" "1"\n        "PersonaName"', result)
        self.assertEqual([a['remembered'] for a in accounts(result)], [True, False])

    def test_chooser_patch_preserves_other_settings_and_checks_path(self):
        raw = b'"InstallConfigStore" { "WebStorage" { "Auth" { "AlwaysShowUserChooser" "1" "Other" "secret" } } "Other" "1" }'
        self.assertEqual(chooser_preference(raw), "1")
        self.assertEqual(chooser_preference(raw, "0"), raw.replace(b'"AlwaysShowUserChooser" "1"', b'"AlwaysShowUserChooser" "0"'))
        self.assertIsNone(chooser_preference(b'"InstallConfigStore" { "Other" "1" }'))

    def test_lossless_selection_preserves_unknown_fields_comments_and_auth_flags(self):
        rows = accounts(RAW)
        self.assertTrue(rows[0]["remembered"])
        self.assertFalse(rows[1]["remembered"])
        result = select_account(RAW, TWO)
        expected = RAW.replace(b'"MostRecent" "1"', b'"MostRecent" "0"', 1)
        expected = expected.replace(b'"MostRecent" "0"\r\n    }', b'"MostRecent" "1"\r\n    }', 1)
        self.assertEqual(result, expected)
        self.assertEqual(accounts(result)[0]["name"], 'Первый "ник"')
        self.assertFalse(accounts(result)[1]["remembered"])

    def test_missing_most_recent_is_inserted_and_bom_survives(self):
        missing = RAW.replace(b'        "MostRecent" "0"\r\n', b'')
        result = select_account(b'\xef\xbb\xbf' + missing, TWO)
        self.assertTrue(result.startswith(b'\xef\xbb\xbf'))
        self.assertEqual([a["steam_id"] for a in accounts(result) if a["recent"]], [TWO])

    def test_duplicate_fields_ids_and_malformed_input_are_rejected(self):
        for raw in (b'"users" {', b'"users" {} "users" {}',
                    RAW.replace(b'"second_login"', b'"second_login" "AccountName" "other"'),
                    RAW.replace(TWO.encode(), ONE.encode()), b'"users" { "not-an-id" {} }'):
            with self.assertRaises(SteamError): accounts(raw)
        with self.assertRaises(SteamError): select_account(RAW, THREE)
        self.assertFalse(valid_id("../../76561198000000001"))


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.native = FakeNative(self.directory.name)
        self.store = MemoryStore()
        self.switcher = Switcher(self.native, self.store, timeout=0)

    def tearDown(self): self.directory.cleanup()

    def test_success_requires_active_owner_and_does_not_change_other_account_fields(self):
        result = self.switcher.switch(TWO)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(self.native.active, TWO)
        self.assertEqual(self.native.registry["value"], "second_login")
        self.assertFalse(accounts(self.native.read_logins())[1]["remembered"])
        self.assertFalse(self.switcher.has_recovery())

    def test_password_or_guard_prompt_is_pending_not_a_success(self):
        self.native.auto_login = False
        result = self.switcher.switch(TWO)
        self.assertEqual(result["state"], "login")
        self.assertTrue(self.switcher.has_recovery())
        with self.assertRaises(SteamError): self.switcher.switch(ONE)
        self.native.active = TWO
        self.assertTrue(self.switcher.complete_if_active())
        self.assertFalse(self.switcher.has_recovery())

    def test_running_game_does_not_mutate_files_registry_or_create_journal(self):
        self.native.stop_error = True
        with self.assertRaises(SteamError): self.switcher.switch(TWO)
        self.assertEqual(self.native.read_logins(), RAW)
        self.assertEqual(self.native.registry["value"], "first_login")
        self.assertFalse(self.store.values)
        self.assertEqual(self.native.launches, 0)

    def test_registry_failure_rolls_back_original_file_before_restarting(self):
        self.native.fail_registry_once = True
        with self.assertRaises(SteamError): self.switcher.switch(TWO)
        self.assertEqual(self.native.read_logins(), RAW)
        self.assertEqual(self.native.active, ONE)
        self.assertEqual(self.native.chooser, "1")
        self.assertFalse(self.switcher.has_recovery())

    def test_metadata_is_read_again_after_steam_exits(self):
        updated = RAW.replace(b'"opaque-value"', b'"fresh-value"')
        self.native.after_stop = lambda: self.native.login_file.write_bytes(updated)
        self.switcher.switch(TWO)
        self.assertIn(b'fresh-value', self.native.read_logins())

    def test_native_remember_preference_is_enabled_only_for_a_remembered_target(self):
        remembered = RAW.replace(b'"RememberPassword" "0"', b'"RememberPassword" "1"').replace(b'"AllowAutoLogin" "0"', b'"AllowAutoLogin" "1"')
        self.native.login_file.write_bytes(remembered)
        self.switcher.switch(TWO)
        self.assertEqual(self.native.remember, {"exists": True, "value": 1, "kind": 4})

    def test_launch_failure_restores_original_remember_preference_and_selection(self):
        remembered = RAW.replace(b'"RememberPassword" "0"', b'"RememberPassword" "1"').replace(b'"AllowAutoLogin" "0"', b'"AllowAutoLogin" "1"')
        self.native.login_file.write_bytes(remembered)
        self.native.fail_launch_once = True
        with self.assertRaises(SteamError): self.switcher.switch(TWO)
        self.assertEqual(self.native.remember, {"exists": False})
        self.assertEqual(self.native.read_logins(), remembered)
        self.assertEqual(self.native.active, ONE)

    def test_invalid_recovery_registry_cannot_partially_restore_files(self):
        self.native.auto_login = False
        self.switcher.switch(TWO)
        self.store.values[JOURNAL]['remember'] = {'exists': True, 'kind': 4, 'value': 'not-an-integer'}
        selected = self.native.read_logins()
        with self.assertRaises(SteamError): self.switcher.restore()
        self.assertEqual(self.native.read_logins(), selected)

    def test_recovery_preserves_credentials_refreshed_by_steam_and_new_accounts(self):
        self.native.auto_login = False
        self.switcher.switch(TWO)
        changed = self.native.read_logins().replace(b'"opaque-value"', b'"refreshed-value"')
        end = changed.rfind(b'}')
        extra = ('"%s" {"AccountName" "third" "PersonaName" "Third" "MostRecent" "0"}\r\n' % THREE).encode()
        self.native.login_file.write_bytes(changed[:end] + extra + changed[end:])
        self.switcher.restore()
        self.assertIn(b'refreshed-value', self.native.read_logins())
        self.assertEqual(len(self.native.account_list()), 3)
        self.assertEqual([a["steam_id"] for a in self.native.account_list() if a["recent"]], [ONE])
        self.assertEqual(self.native.registry["value"], "first_login")

    def test_corrupt_or_other_installation_journal_is_not_applied(self):
        self.native.auto_login = False
        self.switcher.switch(TWO)
        self.store.values[JOURNAL]["root"] = "C:/other-steam"
        selected = self.native.read_logins()
        with self.assertRaises(SteamError): self.switcher.restore()
        self.assertEqual(self.native.read_logins(), selected)


class ProfileTests(unittest.TestCase):
    def page(self, sid=ONE, level="64"):
        return '<meta property="og:url" content="https://steamcommunity.com/profiles/%s/"/><div class="profile_header_badgeinfo"><span class="friendPlayerLevelNum">%s</span></div><script>var g_rgProfileData = %s;</script>' % (sid, level, json.dumps({"steamid": sid}))

    def test_public_level_checks_identity_and_is_not_legacy_steam_rating(self):
        self.assertEqual(parse_public_level(self.page(), ONE), 64)
        for page in (self.page(TWO), self.page(level=""), '<steamRating>10</steamRating>',
                     self.page() + '<div class="profile_header_badgeinfo"><span class="friendPlayerLevelNum">7</span></div>'):
            with self.assertRaises(SteamError): parse_public_level(page, ONE)

    def test_friend_levels_are_never_used_as_the_account_level(self):
        friends = '<div class="profile_topfriends"><div class="friendBlock"><span class="friendPlayerLevelNum">999</span></div></div>'
        self.assertEqual(parse_public_level(self.page(level="9") + friends, ONE), 9)
        with self.assertRaises(SteamError):
            parse_public_level(self.page(level="") + friends, ONE)

    def test_public_request_has_no_private_headers_or_key_and_api_zero_is_valid(self):
        factory = MagicMock()
        session = factory.return_value.__enter__.return_value
        response = Mock(status_code=200, text=self.page(), content=self.page().encode())
        session.get.return_value = response
        data = fetch_level(ONE, session_factory=factory)
        self.assertEqual(data["level"], 64)
        session.get.assert_called_once_with(f'https://steamcommunity.com/profiles/{ONE}/?l=english', timeout=(5, 15), allow_redirects=False)
        self.assertFalse(session.trust_env)
        response.json.return_value = {"response": {"player_level": 0}}
        self.assertEqual(fetch_level(ONE, "key", factory)["level"], 0)
        response.json.return_value = {"response": {"player_level": True}}
        with self.assertRaises(SteamError): fetch_level(ONE, "key", factory)

    def test_api_key_and_raw_error_body_are_never_in_error_messages(self):
        factory = MagicMock()
        session = factory.return_value.__enter__.return_value
        session.get.side_effect = requests.ConnectionError("https://api/?key=private-key")
        with self.assertRaises(SteamError) as caught: fetch_level(ONE, "private-key", factory)
        self.assertNotIn("private-key", str(caught.exception))

    def test_vanity_redirect_is_bounded_to_steam_and_owner_is_still_verified(self):
        factory = MagicMock()
        session = factory.return_value.__enter__.return_value
        redirect = Mock(status_code=302, headers={"Location": "https://steamcommunity.com/id/example/"})
        response = Mock(status_code=200, text=self.page(), content=self.page().encode())
        session.get.side_effect = [redirect, response]
        self.assertEqual(fetch_level(ONE, session_factory=factory)["level"], 64)
        self.assertEqual(session.get.call_count, 2)
        session.get.reset_mock()
        redirect.headers["Location"] = "https://other.example/id/test"
        session.get.side_effect = [redirect]
        with self.assertRaises(SteamError): fetch_level(ONE, session_factory=factory)
        self.assertEqual(session.get.call_count, 1)

    def test_connection_log_is_scoped_to_process_lifetime_and_clears_on_logout(self):
        from datetime import datetime
        started = datetime(2026, 10, 20, 12, 0).timestamp()
        aid = int(ONE) - 76561197960265728
        old = f"[2026-10-19 12:00:00] [Logged On, 1, 2] [U:1:{aid}] RecvMsgClientLogOnResponse() : processing complete"
        new = f"[2026-10-20 12:00:01] [Logged On, 1, 2] [U:1:{aid}] RecvMsgClientLogOnResponse() : processing complete"
        self.assertIsNone(identity_from_log(old, started, started + 10))
        self.assertEqual(identity_from_log(old + '\n' + new, started, started + 10), ONE)
        self.assertIsNone(identity_from_log(new + f"\n[2026-10-20 12:00:02] [Logged Off, 1, 0] [U:1:{aid}] ConnectionDisconnected", started, started + 10))


class StoreTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_secrets_encrypted_and_atomic_failure_keeps_old_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.write('api-key.dpapi', {"key": "private-key"}, encrypted=True)
            self.assertNotIn(b'private-key', (Path(directory) / 'api-key.dpapi').read_bytes())
            self.assertEqual(store.read('api-key.dpapi', encrypted=True)["key"], "private-key")
            store.write('settings.json', {"value": "old"})
            with patch('steam_sda.storage.os.replace', side_effect=OSError('disk full')):
                with self.assertRaises(SteamError): store.write('settings.json', {"value": "new"})
            self.assertEqual(store.read('settings.json')["value"], "old")
