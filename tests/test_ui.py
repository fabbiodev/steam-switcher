from steam_sda.bootstrap import create_services
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
import locale
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication, QDialog

from steam_sda.presentation.qt.window import MainWindow
from steam_sda.presentation.qt.dialogs import ExportDialog, ArchivePasswordDialog
from test_core import MemoryStore, ONE, TWO


class NativeFixture:
    root = Path('C:/fake-steam')
    login_file = root / 'config/loginusers.vdf'

    def login_revision(self): return None

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
        self.win = MainWindow(create_services(NativeFixture(), self.store, enable_devices=False), start_services=False)

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

    def test_updates_dialog_uses_injected_service_and_does_not_modify_steam(self):
        from unittest.mock import Mock
        from steam_sda.presentation.qt.update_dialog import UpdateDialog
        from steam_sda.domain.updates import UpdateRelease, RELEASES_URL
        service = Mock()
        service.automatic.return_value = True
        release = UpdateRelease('0.7.3', f'{RELEASES_URL}/download/v0.7.3/SteamSwitcher-v0.7.3-Setup-x64.exe', 'a' * 64, 100)
        dialog = UpdateDialog(service, release, self.win)
        self.assertTrue(dialog.install_button.isEnabled())
        self.assertIn('0.7.3', dialog.status.text())
        dialog.automatic.setChecked(False)
        service.set_automatic.assert_called_once_with(False)
        service.install.assert_not_called()
        self.assertFalse(self.win._update_dialog_open)
        dialog.deleteLater()

    def test_update_handoff_is_deferred_until_window_can_close_and_failure_keeps_it_open(self):
        from unittest.mock import Mock
        from steam_sda.domain.errors import SteamError
        from PyQt6.QtGui import QCloseEvent
        service = Mock()
        self.win.services.updates = service
        self.win._update_to_install = ('release-fixture', 'path-fixture')
        self.win.auth_worker = object()
        event = QCloseEvent()
        self.win.closeEvent(event)
        self.assertFalse(event.isAccepted())
        service.install.assert_not_called()
        self.win.auth_worker = None
        service.install.side_effect = SteamError('Synthetic update launch failure')
        self.win.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertIsNone(self.win._update_to_install)
        self.assertFalse(self.win._closing)
        self.assertIn('Synthetic update launch failure', self.win.banner.text())

    def test_update_dialog_finishes_background_check_and_download_before_handoff(self):
        from unittest.mock import Mock
        from PyQt6.QtTest import QTest
        from steam_sda.presentation.qt.update_dialog import UpdateDialog
        from steam_sda.domain.updates import UpdateRelease, RELEASES_URL
        service = Mock()
        service.automatic.return_value = True
        release = UpdateRelease('0.7.3', f'{RELEASES_URL}/download/v0.7.3/SteamSwitcher-v0.7.3-Setup-x64.exe', 'a' * 64, 100)
        service.check.return_value = release
        service.download.return_value = 'synthetic-installer-path'
        dialog = UpdateDialog(service, parent=self.win)
        dialog.check()
        for _ in range(200):
            QTest.qWait(5)
            if dialog.worker is None: break
        self.assertIsNone(dialog.worker)
        self.assertEqual(dialog.release, release)
        self.assertTrue(dialog.install_button.isEnabled())
        dialog.download()
        for _ in range(200):
            QTest.qWait(5)
            if dialog.worker is None: break
        self.assertIsNone(dialog.worker)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.installer_path, 'synthetic-installer-path')
        service.install.assert_not_called()
        dialog.deleteLater()

    def test_export_dialog_accepts_empty_short_and_long_passwords_with_secrets(self):
        for password in ('', 'x', 'я', ' ', 'x' * 40000):
            with self.subTest(length=len(password)):
                dialog = ExportDialog(True, self.win)
                dialog.password.setText(password)
                dialog.confirm.setText(password)
                self.assertEqual(dialog.password.text(), password)
                self.assertIn('AES-256' if password else 'без шифрования', dialog.protection_note.text())
                dialog._accept()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                dialog.deleteLater()

    def test_export_dialog_rejects_mismatched_passwords(self):
        dialog = ExportDialog(True, self.win)
        dialog.password.setText('x')
        dialog.confirm.setText('y')
        dialog._accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertEqual(dialog.error.text(), 'Пароли не совпадают.')
        dialog.deleteLater()

    def test_import_dialog_preserves_passwords_beyond_default_input_limit(self):
        dialog = ArchivePasswordDialog(self.win)
        password = 'я' * 40000
        dialog.setTextValue(password)
        self.assertEqual(dialog.textValue(), password)
        dialog.deleteLater()

    def test_failed_refresh_keeps_cached_level_and_old_generation_is_ignored(self):
        self.win._level_result(self.win.generation, ONE, {"level": 64, "source": "Steam Community", "updated_at": 1})
        self.win._level_result(self.win.generation, ONE, {"error": "Недоступно"})
        self.assertEqual(self.win.level.text(), '64')
        self.assertIn('Сохранённый', self.win.level_note.text())
        self.win._level_result(self.win.generation - 1, ONE, {"level": 999})
        self.assertEqual(self.win.level.text(), '64')

    def test_level_update_date_renders_with_non_cyrillic_windows_locale(self):
        original = locale.setlocale(locale.LC_ALL)
        try:
            locale.setlocale(locale.LC_ALL, 'English_United States.1252' if os.name == 'nt' else 'C')
            stamp = datetime(2026, 1, 2, 3, 4).timestamp()
            self.win._level_result(self.win.generation, ONE, {
                'level': 64, 'source': 'Steam Community', 'updated_at': stamp,
            })
            self.assertEqual(self.win.level_note.text(), 'Steam Community · обновлено 02.01 в 03:04')
        finally:
            locale.setlocale(locale.LC_ALL, original)

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
        with patch('steam_sda.presentation.qt.window.time.monotonic', return_value=100):
            self.win.tick()
        self.assertEqual(self.win.recovery.pending_sid, ONE)
        with patch('steam_sda.presentation.qt.window.time.monotonic', return_value=116), patch.object(self.win, '_begin_switch') as begin:
            self.win.tick()
            begin.assert_called_once_with(ONE)

    def test_closing_steam_does_not_start_it_again(self):
        from unittest.mock import Mock
        from test_accounts_auth import record
        self.win.vault.save(record())
        self.win.managed = self.win.vault.all()
        self.win.native.active_id = Mock(return_value=None)
        self.win.native.processes = Mock(return_value=[])
        with patch('steam_sda.presentation.qt.window.time.monotonic', return_value=100):
            self.win.tick()
        with patch('steam_sda.presentation.qt.window.time.monotonic', return_value=116), patch.object(self.win, '_begin_switch') as begin:
            self.win.tick()
            begin.assert_not_called()

    def test_selected_native_session_can_be_exported_without_scanning_other_tokens(self):
        from test_accounts_auth import jwt
        from steam_sda.domain.accounts import metadata
        row = metadata(self.win.selected)
        with patch.object(self.win.desktop_token, 'selected_token', return_value=jwt(refresh=True)) as selected:
            self.win.accounts.capture_selected_token(row)
            selected.assert_called_once()
            self.assertEqual(selected.call_args.args[0]['steam_id'], ONE)
        self.assertTrue(row['session']['refresh_token'])

    def test_export_database_includes_every_account_even_with_filtered_selection(self):
        import tempfile
        from steam_sda.infrastructure.persistence.archives import import_accounts
        self.win.search.setText('second_login')
        self.assertEqual(self.win.list.count(), 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'accounts.zip'
            with patch.object(self.win.desktop_token, 'selected_token', return_value=''), \
                    patch('steam_sda.presentation.qt.window.ExportDialog.exec', return_value=QDialog.DialogCode.Accepted), \
                    patch('steam_sda.presentation.qt.window.QFileDialog.getSaveFileName', return_value=(str(path), 'ZIP')):
                self.win.export_button.click()
            self.assertEqual({r['steam_id'] for r in import_accounts(path)}, {ONE, TWO})
            self.assertIn('Аккаунтов: 2', self.win.banner.text())

    def test_import_database_adds_all_rows_without_starting_an_account(self):
        import tempfile
        from test_accounts_auth import record
        from test_core import THREE
        from steam_sda.infrastructure.persistence.archives import export_accounts
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'accounts.zip'
            export_accounts(path, [record(), record(THREE, 'third_login')])
            with patch('steam_sda.presentation.qt.window.QFileDialog.getOpenFileName', return_value=(str(path), 'ZIP')), \
                    patch('steam_sda.presentation.qt.window.ArchivePasswordDialog.exec', return_value=QDialog.DialogCode.Accepted), \
                    patch.object(self.win, '_begin_auth') as auth:
                self.win.import_button.click()
            self.assertEqual(set(self.win.vault.all()), {ONE, THREE})
            self.assertEqual({r['steam_id'] for r in self.win.account_rows}, {ONE, TWO, THREE})
            auth.assert_not_called()

    def test_delete_requires_confirmation_and_removes_only_selected_account(self):
        with patch('steam_sda.presentation.qt.window.DeleteAccountDialog.exec', return_value=QDialog.DialogCode.Rejected):
            self.win.delete_selected()
        self.assertEqual(len(self.win.account_rows), 2)
        self.win.list.setCurrentRow(1)
        with patch('steam_sda.presentation.qt.window.DeleteAccountDialog.exec', return_value=QDialog.DialogCode.Accepted):
            self.win.delete_button.click()
        self.assertEqual([r['steam_id'] for r in self.win.account_rows], [ONE])
        self.win.reload_accounts(refresh_levels=False)
        self.assertEqual(self.win.list.count(), 1)
        self.assertIn(TWO, self.win.accounts.vault.excluded())
        self.assertIn('удалён', self.win.banner.text())

    def test_tabs_and_busy_state_keep_account_operations_available_and_guarded(self):
        self.win.details_tab.click()
        self.assertEqual(self.win.pages.currentIndex(), 1)
        self.assertTrue(self.win.delete_button.isEnabled())
        self.win.overview_tab.click()
        self.assertEqual(self.win.pages.currentIndex(), 0)
        self.win.auth_worker = object()
        try:
            self.win.render()
            self.assertFalse(self.win.delete_button.isEnabled())
            self.assertFalse(self.win.import_button.isEnabled())
            self.assertFalse(self.win.export_button.isEnabled())
            with patch('steam_sda.presentation.qt.window.DeleteAccountDialog') as dialog:
                self.win.delete_selected()
                dialog.assert_not_called()
        finally:
            self.win.auth_worker = None

    def test_empty_search_disables_account_actions_and_activity_is_bounded(self):
        self.win.search.setText('no_such_account')
        self.assertEqual(self.win.list.count(), 0)
        for action in (self.win.switch_button, self.win.delete_button, self.win.copy_button):
            self.assertFalse(action.isEnabled())
        self.assertTrue(self.win.export_button.isEnabled())
        for i in range(30): self.win.message(f'Synthetic event {i}')
        self.assertEqual(self.win.activity_list.count(), 20)
        self.assertIn('Synthetic event 29', self.win.activity_list.item(0).text())

    def test_button_press_animation_does_not_delay_action_and_finishes_on_release(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        self.win.show()
        self.app.processEvents()
        self.win.list.setCurrentRow(1)
        with patch.object(self.win, '_begin_switch') as begin:
            QTest.mousePress(self.win.switch_button, Qt.MouseButton.LeftButton)
            self.win.switch_button.press_animation.setCurrentTime(90)
            self.assertEqual(self.win.switch_button.pressDepth, 1)
            begin.assert_not_called()
            QTest.mouseRelease(self.win.switch_button, Qt.MouseButton.LeftButton)
            begin.assert_called_once_with(TWO)
            self.win.switch_button.press_animation.setCurrentTime(90)
            self.assertEqual(self.win.switch_button.pressDepth, 0)

    def test_confirmation_displays_untrusted_account_name_as_plain_text(self):
        from steam_sda.presentation.qt.dialogs import DeleteAccountDialog
        from PyQt6.QtWidgets import QLabel
        from PyQt6.QtCore import Qt
        dialog = DeleteAccountDialog({**self.win.selected, 'name': '<b>unsafe</b>'}, self.win)
        title = next(widget for widget in dialog.findChildren(QLabel) if '<b>unsafe</b>' in widget.text())
        self.assertEqual(title.textFormat(), Qt.TextFormat.PlainText)
        dialog.deleteLater()

    def test_list_slide_does_not_delay_selection_and_search_restores_full_opacity(self):
        self.app.setProperty('animationsEnabled', True)
        try:
            self.win.show()
            self.app.processEvents()
            self.win.search.setText('second_login')
            self.assertEqual(self.win.selected_id, TWO)
            self.assertEqual(self.win.list.count(), 1)
            self.assertEqual(self.win.name.text(), 'Second')
            self.win.list.reveal_animation.setCurrentTime(240)
            self.assertEqual(self.win.list.revealProgress, 1)
            self.win.search.clear()
            self.assertEqual(self.win.list.count(), 2)
            self.win.list.setCurrentRow(0)
            self.assertEqual(self.win.selected_id, ONE)
            self.win.list.reveal_animation.setCurrentTime(240)
            self.assertEqual(self.win.list.revealProgress, 1)
        finally:
            self.app.setProperty('animationsEnabled', False)

    def test_smooth_list_scroll_accumulates_input_clamps_bounds_and_stops_when_hidden(self):
        from steam_sda.presentation.qt.account_list import AnimatedAccountList
        listing = AnimatedAccountList()
        listing.resize(260, 200)
        listing.addItems([f'Account {i}' for i in range(80)])
        self.app.setProperty('animationsEnabled', True)
        try:
            listing.show()
            self.app.processEvents()
            listing.smooth_scroll(160)
            listing.smooth_scroll(160)
            self.assertEqual(listing.scroll_animation.endValue(), 320)
            listing.scroll_animation.setCurrentTime(170)
            self.assertEqual(listing.verticalScrollBar().value(), 320)
            listing.smooth_scroll(-10000)
            listing.scroll_animation.setCurrentTime(170)
            self.assertEqual(listing.verticalScrollBar().value(), 0)
            listing.smooth_scroll(100000)
            listing.scroll_animation.setCurrentTime(170)
            self.assertEqual(listing.verticalScrollBar().value(), listing.verticalScrollBar().maximum())
            from PyQt6.QtCore import Qt
            from PyQt6.QtTest import QTest
            listing.smooth_scroll(-160)
            QTest.keyClick(listing, Qt.Key.Key_Home)
            self.assertEqual(listing.currentRow(), 0)
            self.assertEqual(listing.scroll_animation.state(), listing.scroll_animation.State.Stopped)
            listing.reveal()
            listing.hide()
            self.assertEqual(listing.revealProgress, 1)
            self.assertEqual(listing.scroll_animation.state(), listing.scroll_animation.State.Stopped)
        finally:
            listing.close()
            listing.deleteLater()
            self.app.setProperty('animationsEnabled', False)

    def test_reduced_motion_disables_list_slide_and_scroll_animation(self):
        self.app.setProperty('animationsEnabled', False)
        self.win.show()
        self.app.processEvents()
        self.win.list.reveal()
        self.assertEqual(self.win.list.revealProgress, 1)
        self.assertEqual(self.win.list.reveal_animation.state(), self.win.list.reveal_animation.State.Stopped)
        self.win.list.smooth_scroll(160)
        self.assertEqual(self.win.list.scroll_animation.state(), self.win.list.scroll_animation.State.Stopped)

    def test_save_temporary_session_is_visible_and_does_not_restart_or_authenticate(self):
        from test_accounts_auth import jwt
        token = jwt(TWO, refresh=True)
        self.win.list.setCurrentRow(1)
        with patch.object(self.win.desktop_token, 'selected_token', return_value=token), \
                patch.object(self.win, '_begin_switch') as switch, patch.object(self.win, '_begin_auth') as auth:
            self.win.save_session_button.click()
        self.assertEqual(self.win.vault.get(TWO)['session']['refresh_token'], token)
        self.assertIn('Разовый вход', self.win.session_state.text())
        self.assertIn('сессия в базе', self.win.session_state.text())
        switch.assert_not_called()
        auth.assert_not_called()

    def test_hover_feedback_fades_between_rows_and_disabled_motion_is_immediate(self):
        listing = self.win.list
        self.app.setProperty('animationsEnabled', True)
        try:
            self.win.show()
            self.app.processEvents()
            listing.set_hover_row(0)
            listing.hover_animation.setCurrentTime(140)
            self.assertEqual(listing.hover_amount(0), 1)
            listing.set_hover_row(1)
            listing.hover_animation.setCurrentTime(140)
            self.assertEqual(listing.hover_amount(0), 0)
            self.assertEqual(listing.hover_amount(1), 1)
            listing.set_hover_row(-1)
            listing.hover_animation.setCurrentTime(140)
            self.assertEqual(listing.hover_amount(1), 0)
        finally:
            self.app.setProperty('animationsEnabled', False)

    def test_search_focus_glow_preserves_typing_and_keyboard_selection(self):
        self.app.setProperty('animationsEnabled', True)
        try:
            self.win.show()
            self.app.processEvents()
            self.win.search.setFocus()
            self.app.processEvents()
            self.win.search.glow_animation.setCurrentTime(140)
            self.assertEqual(self.win.search.glowProgress, 1)
            self.win.search.setText('second_login')
            self.assertEqual(self.win.selected_id, TWO)
        finally:
            self.app.setProperty('animationsEnabled', False)
