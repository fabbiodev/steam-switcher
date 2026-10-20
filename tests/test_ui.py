import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

from steam_sda.ui import MainWindow
from test_core import MemoryStore, ONE, TWO


class NativeFixture:
    root = Path('C:/fake-steam')
    login_file = root / 'config/loginusers.vdf'

    def account_list(self):
        return [{"steam_id": ONE, "name": "First", "login": "first_login", "remembered": True},
                {"steam_id": TWO, "name": "Second", "login": "second_login", "remembered": False}]

    def active_id(self): return ONE
    def games_running(self): return False


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.store = MemoryStore()
        self.win = MainWindow(NativeFixture(), self.store, start_services=False)

    def tearDown(self):
        self.win.close()
        self.win.deleteLater()
        self.app.processEvents()

    def test_current_account_cannot_be_switched_and_search_matches_login(self):
        self.assertEqual(self.win.selected_id, ONE)
        self.assertFalse(self.win.switch_button.isEnabled())
        self.win.search.setText('second_login')
        self.assertEqual(self.win.list.count(), 1)
        self.assertEqual(self.win.selected_id, TWO)
        self.assertTrue(self.win.switch_button.isEnabled())
        self.assertIn('Steam Guard', self.win.session_note.text())

    def test_failed_refresh_keeps_cached_level_and_old_generation_is_ignored(self):
        self.win._level_result(self.win.generation, ONE, {"level": 64, "source": "Steam Community", "updated_at": 1})
        self.win._level_result(self.win.generation, ONE, {"error": "Недоступно"})
        self.assertEqual(self.win.level.text(), '64')
        self.assertIn('Сохранённый', self.win.level_note.text())
        self.win._level_result(self.win.generation - 1, ONE, {"level": 999})
        self.assertEqual(self.win.level.text(), '64')

    def test_switch_button_dispatches_selected_account(self):
        self.win.list.setCurrentRow(1)
        with patch.object(self.win, '_begin_switch') as begin:
            self.win.switch_button.click()
        begin.assert_called_once_with(TWO)

    def test_untrusted_persona_name_is_plain_text(self):
        self.win.account_rows[0]['name'] = '<b>not markup</b>'
        self.win.render()
        self.assertEqual(self.win.name.text(), '<b>not markup</b>')

    def test_imported_accounts_appear_and_duplicate_native_account_does_not(self):
        from test_accounts_auth import record
        from test_core import THREE
        self.win.vault.save(record())
        self.win.vault.save(record(THREE, 'third_login'))
        self.win.reload_accounts(refresh_levels=False)
        self.assertEqual(len(self.win.account_rows), 3)
        self.win.search.setText('third_login')
        self.assertEqual(self.win.list.count(), 1)
        self.assertEqual(self.win.selected_id, THREE)
        self.assertTrue(self.win.switch_button.isEnabled())

    def test_automatic_auth_waits_until_due_and_invalid_password_disables_retries(self):
        import time
        from test_accounts_auth import record
        row = record()
        row['next_check'] = time.time() + 3600
        self.win.vault.save(row)
        self.win.managed = self.win.vault.all()
        self.win._start_services = True
        with patch.object(self.win, '_begin_auth') as begin:
            self.win._auto_auth()
            begin.assert_not_called()
            self.win.managed[ONE]['next_check'] = 0
            self.win._auto_auth()
            begin.assert_called_once_with(ONE, automatic=True)
        self.win._auth_result({'steam_id': ONE, 'error': 'Неверный пароль', 'requires_input': True})
        self.assertFalse(self.win.vault.get(ONE)['auto_restore'])

    def test_lost_desktop_session_recovers_its_owner_even_if_another_card_is_selected(self):
        from unittest.mock import Mock
        from test_accounts_auth import record
        self.win.vault.save(record())
        self.win.managed = self.win.vault.all()
        self.win.list.setCurrentRow(1)
        self.win.native.active_id = Mock(return_value=None)
        self.win.native.processes = Mock(return_value=[object()])
        self.win.native.registry_value = Mock(return_value={'value': 'first_login'})
        with patch('steam_sda.ui.time.monotonic', return_value=100):
            self.win.tick()
        self.assertEqual(self.win._recover_account, ONE)
        with patch('steam_sda.ui.time.monotonic', return_value=116), patch.object(self.win, '_begin_switch') as begin:
            self.win.tick()
            begin.assert_called_once_with(ONE)

    def test_closing_steam_does_not_start_it_again(self):
        from unittest.mock import Mock
        from test_accounts_auth import record
        self.win.vault.save(record())
        self.win.managed = self.win.vault.all()
        self.win.native.active_id = Mock(return_value=None)
        self.win.native.processes = Mock(return_value=[])
        with patch('steam_sda.ui.time.monotonic', return_value=100):
            self.win.tick()
        with patch('steam_sda.ui.time.monotonic', return_value=116), patch.object(self.win, '_begin_switch') as begin:
            self.win.tick()
            begin.assert_not_called()

    def test_selected_native_session_can_be_exported_without_scanning_other_tokens(self):
        from test_accounts_auth import jwt
        from steam_sda.accounts import metadata
        row = metadata(self.win.selected)
        with patch.object(self.win.desktop_token, 'selected_token', return_value=jwt(refresh=True)) as selected:
            self.win._capture_selected_token(row)
            selected.assert_called_once()
            self.assertEqual(selected.call_args.args[0]['steam_id'], ONE)
        self.assertTrue(row['session']['refresh_token'])
