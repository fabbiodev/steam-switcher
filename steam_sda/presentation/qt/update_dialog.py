"""Update controls use the same application service as the background check."""
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QCheckBox, QProgressBar
from steam_sda import __version__
from steam_sda.domain.errors import SteamError
from .widgets import label, button
from .workers import UpdateWorker


class UpdateDialog(QDialog):
    def __init__(self, service, available=None, parent=None):
        super().__init__(parent)
        self.service, self.release = service, available
        self.worker = None
        self._result = {}
        self._close_requested = False
        self.installer_path = None
        self.setWindowTitle('Обновления Steam SDA')
        self.setMinimumWidth(520)
        box = QVBoxLayout(self)
        box.setContentsMargins(24, 24, 24, 24)
        box.setSpacing(15)
        box.addWidget(label('Обновления', 'headline'))
        box.addWidget(label(f'Установлена версия {__version__}', 'muted'))
        self.status = label('', 'nameSmall')
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        self.automatic = QCheckBox('Проверять обновления автоматически')
        self.automatic.setChecked(service.automatic())
        self.automatic.toggled.connect(self._set_automatic)
        box.addWidget(self.automatic)
        note = label('Проверка выполняется в фоне не чаще раза в сутки. '
                     'Установщик проверяется по SHA-256. При установке Steam SDA закроется '
                     'и откроется после обновления; аккаунты сохранятся. '
                     'Перед установкой закройте Steam, чтобы освободить DLL Steam SDA.', 'muted')
        note.setWordWrap(True)
        box.addWidget(note)
        self.progress = QProgressBar()
        self.progress.hide()
        box.addWidget(self.progress)
        actions = QHBoxLayout()
        self.check_button = button('Проверить сейчас', self.check, icon_name='refresh')
        self.install_button = button('Скачать и установить', self.download, True, icon_name='import')
        actions.addWidget(self.check_button)
        actions.addWidget(self.install_button)
        box.addLayout(actions)
        self.cancel_button = button('Закрыть', self.reject)
        box.addWidget(self.cancel_button)
        self._render_available()

    def _set_automatic(self, value):
        try:
            self.service.set_automatic(value)
        except SteamError as exc:
            self.status.setText(str(exc))
            self.automatic.blockSignals(True)
            self.automatic.setChecked(not value)
            self.automatic.blockSignals(False)

    def _render_available(self):
        self.status.setText(f'Доступна версия {self.release.version}' if self.release else 'Нажмите «Проверить сейчас», чтобы найти новый выпуск.')
        self.install_button.setEnabled(self.release is not None)

    def check(self):
        self._start()

    def download(self):
        if self.release:
            self._start(self.release)

    def _start(self, release=None):
        if self.worker:
            return
        self._result = {}
        self.check_button.setEnabled(False)
        self.install_button.setEnabled(False)
        self.status.setText('Скачиваю и проверяю установщик…' if release else 'Проверяю GitHub Releases…')
        self.progress.setRange(0, 100 if release else 0)
        self.progress.setValue(0)
        self.progress.show()
        self.worker = UpdateWorker(self.service, release, self)
        self.worker.result.connect(self._receive)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.finished.connect(self._finished)
        self.worker.start()

    def _receive(self, result):
        self._result = result

    def _finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.progress.hide()
        if self._close_requested:
            super().reject()
            return
        self.check_button.setEnabled(True)
        result = self._result
        if result.get('error'):
            self.status.setText(result['error'])
            self.install_button.setEnabled(self.release is not None)
        elif result.get('path'):
            self.installer_path = result['path']
            self.accept()
        else:
            self.release = result.get('release')
            self._render_available()
            if self.release is None:
                self.status.setText('Установлена последняя стабильная версия.')

    def reject(self):
        if self.worker:
            self._close_requested = True
            self.worker.requestInterruption()
            self.status.setText('Отменяю запрос…')
            return
        super().reject()

    def closeEvent(self, event):
        if self.worker:
            self.reject()
            event.ignore()
        else:
            event.accept()
