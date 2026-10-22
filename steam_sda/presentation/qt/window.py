"""Qt view: widgets, user input and dispatch to injected application services."""
from datetime import datetime
from pathlib import Path
import time
import webbrowser
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QHBoxLayout, QMainWindow,
    QWidget, QFrame, QListWidget, QListWidgetItem, QLineEdit, QProgressBar, QScrollArea,
    QLayout, QFileDialog, QButtonGroup, QSizePolicy)
from steam_sda.domain.errors import SteamError
from steam_sda.domain.accounts import claims, metadata, session_status
from steam_sda.domain.identity import valid_id
from steam_sda.domain.device import fingerprint
from steam_sda.application.sessions import next_due_auth
from .widgets import label, button, badge, icon_tile, Backdrop, AnimatedLineEdit
from .icons import icon
from .motion import TransitionStack
from .account_list import AnimatedAccountList, avatar_colors
from PyQt6.QtGui import QShortcut, QKeySequence
from steam_sda import __version__
from .dialogs import LoginDialog, ArchivePasswordDialog, ExportDialog, SettingsDialog, DeleteAccountDialog
from .workers import LevelWorker, SwitchWorker, AuthWorker, UpdateWorker
from .update_dialog import UpdateDialog


class MainWindow(QMainWindow):
    def __init__(self, services, start_services=True):
        super().__init__()
        self.setWindowTitle("Steam SDA")
        self.resize(1180, 840)
        self.setMinimumSize(860, 620)
        self.services = services
        self.store = services.store
        self.api_key, self.levels, self.settings = "", {}, {}
        self.account_rows, self.selected_id = [], None
        self.native, self.switcher = services.native, services.switcher
        self.level_worker = self.switch_worker = None
        self.auth_worker = None
        self.update_worker = None
        self.available_update = None
        self._update_result = {}
        self._update_dialog_open = False
        self._update_to_install = None
        self._next_update_check = 0
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
            QTimer.singleShot(3000, self.check_updates)

    def _build(self):
        surface = Backdrop()
        self.setCentralWidget(surface)
        root = QHBoxLayout(surface)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(282)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 24, 18, 18)
        side.setSpacing(12)
        brand = QHBoxLayout()
        brand.addWidget(icon_tile('switch', size=40))
        brand.addWidget(label('Steam SDA', 'brand'))
        brand.addStretch()
        brand.addWidget(badge(__version__))
        side.addLayout(brand)
        side.addWidget(label('Все ваши входы в одном месте', 'muted'))
        side.addSpacing(18)
        self.count = label('Аккаунты', 'eyebrow')
        side.addWidget(self.count)
        self.search = AnimatedLineEdit()
        self.search.setPlaceholderText('Найти аккаунт')
        self.search.setToolTip('Поиск по имени, логину и SteamID · Ctrl+K')
        self.search.addAction(icon('search'), QLineEdit.ActionPosition.LeadingPosition)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._list)
        side.addWidget(self.search)
        self.list = AnimatedAccountList()
        self.list.setObjectName('accounts')
        self.list.setMouseTracking(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.currentItemChanged.connect(self._select)
        side.addWidget(self.list, 1)
        self.reload_button = button('Обновить список', self.reload_accounts, ghost=True)
        self.open_button = button('Открыть Steam', self.open_steam, ghost=True)
        self.settings_button = button('Настройки', self.edit_settings, ghost=True)
        self.updates_button = button('Обновления', self.show_updates, icon_name='refresh', ghost=True)
        for control in (self.reload_button, self.open_button, self.settings_button, self.updates_button):
            side.addWidget(control)
        side.addWidget(label('Данные хранятся на этом ПК', 'eyebrow'))
        root.addWidget(sidebar)
        content = QWidget()
        content.setObjectName('content')
        detail = QVBoxLayout(content)
        detail.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        detail.setContentsMargins(26, 24, 26, 20)
        detail.setSpacing(14)
        toolbar = QHBoxLayout()
        workspace = QVBoxLayout()
        workspace.setSpacing(3)
        workspace.addWidget(label('ЛИЧНОЕ ПРОСТРАНСТВО', 'eyebrow'))
        workspace.addWidget(label('Мои аккаунты', 'workspaceTitle'))
        toolbar.addLayout(workspace, 1)
        self.import_button = button('Импорт базы', self.import_selected, icon_name='import', ghost=True)
        self.export_button = button('Экспорт базы', self.export_selected, icon_name='export', ghost=True)
        toolbar.addWidget(self.import_button)
        toolbar.addWidget(self.export_button)
        detail.addLayout(toolbar)
        identity_panel = QFrame()
        identity_panel.setObjectName('identity')
        top = QHBoxLayout(identity_panel)
        top.setContentsMargins(22, 18, 22, 18)
        top.setSpacing(16)
        self.avatar = label('', 'avatar')
        self.avatar.setFixedSize(66, 66)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.avatar)
        identity = QVBoxLayout()
        identity.setSpacing(4)
        identity.addWidget(label('ВЫБРАННЫЙ АККАУНТ', 'eyebrow'))
        self.name = label('Выберите аккаунт', 'name')
        self.name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        identity.addWidget(self.name)
        self.login = label('', 'muted')
        self.login.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        identity.addWidget(self.login)
        top.addLayout(identity, 1)
        self.current = badge('')
        top.addWidget(self.current, 0, Qt.AlignmentFlag.AlignTop)
        detail.addWidget(identity_panel)
        nav = QHBoxLayout()
        self.tabs = QButtonGroup(self)
        self.overview_tab = button('Вход и обзор', lambda: self.pages.go(0), icon_name='grid', ghost=True)
        self.details_tab = button('Сессия и данные', lambda: self.pages.go(1), icon_name='shield', ghost=True)
        for tab in (self.overview_tab, self.details_tab):
            tab.setCheckable(True)
            self.tabs.addButton(tab)
            nav.addWidget(tab)
        self.overview_tab.setChecked(True)
        nav.addStretch()
        detail.addLayout(nav)
        self.banner = label('', 'banner')
        self.banner.hide()
        detail.addWidget(self.banner)
        self.pages = TransitionStack()
        detail.addWidget(self.pages)
        overview = QWidget()
        overview_box = QVBoxLayout(overview)
        overview_box.setContentsMargins(0, 0, 0, 0)
        overview_box.setSpacing(16)
        self.pages.addWidget(overview)

        def card(title, glyph, parent_layout, role=None):
            panel = QFrame()
            panel.setObjectName('panel')
            panel.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Preferred if isinstance(parent_layout, QHBoxLayout) else QSizePolicy.Policy.Maximum)
            if role:
                panel.setProperty('role', role)
            box = QVBoxLayout(panel)
            box.setContentsMargins(20, 18, 20, 18)
            box.setSpacing(10)
            heading = QHBoxLayout()
            heading.addWidget(icon_tile(glyph, size=30))
            heading.addWidget(label(title, 'headline'), 1)
            box.addLayout(heading)
            parent_layout.addWidget(panel)
            return box

        stats = QHBoxLayout()
        stats.setSpacing(16)
        level_box = card('Уровень Steam', 'spark', stats)
        self.level = label('—', 'level')
        level_box.addWidget(self.level)
        self.level_note = label('Уровень ещё не загружен', 'muted')
        level_box.addWidget(self.level_note)
        self.refresh_button = button('Обновить уровни', lambda: self.refresh_levels(force=True), ghost=True)
        level_box.addWidget(self.refresh_button)
        device_box = card('Сохранённое устройство', 'monitor', stats)
        self.hwid_value = label('Ещё не сохранено', 'deviceValue')
        device_box.addWidget(self.hwid_value)
        self.hwid_note = label('', 'muted')
        device_box.addWidget(self.hwid_note, 1)
        self.auto_badge = badge('Автовход выключен')
        self.mafile_badge = badge('Без maFile')
        flags = QHBoxLayout()
        flags.addWidget(self.auto_badge)
        flags.addWidget(self.mafile_badge)
        flags.addStretch()
        device_box.addLayout(flags)
        session_box = card('Открыть аккаунт', 'key', overview_box, role='session')
        session_state_row = QHBoxLayout()
        self.session_state = badge('')
        self.session_state.setWordWrap(True)
        session_state_row.addWidget(self.session_state)
        session_state_row.addStretch()
        self.save_session_button = button('Сохранить сессию', self.save_selected_session, icon_name='lock', ghost=True)
        session_state_row.addWidget(self.save_session_button)
        session_box.addLayout(session_state_row)
        self.session_note = label('', 'muted')
        session_box.addWidget(self.session_note)
        self.switch_button = button('Переключить Steam', self.switch_selected, True)
        session_box.addWidget(self.switch_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        session_box.addWidget(self.progress)
        self.restore_button = button('Вернуть прежний вход', self.restore_previous)
        self.restore_button.hide()
        session_box.addWidget(self.restore_button)
        auth_actions = QHBoxLayout()
        self.login_button = button('Настроить автовход', self.configure_login)
        self.renew_button = button('Обновить сессию', self.renew_selected)
        auth_actions.addWidget(self.login_button)
        auth_actions.addWidget(self.renew_button)
        session_box.addLayout(auth_actions)
        overview_box.addLayout(stats)
        activity_box = card('История действий', 'activity', overview_box)
        self.activity_list = QListWidget()
        self.activity_list.setObjectName('activity')
        self.activity_list.setFixedHeight(68)
        self.activity_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.activity_list.addItem('Готово · выберите аккаунт и откройте Steam')
        activity_box.addWidget(self.activity_list)
        overview_box.addStretch()
        details = QWidget()
        details_box = QVBoxLayout(details)
        details_box.setContentsMargins(0, 0, 0, 0)
        details_box.setSpacing(16)
        self.pages.addWidget(details)
        data_box = card('Сессия и привязка', 'shield', details_box)
        self.auth_note = label('', 'body')
        self.auth_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        data_box.addWidget(self.auth_note)
        extras = QHBoxLayout()
        self.profile_button = button('Профиль Steam', self.open_profile)
        self.copy_button = button('Копировать SteamID', self.copy_id)
        extras.addWidget(self.profile_button)
        extras.addWidget(self.copy_button)
        data_box.addLayout(extras)
        help_box = card('Добавление и перенос', 'folder', details_box)
        help_box.addWidget(label('Войдите в Steam и обновите список. Для разового входа доступна кнопка «Сохранить сессию». '
                                '«Импорт базы» принимает общий ZIP, архив одного аккаунта или maFile. '
                                '«Экспорт базы» сохраняет все аккаунты, сессии и HWID одним архивом.', 'muted'))
        removal_box = card('Управление записью', 'user', details_box)
        removal_box.addWidget(label('Удаление очищает данные аккаунта в Steam SDA. Повторное добавление — через импорт.', 'muted'))
        self.delete_button = button('Удалить аккаунт', self.delete_selected)
        self.delete_button.setProperty('danger', True)
        removal_box.addWidget(self.delete_button)
        details_box.addStretch()
        detail.addStretch()
        self.path_label = label('', 'muted')
        detail.addWidget(self.path_label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        for sequence, callback in (('Ctrl+K', self.search.setFocus), ('Ctrl+I', self.import_button.click),
                                   ('Ctrl+E', self.export_button.click), ('Ctrl+R', self.reload_button.click)):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(callback)
        self.import_button.setToolTip('Импорт всей базы из ZIP или отдельного maFile · Ctrl+I')
        self.export_button.setToolTip('Все аккаунты, сессии и HWID одним архивом · Ctrl+E')

    def message(self, text, error=False):
        if text:
            item = QListWidgetItem(datetime.now().strftime('%H:%M') + '  ·  ' + text.replace('\n', ' '))
            item.setToolTip(text)
            self.activity_list.insertItem(0, item)
            while self.activity_list.count() > 20:
                self.activity_list.takeItem(self.activity_list.count() - 1)
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
            state, portable, temporary = session_status(account, self.managed.get(account['steam_id'], {}), account['steam_id'] == self._active)
            account.update(session_label=state, session_saved=portable, temporary_session=temporary)
            level = self.levels.get(account["steam_id"], {}).get("level")
            text = account["name"] + (f"  ·  {level}" if level is not None else "")
            text += "\n" + account["login"] + (" · открыт" if self._active == account["steam_id"] else "")
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, account["steam_id"])
            item.setData(Qt.ItemDataRole.UserRole + 1, {**account, 'active': self._active == account['steam_id'], 'level': level})
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
        self.list.reveal()
        self.render()

    def _select(self, item, previous):
        self.pages.reveal()
        self.selected_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.render()

    def render(self):
        account = self.selected
        self.name.setText(account["name"] if account else "Нет сохранённых аккаунтов" if not self.account_rows else "Ничего не найдено")
        self.login.setText(f"{account['login']}  ·  {account['steam_id']}" if account else
                           'Добавьте базу через «Импорт базы» или войдите в Steam и обновите список.' if not self.account_rows else
                           'Попробуйте другое имя, логин или SteamID.')
        self.avatar.setText((account['name'][:2].upper() if account else '—'))
        bg, fg = avatar_colors(self.selected_id or '')
        self.avatar.setStyleSheet(f'background: {bg}; color: {fg}; border-radius: 20px; font-size: 24px; font-weight: 600;')
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
        self.current.setText("● Сейчас в Steam" if current else "Не открыт")
        self.current.setProperty('tone', 'success' if current else 'neutral')
        self.current.style().unpolish(self.current)
        self.current.style().polish(self.current)
        busy = self.switch_worker is not None or self.auth_worker is not None
        self.switch_button.setEnabled(bool(account) and (not current or reapply_hwid) and not busy)
        self.switch_button.setText("SteamKit обновляет сессию…" if self.auth_worker else "Переключаю…" if busy else "Перезапустить с HWID" if reapply_hwid else "Аккаунт уже открыт" if current else "Переключить Steam")
        self.session_note.setText('Выберите аккаунт слева. Для переноса всех аккаунтов используйте кнопки вверху.' if not account else
            "Steam уже работает с этим аккаунтом." if current else
            "Используется вход, который Steam сохранил на этом компьютере. Клиент будет перезапущен." if account and account.get("remembered") else
            "Steam может запросить вход и Steam Guard. Подтвердите их в окне самого Steam.")
        self.settings_button.setEnabled(not busy)
        self.updates_button.setEnabled(not busy and self.update_worker is None)
        self.reload_button.setEnabled(not busy)
        self.open_button.setEnabled(bool(self.native) and not busy)
        self.refresh_button.setEnabled(bool(self.account_rows) and self.level_worker is None and not self._closing
                                       and time.time() >= self._level_retry_at)
        remaining = max(0, int(self._level_retry_at - time.time()))
        self.refresh_button.setText(f"Повтор через {remaining // 60}:{remaining % 60:02d}" if remaining else "Обновить уровни")
        self.progress.setVisible(busy)
        managed = self.managed.get(self.selected_id, {})
        if account:
            session_label, portable, temporary = session_status(account, managed, current)
        else:
            session_label, portable, temporary = 'Нет выбранного аккаунта', False, False
        self.session_state.setText(session_label)
        self.session_state.setProperty('tone', 'success' if portable else 'neutral')
        self.session_state.style().unpolish(self.session_state)
        self.session_state.style().polish(self.session_state)
        self.save_session_button.setEnabled(bool(account) and not busy)
        self.save_session_button.setToolTip('Сохранить доступную клиентскую сессию в базе, в том числе без «Запомнить меня» в Steam.')
        auth_note = 'Автовход через SteamKit: ' + ('включён' if managed.get('auto_restore') else 'выключен')
        auth_note += '\n' + session_label
        hwid = managed.get('hwid')
        self.hwid_value.setText(fingerprint(hwid)[:12] if hwid else 'Ещё не сохранено')
        self.hwid_note.setText('Первая привязка · ' + datetime.fromtimestamp(hwid['captured_at']).strftime('%d.%m.%Y %H:%M')
                               if hwid else 'Профиль устройства сохраняется при первой привязке аккаунта.')
        self.auto_badge.setText('Автовход включён' if managed.get('auto_restore') else 'Автовход выключен')
        self.mafile_badge.setText('maFile добавлен' if managed.get('mafile') else 'Без maFile')
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
        self.export_button.setEnabled(bool(self.account_rows) and not busy)
        self.delete_button.setEnabled(bool(account) and not busy)
        self.profile_button.setEnabled(bool(account))
        self.copy_button.setEnabled(bool(account))
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

    def save_selected_session(self):
        if not self.selected or self.auth_worker or self.switch_worker:
            return
        try:
            self.accounts.save_session(self.selected)
            self.reload_accounts(refresh_levels=False)
            self.message('Сессия сохранена в защищённой базе. Теперь она включается в экспорт всей базы.')
        except SteamError as exc:
            self.message(str(exc), True)

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
        if self._start_services:
            self.check_updates()
        if self.switch_worker or self.auth_worker or not self.native or self._closing or self._update_dialog_open:
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

    def check_updates(self):
        if (self._closing or self._update_dialog_open or self.update_worker
                or not self.services.updates or time.time() < self._next_update_check):
            return
        self._next_update_check = time.time() + 3600
        self._update_result = {}
        self.update_worker = UpdateWorker(self.services.updates, parent=self, force=False)
        self.update_worker.result.connect(self._receive_update)
        self.update_worker.finished.connect(self._update_checked)
        self.updates_button.setEnabled(False)
        self.update_worker.start()

    def _receive_update(self, result):
        self._update_result = result

    def _update_checked(self):
        worker, self.update_worker = self.update_worker, None
        worker.deleteLater()
        if self._closing:
            QTimer.singleShot(0, self.close)
            return
        release = self._update_result.get('release')
        if release:
            self.available_update = release
            self.updates_button.setText(f'Доступна {release.version}')
            self.message(f'Доступна Steam SDA {release.version}. Откройте «Обновления», чтобы установить.')
        self.render()

    def show_updates(self):
        if self.auth_worker or self.switch_worker or self.update_worker or self._closing or not self.services.updates:
            return
        self._update_dialog_open = True
        try:
            dialog = UpdateDialog(self.services.updates, self.available_update, self)
        except SteamError as exc:
            self._update_dialog_open = False
            self.message(str(exc), True)
            return
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        self.available_update = dialog.release
        self._update_dialog_open = False
        if accepted and dialog.installer_path:
            self._update_to_install = (dialog.release, dialog.installer_path)
            self._closing = True
            self.close()
        dialog.deleteLater()

    def open_profile(self):
        if self.selected_id and valid_id(self.selected_id):
            webbrowser.open(f"https://steamcommunity.com/profiles/{self.selected_id}/")

    def copy_id(self):
        if self.selected_id:
            QApplication.clipboard().setText(self.selected_id)
            self.message("SteamID скопирован.")

    def import_selected(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Импорт базы аккаунтов или maFile', '', 'База Steam / maFile (*.zip *.maFile *.mafile)')
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
            merged = self.accounts.import_all_file(path, password, self.account_rows)
            self.selected_id = merged[0]['steam_id']
            self.reload_accounts()
            self.message(f'Импорт завершён. Аккаунтов: {len(merged)}. Исходный файл сохранён.')
            if Path(path).suffix.casefold() == '.mafile' and merged[0].get('auto_restore'):
                self._begin_auth(merged[0]['steam_id'])
        except SteamError as exc:
            self.message(str(exc), True)

    def export_selected(self):
        if not self.account_rows:
            return
        try:
            rows = self.accounts.prepare_all_export()
        except SteamError as exc:
            self.message(str(exc), True)
            return
        dialog = ExportDialog(any(row.get('mafile') or row.get('session') for row in rows), self, account_count=len(rows))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            path, _ = QFileDialog.getSaveFileName(self, 'Экспорт всей базы аккаунтов', 'steam-accounts.zip', 'ZIP (*.zip)')
            if path:
                try:
                    self.accounts.export_all_file(path, rows, dialog.password.text(), dialog.secrets.isChecked())
                    self.message(f'База сохранена одним архивом. Аккаунтов: {len(rows)}.')
                except SteamError as exc:
                    self.message(str(exc), True)
        dialog.password.clear()
        dialog.confirm.clear()
        dialog.deleteLater()

    def delete_selected(self):
        if not self.selected or self.auth_worker or self.switch_worker or self._closing:
            return
        account = dict(self.selected)
        dialog = DeleteAccountDialog(account, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        dialog.deleteLater()
        if not accepted:
            return
        try:
            self.accounts.delete(account['steam_id'])
            self.levels.pop(account['steam_id'], None)
            self.services.preferences.save_levels(self.levels)
            if self.recovery.pending_sid == account['steam_id']:
                self.recovery.pending_sid = None
            self.reload_accounts(refresh_levels=False)
            self.message('Аккаунт «' + account['name'] + '» удалён из базы. Вернуть его можно через импорт.')
        except (SteamError, OSError):
            self.reload_accounts(refresh_levels=False)
            self.message('Не удалось полностью удалить запись. Проверьте доступ к локальному хранилищу.', True)

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
        if self._start_services and not self._update_dialog_open and not self._closing:
            sid = next_due_auth(self.managed, self._last_auth_attempt)
            if sid:
                self._begin_auth(sid, automatic=True)

    def closeEvent(self, event):
        if self.switch_worker or self.auth_worker:
            self.message("Дождитесь завершения операции Steam, затем закройте приложение.")
            event.ignore()
            return
        if self.update_worker:
            self._closing = True
            self.update_worker.requestInterruption()
            event.ignore()
            return
        if self.level_worker:
            self._closing = True
            self.level_worker.requestInterruption()
            self.message("Завершаю текущий запрос Steam…")
            event.ignore()
            return
        self.timer.stop()
        if self._update_to_install:
            try:
                self.services.updates.install(*self._update_to_install)
            except SteamError as exc:
                self._closing = False
                self._update_to_install = None
                if self._start_services:
                    self.timer.start()
                self.message(str(exc), True)
                event.ignore()
                return
            self._update_to_install = None
        event.accept()
