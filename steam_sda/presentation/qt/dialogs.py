"""Dialogs collect user choices; application services validate them."""
import webbrowser
from PyQt6.QtWidgets import QDialog, QInputDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QCheckBox, QFormLayout, QFileDialog
from steam_sda.domain.errors import SteamError
from .widgets import label, button


class DeleteAccountDialog(QDialog):
    def __init__(self, account, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Удаление аккаунта')
        self.setMinimumWidth(460)
        box = QVBoxLayout(self)
        box.setContentsMargins(24, 24, 24, 24)
        box.setSpacing(16)
        box.addWidget(label('Удалить аккаунт из базы?', 'headline'))
        box.addWidget(label(account['name'] + '\n' + account['login'], 'nameSmall'))
        box.addWidget(label('Будут удалены сохранённые в Steam SDA пароль, сессия, maFile и HWID. '
                            'Аккаунт исчезнет из списка и не добавится при обновлении. '
                            'Вернуть его можно через импорт.', 'muted'))
        box.addWidget(label('Аккаунт Steam и исходные файлы останутся на месте. Текущий вход в Steam сохранится.', 'muted'))
        actions = QHBoxLayout()
        cancel = button('Отмена', self.reject)
        cancel.setDefault(True)
        self.confirm = button('Удалить аккаунт', self.accept)
        self.confirm.setProperty('danger', True)
        actions.addWidget(cancel)
        actions.addWidget(self.confirm)
        box.addLayout(actions)
        cancel.setFocus()


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


class ArchivePasswordDialog(QInputDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Пароль архива')
        self.setLabelText('Оставьте пустым для архива без пароля:')
        self.setInputMode(QInputDialog.InputMode.TextInput)
        self.setTextEchoMode(QLineEdit.EchoMode.Password)
        self.findChild(QLineEdit).setMaxLength(2**31 - 1)
        self.setOkButtonText('Импорт')
        self.setCancelButtonText('Отмена')


class ExportDialog(QDialog):
    def __init__(self, has_secrets, parent, account_count=1):
        super().__init__(parent)
        self.setWindowTitle('Экспорт всей базы аккаунтов')
        self.setMinimumWidth(460)
        box = QVBoxLayout(self)
        box.setContentsMargins(24, 24, 24, 24)
        box.setSpacing(14)
        box.addWidget(label(f'Аккаунтов в архиве: {account_count}', 'headline'))
        self.secrets = QCheckBox('Включить maFile и токены входа')
        self.secrets.setEnabled(has_secrets)
        self.secrets.setChecked(has_secrets)
        box.addWidget(self.secrets)
        box.addWidget(label('Пароль Steam в архив не включается. Архив с maFile и токенами позволяет войти в аккаунт.', 'muted'))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setMaxLength(2**31 - 1)
        self.password.setPlaceholderText('Необязательно · оставьте пустым для ZIP без пароля')
        box.addWidget(self.password)
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm.setMaxLength(2**31 - 1)
        self.confirm.setPlaceholderText('Повторите пароль архива')
        box.addWidget(self.confirm)
        self.protection_note = label('', 'muted')
        box.addWidget(self.protection_note)
        self.password.textChanged.connect(self._update_protection)
        self.secrets.toggled.connect(self._update_protection)
        self._update_protection()
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
        else:
            self.accept()

    def _update_protection(self):
        if self.password.text():
            text = 'Архив будет зашифрован AES-256.'
        elif self.secrets.isChecked():
            text = 'ZIP без шифрования: maFile и токены будут доступны тому, у кого есть архив.'
        else:
            text = 'ZIP без шифрования.'
        self.protection_note.setText(text)


class SettingsDialog(QDialog):
    def __init__(self, steam_path, api_key, validator, parent=None):
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
        self.validator = validator

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Папка со steam.exe", self.path.text())
        if path:
            self.path.setText(path)

    def _save(self):
        try:
            self.native = self.validator(self.path.text().strip())
        except SteamError as exc:
            self.error.setText(str(exc))
            return
        key = self.api_key.text().strip()
        if key and (len(key) != 32 or any(c not in "0123456789abcdefABCDEF" for c in key)):
            self.error.setText("Введите API-ключ из 32 шестнадцатеричных символов или оставьте поле пустым.")
            return
        self.accept()
