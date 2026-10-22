from steam_sda.bootstrap import create_services, resource_path, prepare_app_data_dir
import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser(description="Steam SDA")
    parser.add_argument("--check-ui", metavar="REPORT")
    args = parser.parse_args()
    if sys.platform == "win32":
        os.environ.setdefault("QT_QPA_PLATFORM", "windows:fontengine=directwrite")
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QFont, QFontDatabase, QPalette, QColor, QIcon
    app = QApplication(sys.argv)
    app.setApplicationName("Steam SDA")
    app.setOrganizationName("SteamSDA")
    app.setWindowIcon(QIcon(str(resource_path('assets/app.ico'))))
    app.setStyle("Fusion")
    from .theme import COLORS
    palette = app.palette()
    for role, color in ((QPalette.ColorRole.Window, COLORS['background']), (QPalette.ColorRole.WindowText, COLORS['text']),
                        (QPalette.ColorRole.Base, COLORS['surface']), (QPalette.ColorRole.Text, COLORS['text']),
                        (QPalette.ColorRole.Button, COLORS['raised']), (QPalette.ColorRole.ButtonText, COLORS['text']),
                        (QPalette.ColorRole.Highlight, COLORS['accent_deep']), (QPalette.ColorRole.HighlightedText, COLORS['text'])):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    font_id = QFontDatabase.addApplicationFont(str(resource_path("assets/fonts/Inter-V.ttf")))
    families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    font = QFont(families[0] if families else "Segoe UI", 10)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.PreferQuality)
    app.setFont(font)
    app.setStyleSheet(resource_path("assets/style.qss").read_text(encoding="utf-8"))
    from steam_sda.presentation.qt.window import MainWindow
    if args.check_ui:
        from steam_sda.diagnostics.ui_check import check_ui
        return check_ui(app, args.check_ui)
    # One owner for the credential vault and automatic session workers.
    from PyQt6.QtCore import QLockFile
    from PyQt6.QtWidgets import QMessageBox
    lock = QLockFile(str(prepare_app_data_dir() / 'app.lock'))
    if not lock.tryLock(0):
        QMessageBox.information(None, 'Steam SDA', 'Steam SDA уже запущен. Откройте существующее окно.')
        return 0
    win = MainWindow(create_services())
    win.show()
    return app.exec()
