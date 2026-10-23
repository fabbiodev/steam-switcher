"""Qt thread adapters for application use cases."""
from PyQt6.QtCore import QThread, pyqtSignal
from steam_sda.domain.errors import SteamError, RateLimitError


class UpdateWorker(QThread):
    result = pyqtSignal(dict)
    progress = pyqtSignal(int)

    def __init__(self, service, release=None, parent=None, force=True):
        super().__init__(parent)
        self.service, self.release, self.force = service, release, force

    def run(self):
        try:
            if self.release:
                path = self.service.download(self.release, self.progress.emit, self.isInterruptionRequested)
                self.result.emit({'release': self.release, 'path': path})
            else:
                release = self.service.check(force=self.force, cancelled=self.isInterruptionRequested)
                self.result.emit({'release': release})
        except SteamError as exc:
            self.result.emit({'error': str(exc)})
        except Exception:
            self.result.emit({'error': 'Не удалось обработать обновление. Повторите попытку позже.'})


class LevelWorker(QThread):
    result = pyqtSignal(int, str, dict)

    def __init__(self, ids, api_key, generation, parent=None, *, fetcher):
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


class AvatarWorker(QThread):
    result = pyqtSignal(int, str, bytes)

    def __init__(self, service, ids, generation, parent=None):
        super().__init__(parent)
        self.service, self.ids, self.generation = service, ids, generation

    def run(self):
        known = {}
        # Show every cached image before a single slow network request can block it.
        for sid in self.ids:
            if self.isInterruptionRequested():
                return
            try:
                data = self.service.cached(sid).get('data', b'')
                if data:
                    known[sid] = data
                    self.result.emit(self.generation, sid, data)
            except Exception:
                continue
        for index, sid in enumerate(self.ids):
            if self.isInterruptionRequested():
                return
            if index:
                self.msleep(800)
            try:
                data = self.service.refresh(sid, self.isInterruptionRequested)
                if data and data != known.get(sid) and not self.isInterruptionRequested():
                    self.result.emit(self.generation, sid, data)
            except Exception:
                continue


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
