"""A small Steam account dashboard; all network and switching work is threaded."""

from datetime import datetime
from pathlib import Path
import time
import webbrowser

from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
                             QMainWindow, QWidget, QFrame, QListWidget, QListWidgetItem, QLineEdit,
                             QPushButton, QFileDialog, QProgressBar, QScrollArea, QLayout, QCheckBox, QInputDialog)

from .errors import SteamError, RateLimitError
from .profile import fetch_level
from .steam import NativeSteam, Switcher
from .storage import Store
from .vdf import valid_id
from .accounts import AccountVault, claims, metadata, token_valid, import_account, export_account
from .steamkit import SteamKitAuth, DesktopToken, AuthError


def label(text="", name=""):
    item = QLabel(text)
    item.setObjectName(name)
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setWordWrap(True)
    return item


def button(text, callback, primary=False):
    item = QPushButton(text)
    if primary:
        item.setObjectName("primary")
    item.clicked.connect(callback)
    return item


class LevelWorker(QThread):
    result = pyqtSignal(int, str, dict)

    def __init__(self, ids, api_key, generation, parent=None, fetcher=fetch_level):
        super().__init__(parent)
        self.ids, self.api_key, self.generation, self.fetcher = ids, api_key, generation, fetcher

    def run(self):
        for i, steam_id in enumerate(self.ids):
            if self.isInterruptionRequested():
                return
            if i:
                self.msleep(800)
            try:
                data = self.fetcher(steam_id, self.api_key)
            except RateLimitError as exc:
                self.result.emit(self.generation, steam_id, {"error": str(exc), "retry_after": exc.seconds})
                return
            except SteamError as exc:
                data = {"error": str(exc)}
            except Exception:
                data = {"error": "Не удалось обработать уровень Steam. Повторите обновление."}
            self.result.emit(self.generation, steam_id, data)


class SwitchWorker(QThread):
    progress = pyqtSignal(str)
    result = pyqtSignal(dict)

    def __init__(self, switcher, steam_id=None, parent=None):
        super().__init__(parent)
        self.switcher, self.steam_id = switcher, steam_id

    def run(self):
        try:
            result = self.switcher.switch(self.steam_id, self.progress.emit) if self.steam_id else self.switcher.restore(self.progress.emit)
        except SteamError as exc:
            result = {"error": str(exc), 'steam_id': self.steam_id, 'requires_input': getattr(exc, 'requires_input', False)}
        except Exception:
            result = {"error": "Не удалось завершить переключение. Проверьте Steam; резервная копия сохранена, если выбор уже менялся."}
        self.result.emit(result)


class AuthWorker(QThread):
    result = pyqtSignal(dict)

    def __init__(self, auth, sid, password=None, parent=None):
        super().__init__(parent)
        self.auth, self.sid, self.password = auth, sid, password

    def run(self):
        try:
            self.auth.authenticate(self.sid, password_override=self.password)
            self.result.emit({'steam_id': self.sid, 'ok': True})
        except SteamError as exc:
            self.result.emit({'steam_id': self.sid, 'error': str(exc), 'requires_input': getattr(exc, 'requires_input', False)})
        except Exception:
            self.result.emit({'steam_id': self.sid, 'error': 'Не удалось обновить сессию SteamKit.'})
        finally:
            self.password = None


class LoginDialog(QDialog):
    def __init__(self, row, parent):
        super().__init__(parent)
        self.setWindowTitle('Автовход SteamKit')
        self.setMinimumWidth(510)
        box = QVBoxLayout(self)
        box.setContentsMargins(24, 24, 24, 24)
        box.setSpacing(14)
        box.addWidget(label(row['name'], 'headline'))
        box.addWidget(label(row['login'], 'muted'))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setMaxLength(1024)
        self.password.setPlaceholderText('Пароль Steam — при необходимости')
        box.addWidget(self.password)
        self.remember = QCheckBox('Сохранить новый пароль с защитой Windows')
        box.addWidget(self.remember)
        self.clear_password = QCheckBox('Удалить ранее сохранённый пароль')
        self.clear_password.setVisible(bool(row.get('password')))
        box.addWidget(self.clear_password)
        self.auto = QCheckBox('Восстанавливать вход и обновлять сессию автоматически')
        self.auto.setChecked(row.get('auto_restore', True))
        box.addWidget(self.auto)
        box.addWidget(label('maFile даёт код Steam Guard. Для повторного входа также нужен действующий токен или пароль. '
                            'Без сохранения пароль используется только для этого входа.', 'muted'))
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button('Отмена', self.reject))
        actions.addWidget(button('Сохранить и войти', self.accept, True))
        box.addLayout(actions)


class ExportDialog(QDialog):
    def __init__(self, has_secrets, parent):
        super().__init__(parent)
        self.setWindowTitle('Экспорт аккаунта')
        self.setMinimumWidth(460)
        box = QVBoxLayout(self)
        box.setContentsMargins(24, 24, 24, 24)
        box.setSpacing(14)
        self.secrets = QCheckBox('Включить maFile и токены входа')
        self.secrets.setEnabled(has_secrets)
        self.secrets.setChecked(has_secrets)
        box.addWidget(self.secrets)
        box.addWidget(label('Пароль Steam в архив не включается. Архив с maFile и токенами позволяет войти в аккаунт.', 'muted'))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText('Пароль архива · от 8 символов для данных входа')
        box.addWidget(self.password)
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm.setPlaceholderText('Повторите пароль архива')
        box.addWidget(self.confirm)
        self.error = label('', 'status')
        box.addWidget(self.error)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button('Отмена', self.reject))
        actions.addWidget(button('Экспортировать ZIP', self._accept, True))
        box.addLayout(actions)

    def _accept(self):
        if self.password.text() != self.confirm.text():
            self.error.setText('Пароли не совпадают.')
        elif self.secrets.isChecked() and len(self.password.text()) < 8:
            self.error.setText('Для данных входа нужен пароль архива от 8 символов.')
        else:
            self.accept()


class SettingsDialog(QDialog):
    def __init__(self, steam_path, api_key, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки Steam SDA")
        self.setMinimumWidth(520)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(15)
        root.addWidget(label("Настройки", "headline"))
        form = QFormLayout()
        form.setVerticalSpacing(12)
        path_row = QHBoxLayout()
        self.path = QLineEdit(steam_path)
        path_row.addWidget(self.path, 1)
        browse = button("Выбрать…", self._browse)
        path_row.addWidget(browse)
        form.addRow("Папка Steam", path_row)
        self.api_key = QLineEdit(api_key)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setMaxLength(256)
        self.api_key.setPlaceholderText("Необязательно")
        form.addRow("Steam Web API key", self.api_key)
        root.addLayout(form)
        root.addWidget(label("Без ключа уровень загружается с публичной страницы Steam. "
                             "Если Steam скрывает его, можно использовать собственный API-ключ. "
                             "Ключ хранится с защитой текущего пользователя Windows.", "muted"))
        key_link = button("Открыть страницу API-ключа Steam", lambda: webbrowser.open("https://steamcommunity.com/dev/apikey"))
        key_link.setObjectName("link")
        root.addWidget(key_link)
        self.error = label("", "status")
        self.error.setProperty("error", True)
        root.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(button("Отмена", self.reject))
        row.addWidget(button("Сохранить", self._save, True))
        root.addLayout(row)
        self.native = None

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Папка со steam.exe", self.path.text())
        if path:
            self.path.setText(path)

    def _save(self):
        try:
            self.native = NativeSteam(self.path.text().strip())
            self.native.account_list()
        except SteamError as exc:
            self.error.setText(str(exc))
            return
        key = self.api_key.text().strip()
        if key and (len(key) != 32 or any(c not in "0123456789abcdefABCDEF" for c in key)):
            self.error.setText("Введите API-ключ из 32 шестнадцатеричных символов или оставьте поле пустым.")
            return
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, native=None, store=None, start_services=True):
        super().__init__()
        self.setWindowTitle("Steam SDA")
        self.resize(1020, 680)
        self.setMinimumSize(860, 570)
        self.store = store or Store()
        self.api_key, self.levels, self.settings = "", {}, {}
        self.account_rows, self.selected_id = [], None
        self.native, self.switcher = native, None
        self.level_worker = self.switch_worker = None
        self.auth_worker = None
        self.vault = AccountVault(self.store)
        self.auth = SteamKitAuth(self.vault)
        self.desktop_token = DesktopToken()
        self.managed = {}
        self._recover_account = None
        self._recover_at = 0
        self._last_auth_attempt = 0
        self.generation = 0
        self._pending_levels = False
        self._pending_force = False
        self._closing = False
        self._active = None
        self._mtime = None
        self._start_services = start_services
        self._level_retry_at = 0
        startup_error = ""
        try:
            self.settings = self.store.read("settings.json")
            if self.settings.get("steam_path") is not None and not isinstance(self.settings["steam_path"], str):
                raise SteamError("Некорректная папка Steam в настройках. Выберите её заново.")
            cached = self.store.read("levels.json")
            for sid, value in cached.items():
                if valid_id(sid) and isinstance(value, dict) and type(value.get("level")) is int and 0 <= value["level"] <= 999999:
                    self.levels[sid] = {k: v for k, v in value.items() if k in ("level", "updated_at", "source", "error")}
                    for field in ("source", "error"):
                        if field in self.levels[sid] and not isinstance(self.levels[sid][field], str):
                            self.levels[sid].pop(field)
            backoff = self.store.read("request-backoff.json").get("until", 0)
            if type(backoff) in (int, float) and time.time() < backoff <= time.time() + 3600:
                self._level_retry_at = backoff
            key = self.store.read("api-key.dpapi", encrypted=True).get("key", "")
            self.api_key = key if isinstance(key, str) else ""
            self.native = self.native or NativeSteam(self.settings.get("steam_path"))
            self.managed = self.vault.all()
            self.switcher = Switcher(self.native, self.store, auth=self.auth, vault=self.vault, token_bridge=self.desktop_token)
        except SteamError as exc:
            startup_error = str(exc)
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
            self.account_rows = self.native.account_list() if self.native else []
            self.managed = self.vault.all()
            native_ids = {a['steam_id'] for a in self.account_rows}
            self.account_rows.extend({**metadata(a), 'remembered': False, 'imported': True} for a in self.managed.values() if a['steam_id'] not in native_ids)
            self._active = self.native.active_id() if self.native else None
            self._mtime = self.native.login_file.stat().st_mtime_ns if self.native and self.native.login_file.exists() else None
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
            note += " · обновлено " + datetime.fromtimestamp(updated).strftime("%d.%m в %H:%M")
        if data.get("error"):
            note += ("\nСохранённый уровень · " if "level" in data else "") + data["error"]
        self.level_note.setText(note or "Уровень ещё не загружен")
        current = bool(account and self._active == self.selected_id)
        self.current.setText("Сейчас в Steam" if current else "")
        busy = self.switch_worker is not None or self.auth_worker is not None
        self.switch_button.setEnabled(bool(account) and not current and not busy)
        self.switch_button.setText("SteamKit обновляет сессию…" if self.auth_worker else "Переключаю…" if busy else "Аккаунт уже открыт" if current else "Переключить Steam")
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
        worker = LevelWorker(ids, self.api_key, self.generation, self)
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
                self.store.write("request-backoff.json", {"until": self._level_retry_at})
            except SteamError:
                pass
        if "level" in data:
            self.levels[steam_id] = data
        else:
            self.levels[steam_id] = {**self.levels.get(steam_id, {}), "error": data["error"]}
        try:
            self.store.write("levels.json", self.levels)
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
                    row = self.vault.get(result['steam_id'])
                    row['auto_restore'], row['auth_error'] = False, result['error']
                    self.vault.save(row)
                except SteamError as exc:
                    self.message(str(exc), True)
        elif result["state"] == "ready":
            self.message("Steam подтвердил вход в выбранный аккаунт.")
        elif result["state"] == "restored":
            self.message("Прежний выбор восстановлен. Steam открывается.")
        else:
            self.message("Steam открыт. Если он запросил пароль или Steam Guard, завершите вход в его окне.")

    def _switch_finished(self):
        self.switch_worker = None
        self.reload_accounts(refresh_levels=False)

    def tick(self):
        if self.switch_worker or self.auth_worker or not self.native or self._closing:
            return
        try:
            active = self.native.active_id()
            if self._active and not active and self.managed.get(self._active, {}).get('auto_restore'):
                self._recover_account, self._recover_at = self._active, time.monotonic() + 15
            mtime = self.native.login_file.stat().st_mtime_ns if self.native.login_file.exists() else None
            if self.switcher.complete_if_active():
                self.message("Steam подтвердил вход в выбранный аккаунт.")
            if active != self._active or mtime != self._mtime:
                self.reload_accounts(refresh_levels=False)
            else:
                self.render()
            if active:
                self._recover_account = None
            elif self._recover_account and time.monotonic() >= self._recover_at:
                sid, self._recover_account = self._recover_account, None
                if (self.managed.get(sid, {}).get('auto_restore') and self.native.processes()
                        and not self.switcher.has_recovery() and not self.native.games_running()):
                    self._begin_switch(sid)
                    return
            elif not active and not self._recover_account and self.native.processes() and not self.switcher.has_recovery():
                login = self.native.registry_value().get('value', '')
                candidate = next((sid for sid, row in self.managed.items() if row.get('auto_restore') and row['login'].casefold() == login.casefold()), None)
                if candidate:
                    self._recover_account, self._recover_at = candidate, time.monotonic() + 15
            self._auto_auth()
        except (SteamError, OSError) as exc:
            self.message(str(exc) if isinstance(exc, SteamError) else "Не удалось проверить состояние Steam.", True)

    def open_steam(self):
        try:
            if self.native:
                self.native.launch()
        except SteamError as exc:
            self.message(str(exc), True)

    def edit_settings(self):
        dialog = SettingsDialog(str(self.native.root) if self.native else "", self.api_key, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            dialog.deleteLater()
            return
        key = dialog.api_key.text().strip()
        try:
            if self.switcher and self.switcher.has_recovery() and self.native.root != dialog.native.root:
                raise SteamError("Сначала завершите или восстановите прежнее переключение Steam.")
            self.store.write("api-key.dpapi", {"key": key}, encrypted=True)
            self.store.write("settings.json", {"steam_path": str(dialog.native.root)})
            if self.api_key != key:
                self._level_retry_at = 0
                self.store.write("request-backoff.json", {"until": 0})
            self.api_key, self.native = key, dialog.native
            self.switcher = Switcher(self.native, self.store, auth=self.auth, vault=self.vault, token_bridge=self.desktop_token)
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
            password, accepted = QInputDialog.getText(self, 'Пароль архива', 'Оставьте пустым для архива без пароля:', QLineEdit.EchoMode.Password)
            if not accepted:
                return
        try:
            row = import_account(path, password, self.account_rows)
            merged = self.vault.import_record(row)
            if Path(path).suffix.casefold() == '.mafile':
                merged['auto_restore'] = True
                self._capture_selected_token(merged)
                self.vault.save(merged)
            self.selected_id = row['steam_id']
            self.reload_accounts()
            self.message('Аккаунт импортирован. Исходный файл сохранён.')
            if merged.get('auto_restore'):
                self._begin_auth(row['steam_id'])
        except SteamError as exc:
            self.message(str(exc), True)

    def export_selected(self):
        if not self.selected:
            return
        try:
            row = {**self.managed.get(self.selected_id, {}), **metadata(self.selected)}
            self._capture_selected_token(row)
        except SteamError as exc:
            self.message(str(exc), True)
            return
        dialog = ExportDialog(bool(row.get('mafile') or row.get('session')), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            path, _ = QFileDialog.getSaveFileName(self, 'Экспорт аккаунта', 'steam-account.zip', 'ZIP (*.zip)')
            if path:
                try:
                    export_account(path, row, dialog.password.text(), dialog.secrets.isChecked())
                    self.message('Архив аккаунта сохранён.')
                except SteamError as exc:
                    self.message(str(exc), True)
        dialog.password.clear()
        dialog.confirm.clear()
        dialog.deleteLater()

    def _capture_selected_token(self, row):
        if token_valid(row.get('session', {}).get('refresh_token', ''), row['steam_id'], 'client', True):
            return
        try:
            native = self.native.account_list() if self.native else []
            if any(a['steam_id'] == row['steam_id'] and a['login'].casefold() == row['login'].casefold() for a in native):
                token = self.desktop_token.selected_token(row)
                if token:
                    row['session'] = {'refresh_token': token}
        except (SteamError, OSError):
            pass

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
            row['auto_restore'] = dialog.auto.isChecked()
            if dialog.clear_password.isChecked():
                row.pop('password', None)
            password = dialog.password.text() or None
            if password and dialog.remember.isChecked():
                row['password'] = password
            try:
                self._capture_selected_token(row)
                self.vault.save(row)
                self.managed = self.vault.all()
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
        if self.native and self.native.games_running():
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
                row = self.vault.get(result['steam_id'])
                if row:
                    row['auth_error'], row['next_check'] = result['error'], time.time() + 900
                    if result.get('requires_input'):
                        row['auto_restore'] = False
                    self.vault.save(row)
                self.message(result['error'], True)
            else:
                self.message('SteamKit подтвердил аккаунт и обновил сессию.')
            self.managed = self.vault.all()
        except SteamError as exc:
            self.message(str(exc), True)

    def _auth_finished(self):
        self.auth_worker = None
        self.render()

    def _auto_auth(self):
        if not self._start_services or time.time() - self._last_auth_attempt < 60:
            return
        for sid, row in self.managed.items():
            if row.get('auto_restore') and row.get('next_check', 0) <= time.time():
                self._begin_auth(sid, automatic=True)
                return

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
