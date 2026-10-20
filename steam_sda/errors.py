class SteamError(Exception):
    """A message safe to display without exposing session data."""


class RateLimitError(SteamError):
    def __init__(self, seconds=300):
        super().__init__("Steam ограничил частоту запросов. Повторите через несколько минут или используйте API-ключ в настройках.")
        self.seconds = max(60, min(3600, seconds))
