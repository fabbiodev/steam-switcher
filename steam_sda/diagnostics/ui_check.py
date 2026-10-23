"""End-to-end UI and helper verification using only synthetic accounts."""
import json
import os
import sys
from pathlib import Path
from PyQt6.QtGui import QFontInfo
from steam_sda.presentation.qt.window import MainWindow
from steam_sda.bootstrap import create_services
from steam_sda import __version__


def check_ui(app, report_path):
    app.setProperty('animationsEnabled', False)
    import tempfile
    from steam_sda.infrastructure.persistence.storage import Store, protect
    from steam_sda.infrastructure.steam.vdf import accounts
    class FixtureNative:
        root = Path("C:/Steam-UI-check")
        login_file = root / "config/loginusers.vdf"

        def login_revision(self): return None

        def account_list(self):
            return [{"steam_id": "76561198000000001", "name": "nightshift", "login": "night_login", "remembered": True, "recent": True},
                    {"steam_id": "76561198000000002", "name": "second account", "login": "second_login", "remembered": True, "recent": False}]

        def active_id(self): return "76561198000000001"
    with tempfile.TemporaryDirectory() as directory:
        store = Store(directory)
        store.write("levels.json", {"76561198000000001": {"level": 64, "source": "Steam Community"}})
        win = MainWindow(create_services(FixtureNative(), store, enable_devices=False), start_services=False)
        win.show()
        app.processEvents()
        dpapi = protect(protect(b"ui-check"), True) == b"ui-check" if os.name == "nt" else True
        summary = {"ok": win.level.text() == "64" and win.list.count() == 2 and dpapi,
                   "app_version": __version__,
                   "font": QFontInfo(app.font()).family(), "accounts": win.list.count(),
                   "current_switch_disabled": not win.switch_button.isEnabled(), "dpapi": dpapi}
        summary['application_icon'] = not win.windowIcon().isNull() and not win.brand_icon.pixmap().isNull()
        summary['ok'] &= summary['application_icon']
        from PyQt6.QtCore import QBuffer, QIODevice, Qt
        from PyQt6.QtGui import QImage
        image = QImage(64, 64, QImage.Format.Format_RGB32)
        image.fill(0xFF5F9BAF)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, 'JPEG')
        first_item = win.list.item(0)
        win._avatar_result(win.generation, '76561198000000001', bytes(buffer.data()))
        win._level_result(win.generation, '76561198000000001', {'level': 64})
        summary['sidebar_profiles'] = (win.list.item(0) is first_item and win.list.sizeHintForRow(0) == 72
                                       and first_item.data(Qt.ItemDataRole.UserRole + 1)['level'] == 64
                                       and not win.avatar.pixmap().isNull())
        summary['ok'] &= summary['sidebar_profiles']
        win.list.setCurrentRow(1)
        summary["other_switch_enabled"] = win.switch_button.isEnabled()
        summary["ok"] &= summary["other_switch_enabled"] and summary["current_switch_disabled"]
        win.details_tab.click()
        summary['details_navigation'] = win.pages.currentIndex() == 1 and win.delete_button.isEnabled()
        win.accounts.delete(win.selected_id)
        win.reload_accounts(refresh_levels=False)
        summary['local_account_deletion'] = win.list.count() == 1 and win.selected_id == '76561198000000001'
        summary['ok'] &= summary['details_navigation'] and summary['local_account_deletion']
        from steam_sda.presentation.qt.update_dialog import UpdateDialog
        update_dialog = UpdateDialog(win.services.updates, parent=win)
        summary['updates_ui'] = (win.updates_button.isEnabled() and update_dialog.automatic.isChecked()
                                 and not update_dialog.install_button.isEnabled())
        summary['ok'] &= summary['updates_ui']
        update_dialog.close()
        update_dialog.deleteLater()
        if getattr(sys, 'frozen', False):
            from steam_sda.infrastructure.resources import resource_path
            location = Path(sys.modules[MainWindow.__module__].__file__).resolve()
            summary['external_application_code'] = location == resource_path('app/steam_sda/presentation/qt/window.pyc')
            manifest = json.loads(resource_path('layout.json').read_text(encoding='utf-8'))
            summary['organized_distribution'] = (manifest['version'] == __version__
                and all(resource_path(folder).is_dir() for folder in ('app', 'runtime', 'assets', 'integrations', 'docs', 'licenses'))
                and not resource_path('_internal').exists())
            summary['ok'] &= summary['external_application_code'] and summary['organized_distribution']
        from steam_sda.infrastructure.resources import helper_path
        if helper_path().exists():
            import subprocess
            from steam_sda.infrastructure.ipc.processes import run_external
            check = run_external([str(helper_path()), '--self-test'], capture_output=True, text=True,
                                   timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            backend = json.loads(check.stdout)
            summary['steamkit'] = backend
            summary['ok'] &= check.returncode == 0 and backend.get('ok') is True
        elif hasattr(sys, '_MEIPASS'):
            summary['ok'] = False
            summary['steamkit'] = {'error': 'missing'}
        from steam_sda.infrastructure.persistence.archives import export_account, import_account, export_accounts, import_accounts
        import base64
        synthetic = {'steam_id': '76561198000000001', 'name': 'UI check', 'login': 'night_login',
                     'mafile': {'account_name': 'night_login', 'shared_secret': base64.b64encode(bytes(20)).decode(),
                                'Session': {'SteamID': '76561198000000001'}}}
        synthetic['hwid'] = {'version': 1, 'source': 'native_collectors', 'steam_id': synthetic['steam_id'],
                             'captured_at': 1700000000, 'machine_guid': 'lab-guid', 'network_id': bytes(range(16)).hex(),
                             'disk_serial': 'LABDISK', 'pc_name': 'LAB-PC', 'fqdn': 'LAB-PC.lab.invalid'}
        archive = Path(directory) / 'synthetic-account.zip'
        export_account(archive, synthetic, 'synthetic-ui-password')
        restored = import_account(archive, 'synthetic-ui-password')
        summary['encrypted_zip'] = restored['steam_id'] == synthetic['steam_id'] and restored['mafile'] == synthetic['mafile']
        summary['ok'] &= summary['encrypted_zip']
        summary['hwid_roundtrip'] = restored.get('hwid') == synthetic['hwid']
        summary['ok'] &= summary['hwid_roundtrip']
        second = {'steam_id': '76561198000000002', 'name': 'Second check', 'login': 'second_login',
                  'hwid': {**synthetic['hwid'], 'steam_id': '76561198000000002'}}
        database = Path(directory) / 'synthetic-database.zip'
        export_accounts(database, [synthetic, second], 'synthetic-ui-password')
        from steam_sda.infrastructure.persistence.vault import AccountVault
        imported_vault = AccountVault(Store(Path(directory) / 'imported'))
        imported_vault.import_records(import_accounts(database, 'synthetic-ui-password'))
        imported = imported_vault.all()
        summary['database_roundtrip'] = (set(imported) == {synthetic['steam_id'], second['steam_id']}
            and imported[synthetic['steam_id']]['mafile'] == restored['mafile']
            and all(imported[row['steam_id']]['hwid'] == row['hwid'] for row in (synthetic, second)))
        summary['ok'] &= summary['database_roundtrip']
        if helper_path().exists():
            check = run_external([str(helper_path()), '--device-self-test'],
                                 input=json.dumps({'steam_path': 'C:/SyntheticSteam', 'hwid': synthetic['hwid']}),
                                 timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            summary['hwid_module'] = json.loads(check.stdout)
            summary['ok'] &= summary['hwid_module'].get('ok') is True
        Path(report_path).write_text(json.dumps(summary), encoding="utf-8")
        win.close()
    return 0 if summary["ok"] else 1
