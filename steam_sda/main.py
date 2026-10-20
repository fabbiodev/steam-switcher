import argparse
import json
import os
from pathlib import Path
import sys


def resource_path(relative):
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / relative


def main():
    parser = argparse.ArgumentParser(description="Steam SDA")
    parser.add_argument("--check-ui", metavar="REPORT")
    args = parser.parse_args()
    if sys.platform == "win32":
        os.environ.setdefault("QT_QPA_PLATFORM", "windows:fontengine=directwrite")
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QFont, QFontDatabase, QFontInfo
    app = QApplication(sys.argv)
    app.setApplicationName("Steam SDA")
    app.setOrganizationName("SteamSDA")
    app.setStyle("Fusion")
    font_id = QFontDatabase.addApplicationFont(str(resource_path("assets/fonts/Inter-V.ttf")))
    families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    font = QFont(families[0] if families else "Segoe UI", 10)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.PreferQuality)
    app.setFont(font)
    app.setStyleSheet(resource_path("assets/style.qss").read_text(encoding="utf-8"))
    from .ui import MainWindow
    if args.check_ui:
        import tempfile
        from .storage import Store, protect
        from .vdf import accounts
        class FixtureNative:
            root = Path("C:/Steam-UI-check")
            login_file = root / "config/loginusers.vdf"

            def account_list(self):
                return [{"steam_id": "76561198000000001", "name": "nightshift", "login": "night_login", "remembered": True, "recent": True},
                        {"steam_id": "76561198000000002", "name": "second account", "login": "second_login", "remembered": True, "recent": False}]

            def active_id(self): return "76561198000000001"
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.write("levels.json", {"76561198000000001": {"level": 64, "source": "Steam Community"}})
            win = MainWindow(FixtureNative(), store, start_services=False)
            win.show()
            app.processEvents()
            dpapi = protect(protect(b"ui-check"), True) == b"ui-check" if os.name == "nt" else True
            summary = {"ok": win.level.text() == "64" and win.list.count() == 2 and dpapi,
                       "font": QFontInfo(app.font()).family(), "accounts": win.list.count(),
                       "current_switch_disabled": not win.switch_button.isEnabled(), "dpapi": dpapi}
            win.list.setCurrentRow(1)
            summary["other_switch_enabled"] = win.switch_button.isEnabled()
            summary["ok"] &= summary["other_switch_enabled"] and summary["current_switch_disabled"]
            from .steamkit import helper_path
            if helper_path().exists():
                import subprocess
                from .processes import run_external
                check = run_external([str(helper_path()), '--self-test'], capture_output=True, text=True,
                                       timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                backend = json.loads(check.stdout)
                summary['steamkit'] = backend
                summary['ok'] &= check.returncode == 0 and backend.get('ok') is True
            elif hasattr(sys, '_MEIPASS'):
                summary['ok'] = False
                summary['steamkit'] = {'error': 'missing'}
            from .accounts import export_account, import_account
            import base64
            synthetic = {'steam_id': '76561198000000001', 'name': 'UI check', 'login': 'night_login',
                         'mafile': {'account_name': 'night_login', 'shared_secret': base64.b64encode(bytes(20)).decode(),
                                    'Session': {'SteamID': '76561198000000001'}}}
            archive = Path(directory) / 'synthetic-account.zip'
            export_account(archive, synthetic, 'synthetic-ui-password')
            restored = import_account(archive, 'synthetic-ui-password')
            summary['encrypted_zip'] = restored['steam_id'] == synthetic['steam_id'] and restored['mafile'] == synthetic['mafile']
            summary['ok'] &= summary['encrypted_zip']
            Path(args.check_ui).write_text(json.dumps(summary), encoding="utf-8")
            win.close()
        return 0 if summary["ok"] else 1
    # One owner for the credential vault and automatic session workers.
    from PyQt6.QtCore import QLockFile
    from PyQt6.QtWidgets import QMessageBox
    from .storage import data_dir
    data_dir().mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(data_dir() / 'app.lock'))
    if not lock.tryLock(0):
        QMessageBox.information(None, 'Steam SDA', 'Steam SDA уже запущен. Откройте существующее окно.')
        return 0
    win = MainWindow()
    win.show()
    return app.exec()
