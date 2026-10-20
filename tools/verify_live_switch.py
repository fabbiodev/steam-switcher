"""Opt-in live roundtrip on the owner's remembered Steam accounts; no credentials printed."""
import base64
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from steam_sda.errors import SteamError
from steam_sda.steam import NativeSteam, Switcher
from steam_sda.storage import Store


def main():
    if '--run' not in sys.argv:
        raise SystemExit('Explicit --run required; Steam will restart twice.')
    native, store = NativeSteam(), Store()
    switcher = Switcher(native, store, timeout=75)
    original = native.active_id()
    accounts = native.account_list()
    target = next((a for a in accounts if a['steam_id'] != original and a['remembered']), None)
    if not original or not target or native.games_running() or switcher.has_recovery():
        raise SystemExit('Live verification requires an identified current account, another remembered account, no running games, and no pending switch.')
    store.write('verification-initial.dpapi', {'root': str(native.root), 'active': original,
                'loginusers': base64.b64encode(native.read_logins()).decode(), 'registry': native.registry_value()}, encrypted=True)
    report = {'accounts_before': len(accounts), 'switch_state': None, 'original_restored': False}
    try:
        print('Checking native switch...', flush=True)
        result = switcher.switch(target['steam_id'], lambda _: None)
        report['switch_state'] = result['state']
    except SteamError as exc:
        report['error'] = str(exc)
    finally:
        print('Returning to original account...', flush=True)
        try:
            if switcher.has_recovery():
                switcher.restore()
            elif native.active_id() != original:
                switcher.switch(original)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and native.active_id() != original:
                time.sleep(.5)
            report['original_restored'] = native.active_id() == original
            if report['original_restored']:
                switcher.complete_if_active()
        except SteamError as exc:
            report['restore_error'] = str(exc)
    after = native.account_list()
    report['accounts_after'] = len(after)
    report['account_ids_preserved'] = {a['steam_id'] for a in accounts} == {a['steam_id'] for a in after}
    report['pending_recovery'] = switcher.has_recovery()
    path = store.directory / 'live-verification.json'
    path.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['switch_state'] == 'ready' and report['original_restored'] and report['account_ids_preserved'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
