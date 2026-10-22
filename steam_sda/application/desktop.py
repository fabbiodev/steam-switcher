"""Observe Steam and choose recovery actions without exposing registry data to Qt."""
from dataclasses import dataclass
from .sessions import SessionRecovery


@dataclass(frozen=True)
class DesktopUpdate:
    active_id: str | None
    revision: int | None
    switch_completed: bool
    recover_sid: str | None


class DesktopCoordinator:
    def __init__(self, native, switcher):
        self.native, self.switcher = native, switcher
        self.recovery = SessionRecovery()

    def status(self):
        return (self.native.active_id(), self.native.login_revision()) if self.native else (None, None)

    def poll(self, previous, managed):
        active, revision = self.status()
        completed = self.switcher.complete_if_active()
        running = bool(self.native.processes()) if not active else False
        recover_sid = self.recovery.observe(previous, active, managed,
            running=running, pending_switch=self.switcher.has_recovery(),
            games_running=self.native.games_running() if running else False,
            selected_login=self.native.registry_value().get('value', '') if running else '')
        return DesktopUpdate(active, revision, completed, recover_sid)

    def focus_running_client(self):
        if self.native and self.native.processes():
            self.native.launch()
            return True
        return False

    def can_authenticate(self):
        return not self.native or not self.native.games_running()
