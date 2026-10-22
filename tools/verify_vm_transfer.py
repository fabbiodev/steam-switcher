"""Explicit live transfer check on a Windows VM, with a credential-free report."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def require_vm():
    if sys.platform != 'win32':
        raise RuntimeError('A Windows VM is required.')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
        'Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model | ConvertTo-Json -Compress'],
        capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    machine = json.loads(result.stdout)
    identity = (str(machine.get('Manufacturer')) + ' ' + str(machine.get('Model'))).casefold()
    if not any(marker in identity for marker in ('vmware', 'virtualbox', 'virtual machine', 'qemu', 'kvm')):
        raise RuntimeError('Live checks are restricted to a recognized virtual machine.')


def helper(mode, request):
    from steam_sda.infrastructure.resources import helper_path
    from steam_sda.infrastructure.ipc.processes import run_external
    from steam_sda.domain.errors import SteamError
    result = run_external([str(helper_path()), mode], input=json.dumps(request), timeout=70,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    response = json.loads(result.stdout)
    if result.returncode != 0 or response.get('ok') is not True:
        raise SteamError('The device diagnostic helper failed; check the VM build and supported Steam version.')
    return response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--steam-id', required=True)
    parser.add_argument('--archive', help='An existing ZIP on the VM; password can be set in STEAMSDA_ARCHIVE_PASSWORD.')
    parser.add_argument('--report', default=str(ROOT / 'build/vm-transfer-check.json'))
    args = parser.parse_args()
    if not args.run:
        parser.error('Explicit --run required: Steam on the VM will restart.')
    require_vm()  # Before reading the vault or interacting with Steam.
    from PyQt6.QtCore import QLockFile
    from steam_sda.bootstrap import create_services, prepare_app_data_dir
    from steam_sda.domain.device import fingerprint
    from steam_sda.domain.identity import valid_id
    from steam_sda.domain.errors import SteamError
    if not valid_id(args.steam_id):
        parser.error('Invalid SteamID.')
    lock = QLockFile(str(prepare_app_data_dir() / 'app.lock'))
    if not lock.tryLock(0):
        raise SteamError('Close Steam SDA on the VM before the test.')
    services = create_services()
    if not services.native or not services.switcher:
        raise SteamError('Steam was not found on the VM.')
    if services.native.games_running() or services.switcher.has_recovery():
        raise SteamError('Close Steam games and resolve pending recovery on the VM first.')
    if args.archive:
        password = os.environ.pop('STEAMSDA_ARCHIVE_PASSWORD', '')
        rows = services.accounts.archives.read_all(args.archive, password, services.native.account_list())
        row = next((item for item in rows if item['steam_id'] == args.steam_id), None)
        if row is None:
            raise SteamError('The archive belongs to another account.')
        if not row.get('hwid'):
            raise SteamError('The archive has no saved hardware profile; export it with login data.')
        imported = services.accounts.import_all_file(args.archive, password, services.native.account_list())
        merged = next(item for item in imported if item['steam_id'] == args.steam_id)
        if fingerprint(row['hwid']) != fingerprint(merged['hwid']):
            raise SteamError('The VM already has another first-bound profile. Remove that local record and import the source ZIP again.')
    row = services.vault.get(args.steam_id)
    if not row or not row.get('hwid'):
        raise SteamError('Import the selected source account and its profile on the VM first.')
    original = services.native.active_id()
    report = {'ok': False, 'virtual_machine': True, 'account_suffix': args.steam_id[-4:],
              'expected_machine_id_sha256': fingerprint(row['hwid']), 'switch_state': None,
              'authorization_payload_observed': False}
    try:
        baseline = helper('--capture-device', {'steam_path': str(services.native.root)})['hwid']
        report['profile_differs_from_vm_native'] = fingerprint(baseline) != fingerprint(row['hwid'])
        result = services.switcher.switch(args.steam_id)
        report['switch_state'] = result['state']
        launched = services.devices._launches.get(args.steam_id)
        if not launched:
            raise SteamError('No newly instrumented Steam process was recorded.')
        request = {'steam_path': str(services.native.root), 'hwid': row['hwid'], 'steam_pid': launched[0]}
        observation = helper('--observe-device', request)
        report['observation'] = observation
        report['active_owner_matches'] = services.native.active_id() == args.steam_id
        report['ok'] = (result['state'] == 'ready' and report['active_owner_matches']
                        and observation.get('machine_id_inputs_match') is True
                        and observation.get('machine_id_sha256_observed') == fingerprint(row['hwid']))
    except SteamError as exc:
        report['error'] = str(exc)  # Sanitized application error; never helper stdout.
    finally:
        try:
            if services.switcher.has_recovery():
                services.switcher.restore()
            elif original and services.native.active_id() != original:
                services.switcher.switch(original)
            if original:
                deadline = time.monotonic() + 75
                while time.monotonic() < deadline and services.native.active_id() != original:
                    time.sleep(.5)
                report['original_restored'] = services.native.active_id() == original
                report['ok'] &= report['original_restored']
            report['pending_recovery'] = services.switcher.has_recovery()
        except SteamError:
            report['restore_error'] = 'Restore did not complete. Use the recovery action on the VM.'
            report['ok'] = False
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report), flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Include only the error type for unexpected exceptions, never account data.
        from steam_sda.domain.errors import SteamError
        output = {'ok': False, 'error_type': type(exc).__name__}
        if isinstance(exc, SteamError):
            output['error'] = str(exc)
        print(json.dumps(output))
        raise SystemExit(1)
