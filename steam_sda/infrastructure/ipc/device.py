"""Native device helper transport and Windows process observations."""
import json
import subprocess
import psutil
from steam_sda.domain.errors import SteamError
from steam_sda.infrastructure.resources import helper_path
from .processes import run_external


class NativeDeviceGateway:
    def __init__(self, native, runner=run_external):
        self.native, self.runner = native, runner

    def call(self, mode, profile=None):
        request = {'steam_path': str(self.native.root)}
        if profile is not None:
            request['hwid'] = profile
        try:
            result = self.runner([str(helper_path()), mode], input=json.dumps(request), text=True,
                                 encoding='utf-8', capture_output=True, timeout=65,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            response = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise SteamError('Модуль HWID не ответил. Соберите программу через tools/build.py; вход остановлен.') from None
        if result.returncode or not isinstance(response, dict) or response.get('ok') is not True:
            code = response.get('error', '') if isinstance(response, dict) else ''
            messages = {
                'device_version': 'Эта версия Steam пока не поддерживается модулем HWID. Привязка сохранена; вход остановлен.',
                'device_capture': 'Не удалось прочитать все компоненты HWID текущего компьютера. Привязка не создана.',
                'device_running': 'Steam уже запущен. Сначала переключите аккаунт через Steam SDA.',
                'device_late': 'Steam загрузил сборщик HWID до установки профиля. Запуск остановлен.',
            }
            raise SteamError(messages.get(code, 'Не удалось применить сохранённый HWID. Запуск остановлен.'))
        return response

    def is_applied(self, pid, launched):
        try:
            return any(p.pid == pid and launched - 65 <= p.create_time() <= launched
                       for p in self.native.processes())
        except (OSError, psutil.Error):
            return False
