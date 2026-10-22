class SteamError(Exception):
    """A message safe to display without exposing session data."""


class RateLimitError(SteamError):
    def __init__(self, seconds=300):
        super().__init__("Steam ограничил частоту запросов. Повторите через несколько минут или используйте API-ключ в настройках.")
        self.seconds = max(60, min(3600, seconds))


MESSAGES = {
    'needs_credentials': 'Для восстановления нужен пароль или действующая мобильная сессия из maFile.',
    'needs_email_guard': 'Steam требует код из письма. Завершите вход через клиент Steam.',
    'needs_device_guard': 'Steam отклонил код Guard. Проверьте maFile и время Windows.',
    'mobile_token_rejected': 'Мобильная сессия maFile отозвана. Укажите пароль для повторного входа.',
    'owner_mismatch': 'Steam подтвердил другой аккаунт. Передача сессии остановлена.',
    'token_rejected': 'Steam отклонил сохранённый токен. Нужен повторный вход.',
    'steam_5': 'Steam отклонил пароль. Проверьте данные входа.',
    'steam_84': 'Steam ограничил попытки входа. Повторите позже.',
    'timeout': 'Steam не ответил вовремя. Повторите позже.',
}


class AuthError(SteamError):
    def __init__(self, code):
        super().__init__(MESSAGES.get(code, 'SteamKit не смог восстановить вход. Проверьте сеть и данные аккаунта.'))
        self.requires_input = code in {'needs_credentials', 'needs_email_guard', 'needs_device_guard',
                                      'mobile_token_rejected', 'owner_mismatch', 'token_rejected', 'steam_5'}
        if isinstance(code, str) and code.startswith('steam_') and code not in {'steam_84', 'steam_20', 'steam_16', 'steam_2', 'steam_3'}:
            self.requires_input = True
