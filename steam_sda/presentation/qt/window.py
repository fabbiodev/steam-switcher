"""Qt view: widgets, user input and dispatch to injected application services."""
from datetime import datetime
from pathlib import Path
import time
import webbrowser
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QHBoxLayout, QMainWindow,
    QWidget, QFrame, QListWidget, QListWidgetItem, QLineEdit, QProgressBar, QScrollArea,
    QLayout, QFileDialog)
from steam_sda.domain.errors import SteamError
from steam_sda.domain.accounts import claims, metadata
from steam_sda.domain.identity import valid_id
from steam_sda.domain.device import fingerprint
from steam_sda.application.sessions import next_due_auth
from .widgets import label, button
from .dialogs import LoginDialog, ArchivePasswordDialog, ExportDialog, SettingsDialog
from .workers import LevelWorker, SwitchWorker, AuthWorker


class MainWindow(QMainWindow):
    def __init__(self, services, start_services=True):
        super().__init__()
        self.setWindowTitle("Steam SDA")
        self.resize(1020, 680)
        self.setMinimumSize(860, 570)
        self.services = services
        self.store = services.store
        self.api_key, self.levels, self.settings = "", {}, {}
        self.account_rows, self.selected_id = [], None
        self.native, self.switcher = services.native, services.switcher
        self.level_worker = self.switch_worker = None
        self.auth_worker = None
        self.vault = services.vault
        self.devices = services.devices
        self.auth = services.auth
        self.desktop_token = services.desktop_token
        self.accounts = services.accounts
        self.recovery = services.desktop.recovery
        self.managed = {}
        self._last_auth_attempt = 0
        self.generation = 0
        self._pending_levels = False
        self._pending_force = False
        self._closing = False
        self._active = None
        self._mtime = None
        self._start_services = start_services
        self._level_retry_at = 0
        startup_error = services.startup_error
        state = services.state
        self.settings, self.levels, self.api_key = state.settings, state.levels, state.api_key
        self._level_retry_at = state.retry_at
        self._build()
        self.reload_accounts(refresh_levels=False)
        if startup_error:
            self.message(startup_error, True)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.tick)
        if start_services:
            self.timer.start()
            QTimer.singleShot(0, self.refresh_levels)

    def _build(self):
        surface = QWidget()
        surface.setObjectName("surface")
        self.setCentralWidget(surface)
        root = QHBoxLayout(surface)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(250)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(17, 25, 17, 18)
        side.setSpacing(13)
        side.addWidget(label("Steam SDA", "brand"))
        self.count = label("Аккаунты Steam", "muted")
        side.addWidget(self.count)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Ник, логин или SteamID")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._list)
        side.addWidget(self.search)
        self.list = QListWidget()
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.currentItemChanged.connect(self._select)
        side.addWidget(self.list, 1)
        transfer = QHBoxLayout()
        self.import_button = button('Импорт', self.import_selected)
        self.export_button = button('Экспорт', self.export_selected)
        transfer.addWidget(self.import_button)
        transfer.addWidget(self.export_button)
        side.addLayout(transfer)
        self.reload_button = button("Обновить список", self.reload_accounts)
        self.open_button = button("Открыть Steam", self.open_steam)
        self.settings_button = button("Настройки", self.edit_settings)
        side.addWidget(self.reload_button)
        side.addWidget(self.open_button)
        side.addWidget(self.settings_button)
        root.addWidget(sidebar)
        content = QWidget()
        content.setObjectName("surface")
        detail = QVBoxLayout(content)
        detail.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        detail.setContentsMargins(30, 27, 30, 20)
        detail.setSpacing(18)
        top = QHBoxLayout()
        top.addWidget(label("АККАУНТ STEAM", "muted"))
        top.addStretch()
        self.current = label("", "status")
        top.addWidget(self.current)
        detail.addLayout(top)
        self.name = label("Выберите аккаунт", "name")
        self.name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail.addWidget(self.name)
        self.login = label("", "muted")
        detail.addWidget(self.login)
        self.banner = label("", "banner")
        self.banner.hide()
        detail.addWidget(self.banner)
        panel = QFrame()
        panel.setObjectName("panel")
        panel_box = QVBoxLayout(panel)
        panel_box.setContentsMargins(24, 20, 24, 21)
        panel_box.setSpacing(10)
        panel_top = QHBoxLayout()
        panel_top.addWidget(label("Уровень Steam", "headline"))
        panel_top.addStretch()
        self.refresh_button = button("Обновить уровни", lambda: self.refresh_levels(force=True))
        panel_top.addWidget(self.refresh_button)
        panel_box.addLayout(panel_top)
        self.level = label("—", "level")
        panel_box.addWidget(self.level)
        self.level_note = label("Уровень ещё не загружен", "muted")
        panel_box.addWidget(self.level_note)
        detail.addWidget(panel)
        session = QFrame()
        session.setObjectName("panel")
        session_box = QVBoxLayout(session)
        session_box.setContentsMargins(24, 22, 24, 22)
        session_box.setSpacing(13)
        session_box.addWidget(label("Вход в Steam", "headline"))
        self.session_note = label("", "muted")
        session_box.addWidget(self.session_note)
        self.switch_button = button("Переключить Steam", self.switch_selected, True)
        session_box.addWidget(self.switch_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        session_box.addWidget(self.progress)
        self.restore_button = button("Вернуть прежний вход", self.restore_previous)
        self.restore_button.hide()
        session_box.addWidget(self.restore_button)
        self.auth_note = label('', 'muted')
        session_box.addWidget(self.auth_note)
        auth_actions = QHBoxLayout()
        self.login_button = button('Настроить автовход', self.configure_login)
        self.renew_button = button('Обновить сессию', self.renew_selected)
        auth_actions.addWidget(self.login_button)
        auth_actions.addWidget(self.renew_button)
        session_box.addLayout(auth_actions)
        detail.addWidget(session)
        extras = QHBoxLayout()
        extras.addWidget(button("Профиль Steam ↗", self.open_profile))
        extras.addWidget(button("Копировать SteamID", self.copy_id))
        extras.addStretch()
        detail.addLayout(extras)
        detail.addStretch()
        detail.addWidget(label("Список берётся из аккаунтов, сохранённых самим Steam. "
                               "Чтобы добавить аккаунт, войдите в Steam с опцией «Запомнить меня» и обновите список.", "muted"))
        self.path_label = label("", "muted")
        detail.addWidget(self.path_label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

    def message(self, text, error=False):
        self.banner.setText(text)
        self.banner.setProperty("error", error)
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        self.banner.setVisible(bool(text))

    @property
    def selected(self):
        return next((a for a in self.account_rows if a["steam_id"] == self.selected_id), None)

    def reload_accounts(self, checked=False, refresh_levels=True):
        if self.switch_worker or self._closing:
            return
        self.generation += 1
        if self.level_worker:
            self.level_worker.requestInterruption()
        try:
            self.account_rows, self.managed = self.accounts.catalog()
            self.managed = self.accounts.bind_discovered(self.account_rows)
            self._active, self._mtime = self.services.desktop.status()
            self.path_label.setText("Steam · " + str(self.native.root) if self.native else "Укажите папку Steam в настройках")
        except (SteamError, OSError) as exc:
            self.message(str(exc) if isinstance(exc, SteamError) else "Не удалось прочитать список Steam.", True)
        self._list()
        if refresh_levels and self._start_services:
            self.refresh_levels()

    def _list(self):
        query = self.search.text().strip().casefold()
        rows = [a for a in self.account_rows if all(term in " ".join((a["name"], a["login"], a["steam_id"])).casefold() for term in query.split())]
        self.list.blockSignals(True)
        self.list.clear()
        selected = None
        for account in rows:
            level = self.levels.get(account["steam_id"], {}).get("level")
            text = account["name"] + (f"  ·  {level}" if level is not None else "")
            text += "\n" + account["login"] + (" · открыт" if self._active == account["steam_id"] else "")
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, account["steam_id"])
            item.setToolTip(account["steam_id"])
            self.list.addItem(item)
            if account["steam_id"] == self.selected_id:
                selected = item
        if selected is None and self.list.count():
            selected = next((self.list.item(i) for i in range(self.list.count()) if self.list.item(i).data(Qt.ItemDataRole.UserRole) == self._active), self.list.item(0))
        self.list.setCurrentItem(selected)
        self.selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.list.blockSignals(False)
        self.count.setText(f"Аккаунты · {len(rows)} / {len(self.account_rows)}" if query else f"Аккаунты · {len(rows)}")
        self.render()

    def _select(self, item, previous):
        self.selected_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.render()

    def render(self):
        account = self.selected
        self.name.setText(account["name"] if account else "Нет сохранённых аккаунтов" if not self.account_rows else "Ничего не найдено")
        self.login.setText(f"Логин: {account['login']}  ·  {account['steam_id']}" if account else "Откройте Steam и войдите в свой аккаунт.")
        data = self.levels.get(self.selected_id, {})
        self.level.setText(str(data["level"]) if "level" in data else "—")
        updated = data.get("updated_at")
        note = data.get("source", "")
        if type(updated) in (int, float) and 0 < updated < time.time() + 86400:
            stamp = datetime.fromtimestamp(updated)
            note += f" · обновлено {stamp:%d.%m} в {stamp:%H:%M}"
        if data.get("error"):
            note += ("\nСохранённый уровень · " if "level" in data else "") + data["error"]
        self.level_note.setText(note or "Уровень ещё не загружен")
        current = bool(account and self._active == self.selected_id)
        reapply_hwid = bool(current and self.devices and not self.devices.is_applied(self.selected_id))
        self.current.setText("Сейчас в Steam" if current else "")
        busy = self.switch_worker is not None or self.auth_worker is not None
        self.switch_button.setEnabled(bool(account) and (not current or reapply_hwid) and not busy)
        self.switch_button.setText("SteamKit обновляет сессию…" if self.auth_worker else "Переключаю…" if busy else "Перезапустить с HWID" if reapply_hwid else "Аккаунт уже открыт" if current else "Переключить Steam")
        self.session_note.setText("Steam уже работает с этим аккаунтом." if current else
            "Используется вход, который Steam сохранил на этом компьютере. Клиент будет перезапущен." if account and account.get("remembered") else
            "Steam может запросить вход и Steam Guard. Подтвердите их в окне самого Steam.")
        self.settings_button.setEnabled(not busy)
        self.reload_button.setEnabled(not busy)
        self.open_button.setEnabled(bool(self.native) and not busy)
        self.refresh_button.setEnabled(bool(self.account_rows) and self.level_worker is None and not self._closing
                                       and time.time() >= self._level_retry_at)
        remaining = max(0, int(self._level_retry_at - time.time()))
        self.refresh_button.setText(f"Повтор через {remaining // 60}:{remaining % 60:02d}" if remaining else "Обновить уровни")
        self.progress.setVisible(busy)
        managed = self.managed.get(self.selected_id, {})
        auth_note = 'Автовход через SteamKit: ' + ('включён' if managed.get('auto_restore') else 'выключен')
        hwid = managed.get('hwid')
        if hwid:
            auth_note += '\nHWID: ' + fingerprint(hwid)[:12] + ' · первая привязка ' + datetime.fromtimestamp(hwid['captured_at']).strftime('%d.%m.%Y %H:%M')
        else:
            auth_note += '\nHWID: ещё не сохранён'
        if managed.get('mafile'):
            auth_note += ' · maFile добавлен'
        expiry = claims(managed.get('session', {}).get('refresh_token', '')).get('exp')
        if type(expiry) is int and 0 < expiry < time.time() + 10 * 365 * 86400:
            auth_note += '\nКлиентский токен до ' + datetime.fromtimestamp(expiry).strftime('%d.%m.%Y %H:%M')
            if not managed.get('last_check'):
                auth_note += ' · ещё не проверен'
        if managed.get('last_check'):
            auth_note += '\nПроверено SteamKit: ' + datetime.fromtimestamp(managed['last_check']).strftime('%d.%m %H:%M')
        if managed.get('auth_error'):
            auth_note += '\n' + managed['auth_error']
        self.auth_note.setText(auth_note)
        self.login_button.setEnabled(bool(account) and not busy)
        self.renew_button.setEnabled(bool(managed) and not busy)
        self.import_button.setEnabled(not busy)
        self.export_button.setEnabled(bool(account) and not busy)
        try:
            pending = bool(self.switcher and self.switcher.has_recovery())
            self.restore_button.setVisible(pending)
            self.restore_button.setEnabled(not busy)
        except SteamError as exc:
            self.message(str(exc), True)

    def refresh_levels(self, checked=False, force=False):
        if self._closing or time.time() < self._level_retry_at:
            return
        if self.level_worker:
            self._pending_levels = True
            self._pending_force |= force
            return
        ids = []
        for a in self.account_rows:
            cache = self.levels.get(a["steam_id"], {})
            stamp = cache.get("updated_at", 0)
            if force or type(stamp) not in (int, float) or time.time() - stamp >= 3600 or "level" not in cache:
                ids.append(a["steam_id"])
        if not ids:
            return
        worker = LevelWorker(ids, self.api_key, self.generation, self, fetcher=self.services.fetch_level)
        self.level_worker = worker
        worker.result.connect(self._level_result)
        worker.finished.connect(self._levels_finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self.render()

    def _level_result(self, generation, steam_id, data):
        if generation != self.generation or self._closing or steam_id not in {a["steam_id"] for a in self.account_rows}:
            return
        if data.get("retry_after"):
            self._level_retry_at = time.time() + data["retry_after"]
            self._pending_levels = False
            self.message(data["error"], True)
            try:
                self.services.preferences.save_backoff(self._level_retry_at)
            except SteamError:
                pass
        if "level" in data:
            self.levels[steam_id] = data
        else:
            self.levels[steam_id] = {**self.levels.get(steam_id, {}), "error": data["error"]}
        try:
            self.services.preferences.save_levels(self.levels)
        except SteamError as exc:
            self.message(str(exc), True)
        self._list()

    def _levels_finished(self):
        self.level_worker = None
        self.render()
        if self._closing:
            QTimer.singleShot(0, self.close)
        elif self._pending_levels:
            force = self._pending_force
            self._pending_levels = False
            self._pending_force = False
            QTimer.singleShot(0, lambda: self.refresh_levels(force=force))

    def _begin_switch(self, steam_id=None):
        if not self.switcher or self.switch_worker or self.auth_worker:
            return
        worker = SwitchWorker(self.switcher, steam_id, self)
        self.switch_worker = worker
        worker.progress.connect(self.message)
        worker.result.connect(self._switch_result)
        worker.finished.connect(self._switch_finished)
        worker.finished.connect(worker.deleteLater)
        self.message("Подготавливаю переключение…")
        worker.start()
        self.render()

    def switch_selected(self):
        if self.selected_id:
            self._begin_switch(self.selected_id)

    def restore_previous(self):
        self._begin_switch()

    def _switch_result(self, result):
        if result.get("error"):
            self.message(result["error"], True)
            if result.get('requires_input') and result.get('steam_id') in self.managed:
                try:
                    self.accounts.record_auth_failure(result['steam_id'], result['error'], True)
                except SteamError as exc:
                    self.message(str(exc), True)
        elif result["state"] == "ready":
            self.message("Steam подтвердил вход в выбранный аккаунт.")
        elif result["state"] == "restored":
            self.message("Прежний выбор восстановлен. Steam открывается.")
        elif result['state'] == 'hwid_lost':
            self.message('Steam сменил процесс после запуска. Сохранённый HWID в новом процессе не подтверждён. Верните прежний вход и повторите переключение.', True)
        else:
            self.message("Steam открыт. Если он запросил пароль или Steam Guard, завершите вход в его окне.")

    def _switch_finished(self):
        self.switch_worker = None
        self.reload_accounts(refresh_levels=False)

    def tick(self):
        if self.switch_worker or self.auth_worker or not self.native or self._closing:
            return
        try:
            state = self.services.desktop.poll(self._active, self.managed)
            if state.switch_completed:
                self.message("Steam подтвердил вход в выбранный аккаунт.")
            if state.active_id != self._active or state.revision != self._mtime:
                self.reload_accounts(refresh_levels=False)
            else:
                self.render()
            if state.recover_sid:
                self._begin_switch(state.recover_sid)
                return
            self._auto_auth()
        except (SteamError, OSError) as exc:
            self.message(str(exc) if isinstance(exc, SteamError) else "Не удалось проверить состояние Steam.", True)

    def open_steam(self):
        try:
            if self.native and self.selected_id:
                if not self.services.desktop.focus_running_client():
                    self._begin_switch(self.selected_id)
        except SteamError as exc:
            self.message(str(exc), True)

    def edit_settings(self):
        dialog = SettingsDialog(str(self.native.root) if self.native else "", self.api_key, self.services.inspect_installation, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            dialog.deleteLater()
            return
        key = dialog.api_key.text().strip()
        try:
            if self.services.configure_steam(dialog.native, key):
                self._level_retry_at = 0
            self.api_key, self.native = key, self.services.native
            self.devices, self.switcher = self.services.devices, self.services.switcher
            self.accounts = self.services.accounts
            self.recovery = self.services.desktop.recovery
            self.message("Настройки сохранены.")
            self.reload_accounts(refresh_levels=False)
            self.refresh_levels(force=True)
        except SteamError as exc:
            self.message(str(exc), True)
        dialog.api_key.clear()
        dialog.deleteLater()

    def open_profile(self):
        if self.selected_id and valid_id(self.selected_id):
            webbrowser.open(f"https://steamcommunity.com/profiles/{self.selected_id}/")

    def copy_id(self):
        if self.selected_id:
            QApplication.clipboard().setText(self.selected_id)
            self.message("SteamID скопирован.")

    def import_selected(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Импорт аккаунта или maFile', '', 'Аккаунт Steam (*.zip *.maFile *.mafile)')
        if not path:
            return
        password = ''
        if Path(path).suffix.casefold() == '.zip':
            dialog = ArchivePasswordDialog(self)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            password = dialog.textValue()
            dialog.setTextValue('')
            dialog.deleteLater()
            if not accepted:
                return
        try:
            merged = self.accounts.import_file(path, password, self.account_rows)
            self.selected_id = merged['steam_id']
            self.reload_accounts()
            self.message('Аккаунт импортирован. Исходный файл сохранён.')
            if merged.get('auto_restore'):
                self._begin_auth(merged['steam_id'])
        except SteamError as exc:
            self.message(str(exc), True)

    def export_selected(self):
        if not self.selected:
            return
        try:
            row = self.accounts.prepare_export(self.selected)
        except SteamError as exc:
            self.message(str(exc), True)
            return
        dialog = ExportDialog(bool(row.get('mafile') or row.get('session')), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            path, _ = QFileDialog.getSaveFileName(self, 'Экспорт аккаунта', 'steam-account.zip', 'ZIP (*.zip)')
            if path:
                try:
                    self.accounts.export_file(path, row, dialog.password.text(), dialog.secrets.isChecked())
                    self.message('Архив аккаунта сохранён.')
                except SteamError as exc:
                    self.message(str(exc), True)
        dialog.password.clear()
        dialog.confirm.clear()
        dialog.deleteLater()

    def configure_login(self):
        if not self.selected or self.auth_worker or self.switch_worker:
            return
        try:
            row = {**self.managed.get(self.selected_id, {}), **metadata(self.selected)}
        except SteamError as exc:
            self.message(str(exc), True)
            return
        dialog = LoginDialog(row, self)
        password = None
        if dialog.exec() == QDialog.DialogCode.Accepted:
            password = dialog.password.text() or None
            try:
                row = self.accounts.configure_login(self.selected, dialog.auto.isChecked(), password,
                    remember=dialog.remember.isChecked(), clear_password=dialog.clear_password.isChecked())
                self.managed = self.accounts.managed()
                self.render()
                if row['auto_restore'] or password:
                    self._begin_auth(row['steam_id'], password)
                else:
                    self.message('Автовход выключен. Настройки сохранены.')
            except SteamError as exc:
                self.message(str(exc), True)
        dialog.password.clear()
        dialog.deleteLater()

    def renew_selected(self):
        if self.selected_id:
            self._begin_auth(self.selected_id)

    def _begin_auth(self, sid, password=None, automatic=False):
        if self.auth_worker or self.switch_worker or self._closing:
            return
        if not self.services.desktop.can_authenticate():
            if not automatic:
                self.message('Закройте игры Steam перед обновлением сессии.', True)
            return
        self._last_auth_attempt = time.time()
        worker = AuthWorker(self.auth, sid, password, self)
        self.auth_worker = worker
        worker.result.connect(self._auth_result)
        worker.finished.connect(self._auth_finished)
        worker.finished.connect(worker.deleteLater)
        if not automatic:
            self.message('SteamKit проверяет и обновляет сессию…')
        worker.start()
        self.render()

    def _auth_result(self, result):
        try:
            if result.get('error'):
                self.accounts.record_auth_failure(result['steam_id'], result['error'], result.get('requires_input', False))
                self.message(result['error'], True)
            else:
                self.message('SteamKit подтвердил аккаунт и обновил сессию.')
            self.managed = self.accounts.managed()
        except SteamError as exc:
            self.message(str(exc), True)

    def _auth_finished(self):
        self.auth_worker = None
        self.render()

    def _auto_auth(self):
        if self._start_services:
            sid = next_due_auth(self.managed, self._last_auth_attempt)
            if sid:
                self._begin_auth(sid, automatic=True)

    def closeEvent(self, event):
        if self.switch_worker or self.auth_worker:
            self.message("Дождитесь завершения операции Steam, затем закройте приложение.")
            event.ignore()
            return
        if self.level_worker:
            self._closing = True
            self.level_worker.requestInterruption()
            self.message("Завершаю текущий запрос Steam…")
            event.ignore()
            return
        self.timer.stop()
        event.accept()
