"""Qt thread adapters for application use cases."""
from PyQt6.QtCore import QThread, pyqtSignal
from steam_sda.domain.errors import SteamError, RateLimitError


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
