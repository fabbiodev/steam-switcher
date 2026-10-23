"""Render the real UI with synthetic accounts; does not read Steam or sessions."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tests'))
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont, QFontDatabase, QImage, QPainter, QColor, QPolygonF
from PyQt6.QtCore import QBuffer, QIODevice, QPointF
from steam_sda.bootstrap import create_services
from steam_sda.presentation.qt.window import MainWindow
from test_core import MemoryStore, ONE, TWO
from test_ui import NativeFixture
from test_accounts_auth import record
from test_hwid import profile
from steam_sda.infrastructure.persistence.vault import AccountVault
from steam_sda.presentation.qt.dialogs import DeleteAccountDialog
from steam_sda.presentation.qt.update_dialog import UpdateDialog
from steam_sda.domain.updates import UpdateRelease, RELEASES_URL

root = Path(__file__).resolve().parent.parent
app = QApplication([])
app.setStyle('Fusion')
fid = QFontDatabase.addApplicationFont(str(root/'assets/fonts/Inter-V.ttf'))
app.setFont(QFont(QFontDatabase.applicationFontFamilies(fid)[0], 10))
app.setProperty('animationsEnabled', False)
app.setStyleSheet((root/'assets/style.qss').read_text())
store = MemoryStore()
store.write('levels.json', {ONE: {'level':64,'source':'Steam Community','updated_at':time.time()},
                            TWO: {'level':23,'source':'Steam Web API','updated_at':time.time()}})
AccountVault(store).save({**record(TWO, 'second_login'), 'name': 'second account', 'hwid': profile(TWO), 'session_origin': 'steam_temporary'})
win = MainWindow(create_services(NativeFixture(), store, enable_devices=False), start_services=False)
for sid, sky, land, sun in ((ONE, '#273357', '#7885b5', '#f0d9a7'), (TWO, '#96beb8', '#3b6c64', '#f0c1a2')):
    image = QImage(96, 96, QImage.Format.Format_RGB32)
    image.fill(QColor(sky))
    painter = QPainter(image)
    painter.setPen(QColor(sun)); painter.setBrush(QColor(sun)); painter.drawEllipse(57, 14, 22, 22)
    painter.setPen(QColor(land)); painter.setBrush(QColor(land))
    painter.drawPolygon(QPolygonF([QPointF(0, 96), QPointF(35, 34), QPointF(74, 96)]))
    painter.drawPolygon(QPolygonF([QPointF(34, 96), QPointF(74, 45), QPointF(96, 72), QPointF(96, 96)]))
    painter.end()
    buffer = QBuffer(); buffer.open(QIODevice.OpenModeFlag.WriteOnly); image.save(buffer, 'PNG')
    win._avatar_result(win.generation, sid, bytes(buffer.data()))
win.account_rows[0].update(name='nightshift',login='night_login')
win.account_rows[1].update(name='second account',login='second_login')
win._list()
win.show(); app.processEvents()
out = root/'build/previews';out.mkdir(parents=True,exist_ok=True)
win.grab().save(str(out/'current.png'))
win.list.setCurrentRow(1);app.processEvents();win.grab().save(str(out/'switch.png'))
win.details_tab.click();app.processEvents();win.grab().save(str(out/'details.png'))
dialog = DeleteAccountDialog(win.selected, win);dialog.show();app.processEvents();dialog.grab().save(str(out/'delete.png'));dialog.close()
win.overview_tab.click()
win.resize(860,570);app.processEvents();win.grab().save(str(out/'minimum.png'))
update = UpdateRelease('0.7.3', f'{RELEASES_URL}/download/v0.7.3/SteamSwitcher-v0.7.3-Setup-x64.exe', 'a' * 64, 100)
dialog = UpdateDialog(win.services.updates, update, win)
dialog.show();app.processEvents();dialog.grab().save(str(out/'updates.png'));dialog.close()
win.close()
print('Synthetic previews rendered.')
