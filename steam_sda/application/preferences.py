"""App settings and level-cache policy over the storage port."""
from dataclasses import dataclass, field
import time
from steam_sda.domain.errors import SteamError
from steam_sda.domain.identity import valid_id
from .ports import StorePort


@dataclass
class Preferences:
    settings: dict = field(default_factory=dict)
    levels: dict = field(default_factory=dict)
    api_key: str = ''
    retry_at: float = 0


class PreferenceService:
    def __init__(self, store: StorePort):
        self.store = store

    def load(self):
        state = Preferences(settings=self.store.read('settings.json'))
        if state.settings.get('steam_path') is not None and not isinstance(state.settings['steam_path'], str):
            raise SteamError('Некорректная папка Steam в настройках. Выберите её заново.')
        for sid, value in self.store.read('levels.json').items():
            if valid_id(sid) and isinstance(value, dict) and type(value.get('level')) is int and 0 <= value['level'] <= 999999:
                state.levels[sid] = {k: v for k, v in value.items() if k in ('level', 'updated_at', 'source', 'error')}
                for key in ('source', 'error'):
                    if key in state.levels[sid] and not isinstance(state.levels[sid][key], str):
                        state.levels[sid].pop(key)
        until = self.store.read('request-backoff.json').get('until', 0)
        if type(until) in (int, float) and time.time() < until <= time.time() + 3600:
            state.retry_at = until
        key = self.store.read('api-key.dpapi', encrypted=True).get('key', '')
        state.api_key = key if isinstance(key, str) else ''
        return state

    def save(self, steam_path, api_key, previous_key):
        self.store.write('api-key.dpapi', {'key': api_key}, encrypted=True)
        self.store.write('settings.json', {'steam_path': str(steam_path)})
        if api_key != previous_key:
            self.store.write('request-backoff.json', {'until': 0})

    def save_levels(self, levels):
        self.store.write('levels.json', levels)

    def save_backoff(self, until):
        self.store.write('request-backoff.json', {'until': until})
