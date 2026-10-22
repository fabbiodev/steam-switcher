"""Update policy; network, filesystem and process operations stay in adapters."""
import time
from steam_sda.domain.updates import version_tuple
from steam_sda.domain.errors import SteamError
from .ports import StorePort, UpdateGateway


class UpdateService:
    def __init__(self, store: StorePort, gateway: UpdateGateway, current: str):
        self.store, self.gateway, self.current = store, gateway, current

    def automatic(self):
        return self.store.read('updates.json').get('automatic', True) is not False

    def set_automatic(self, value):
        settings = self.store.read('updates.json')
        settings['automatic'] = bool(value)
        self.store.write('updates.json', settings)

    def check(self, force=False, cancelled=lambda: False):
        settings = self.store.read('updates.json')
        checked = settings.get('checked_at', 0)
        recent = type(checked) in (int, float) and 0 <= time.time() - checked < 86400
        if not force and (not self.automatic() or recent):
            return None
        release = self.gateway.latest(self.current, cancelled)
        if cancelled():
            raise SteamError('Проверка обновлений отменена.')
        settings = self.store.read('updates.json')
        settings['checked_at'] = time.time()
        self.store.write('updates.json', settings)
        return release

    def download(self, release, progress=lambda value: None, cancelled=lambda: False):
        release.validate()
        if version_tuple(release.version) <= version_tuple(self.current):
            raise SteamError('Эта версия уже установлена. Понижение версии отключено.')
        return self.gateway.download(release, progress, cancelled)

    def install(self, release, path):
        release.validate()
        if version_tuple(release.version) <= version_tuple(self.current):
            raise SteamError('Понижение версии отключено.')
        self.gateway.install(release, path)
