"""Automatic recovery decisions; no timers, Qt, network or process mutation."""
import time


class SessionRecovery:
    def __init__(self):
        self.pending_sid = None
        self.retry_at = 0

    def observe(self, previous, active, managed, *, running, pending_switch, games_running, selected_login=''):
        if previous and not active and managed.get(previous, {}).get('auto_restore'):
            self.pending_sid, self.retry_at = previous, time.monotonic() + 15
        if active:
            self.pending_sid = None
        elif self.pending_sid and time.monotonic() >= self.retry_at:
            sid, self.pending_sid = self.pending_sid, None
            if managed.get(sid, {}).get('auto_restore') and running and not pending_switch and not games_running:
                return sid
        elif not self.pending_sid and running and not pending_switch:
            candidate = next((sid for sid, row in managed.items() if row.get('auto_restore') and row['login'].casefold() == selected_login.casefold()), None)
            if candidate:
                self.pending_sid, self.retry_at = candidate, time.monotonic() + 15
        return None


def next_due_auth(managed, last_attempt, now=None):
    now = time.time() if now is None else now
    if now - last_attempt < 60:
        return None
    return next((sid for sid, row in managed.items() if row.get('auto_restore') and row.get('next_check', 0) <= now), None)
