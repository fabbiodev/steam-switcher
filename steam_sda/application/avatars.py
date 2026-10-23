"""Public avatar caching policy, independent of Qt and Steam authentication."""
import time
from steam_sda.domain.errors import SteamError, RateLimitError
from steam_sda.domain.identity import valid_id
from .ports import AvatarGateway


class AvatarService:
    def __init__(self, gateway: AvatarGateway):
        self.gateway = gateway
        self._retry = {}
        self._global_retry = 0

    def cached(self, sid):
        if not valid_id(sid):
            return {}
        return self.gateway.read(sid)

    def refresh(self, sid, cancelled=lambda: False):
        if not valid_id(sid) or cancelled():
            return b''
        cache = self.cached(sid)
        now = time.time()
        stamp = cache.get('updated_at', 0)
        if (cache.get('data') and type(stamp) in (int, float) and 0 <= now - stamp < 86400
                or now < max(self._global_retry, self._retry.get(sid, 0))):
            return cache.get('data', b'')
        try:
            data = self.gateway.fetch(sid, cancelled)
            if cancelled():
                return cache.get('data', b'')
            self.gateway.save(sid, data)
            self._retry.pop(sid, None)
            return data
        except SteamError as exc:
            if cancelled():
                return cache.get('data', b'')
            self._retry[sid] = now + 600
            if isinstance(exc, RateLimitError):
                self._global_retry = now + exc.seconds
            return cache.get('data', b'')
