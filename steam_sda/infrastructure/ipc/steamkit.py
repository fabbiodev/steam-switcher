"""SteamKit transport. Account policy belongs to application.authentication."""
import json
import subprocess
from steam_sda.domain.accounts import json_object
from steam_sda.domain.errors import SteamError
from steam_sda.infrastructure.resources import helper_path
from .processes import run_external


class SteamKitGateway:
    def __init__(self, runner=run_external):
        self.runner = runner

    def authenticate(self, request):
        try:
            result = self.runner([str(helper_path())], input=json.dumps(request), text=True,
                                 encoding='utf-8', capture_output=True, timeout=150,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise SteamError('Модуль SteamKit не завершил вход. Данные аккаунта сохранены.')
            return json_object(result.stdout.encode())
        except FileNotFoundError:
            raise SteamError('Модуль SteamKit не найден. Соберите его через tools/build.py.') from None
        except (OSError, subprocess.TimeoutExpired):
            raise SteamError('Не удалось завершить авторизацию SteamKit. Повторите позже.') from None
