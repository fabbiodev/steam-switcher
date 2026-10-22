"""Composition root: the only module that constructs the application's adapters."""
from dataclasses import dataclass
from typing import Callable
from steam_sda.domain.errors import SteamError
from steam_sda.application.accounts import AccountService
from steam_sda.application.authentication import AuthenticationService
from steam_sda.application.devices import DeviceProfiles
from steam_sda.application.desktop import DesktopCoordinator
from steam_sda.application.preferences import PreferenceService, Preferences
from steam_sda.application.switching import Switcher
from steam_sda.application.ports import StorePort, AccountRepository, SteamPort
from steam_sda.infrastructure.persistence.storage import Store
from steam_sda.infrastructure.persistence.vault import AccountVault
from steam_sda.infrastructure.persistence.archives import AccountArchives
from steam_sda.infrastructure.steam.client import NativeSteam
from steam_sda.infrastructure.steam.token_cache import DesktopToken
from steam_sda.infrastructure.ipc.steamkit import SteamKitGateway
from steam_sda.infrastructure.ipc.device import NativeDeviceGateway
from steam_sda.infrastructure.community.levels import fetch_level
from steam_sda.infrastructure.resources import resource_path
from steam_sda.infrastructure.persistence.storage import data_dir


def prepare_app_data_dir():
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@dataclass
class AppServices:
    store: StorePort
    vault: AccountRepository
    auth: AuthenticationService
    desktop_token: DesktopToken
    preferences: PreferenceService
    state: Preferences
    native: SteamPort | None = None
    devices: DeviceProfiles | None = None
    switcher: Switcher | None = None
    accounts: AccountService | None = None
    desktop: DesktopCoordinator | None = None
    fetch_level: Callable[[str, str], dict] = fetch_level
    startup_error: str = ''
    enable_devices: bool = True

    @staticmethod
    def inspect_installation(path):
        native = NativeSteam(path)
        native.account_list()
        return native

    def wire_steam(self, native):
        self.native = native
        self.devices = DeviceProfiles(self.vault, NativeDeviceGateway(native)) if native and self.enable_devices else None
        self.auth.devices = self.devices
        self.accounts = AccountService(self.vault, AccountArchives(), native, self.desktop_token, self.devices)
        self.switcher = Switcher(native, self.store, auth=self.auth, vault=self.vault,
                                 token_bridge=self.desktop_token, devices=self.devices) if native else None
        self.desktop = DesktopCoordinator(native, self.switcher)

    def configure_steam(self, native, api_key):
        if self.switcher and self.switcher.has_recovery() and self.native.root != native.root:
            raise SteamError('Сначала завершите или восстановите прежнее переключение Steam.')
        self.preferences.save(native.root, api_key, self.state.api_key)
        changed = api_key != self.state.api_key
        self.state.api_key = api_key
        self.state.settings = {'steam_path': str(native.root)}
        if changed:
            self.state.retry_at = 0
        self.wire_steam(native)
        return changed


def create_services(native=None, store=None, enable_devices=True, discover=True):
    store = store or Store()
    vault = AccountVault(store)
    preferences = PreferenceService(store)
    services = AppServices(store, vault, AuthenticationService(vault, SteamKitGateway()),
                           DesktopToken(), preferences, Preferences(), enable_devices=enable_devices)
    try:
        services.state = preferences.load()
        if native is None and discover:
            native = NativeSteam(services.state.settings.get('steam_path'))
    except SteamError as exc:
        services.startup_error = str(exc)
    services.wire_steam(native)
    return services
