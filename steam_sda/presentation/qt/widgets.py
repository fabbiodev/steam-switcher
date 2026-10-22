"""Accessible Qt controls with short hover transitions and local vector icons."""
from PyQt6.QtCore import Qt, QSize, QRectF, QPropertyAnimation, QEasingCurve, pyqtProperty
from PyQt6.QtGui import QColor, QPainter, QPen, QLinearGradient
from PyQt6.QtWidgets import QLabel, QPushButton, QSizePolicy, QWidget
from .icons import icon
from .motion import enabled

ACTION_ICONS = {
    'Импорт': 'import', 'Экспорт': 'export', 'Обновить список': 'refresh',
    'Открыть Steam': 'steam', 'Настройки': 'settings', 'Обновить уровни': 'refresh',
    'Переключить Steam': 'switch', 'Вернуть прежний вход': 'undo',
    'Настроить автовход': 'key', 'Обновить сессию': 'refresh',
    'Профиль Steam': 'external', 'Копировать SteamID': 'copy', 'Отмена': 'close',
    'Сохранить': 'check', 'Сохранить и войти': 'check', 'Выбрать…': 'folder',
    'Удалить аккаунт': 'trash',
}


def _mix(first, second, amount):
    a, b = QColor(first), QColor(second)
    return QColor(*(round(x + (y - x) * amount) for x, y in zip(a.getRgb()[:3], b.getRgb()[:3])))


class ActionButton(QPushButton):
    def __init__(self, text, icon_name=None):
        super().__init__(text)
        self.icon_name = icon_name
        self._hover = 0.0
        self._press = 0.0
        self.press_animation = QPropertyAnimation(self, b'pressDepth', self)
        self.press_animation.setDuration(90)
        self.press_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.pressed.connect(lambda: self._animate_press(1))
        self.released.connect(lambda: self._animate_press(0))
        self.hover_animation = QPropertyAnimation(self, b'hoverProgress', self)
        self.hover_animation.setDuration(115)
        self.hover_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(44)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    @pyqtProperty(float)
    def hoverProgress(self):
        return self._hover

    @hoverProgress.setter
    def hoverProgress(self, value):
        self._hover = value
        self.update()

    def _animate_hover(self, target):
        self.hover_animation.stop()
        if enabled():
            self.hover_animation.setStartValue(self._hover)
            self.hover_animation.setEndValue(target)
            self.hover_animation.start()
        else:
            self.hoverProgress = target

    @pyqtProperty(float)
    def pressDepth(self):
        return self._press

    @pressDepth.setter
    def pressDepth(self, value):
        self._press = value
        self.update()

    def _animate_press(self, target):
        self.press_animation.stop()
        if enabled():
            self.press_animation.setStartValue(self._press)
            self.press_animation.setEndValue(target)
            self.press_animation.start()
        else:
            self.pressDepth = target

    def enterEvent(self, event):
        self._animate_hover(1)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate_hover(0)
        super().leaveEvent(event)

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self.text()) + (54 if self.icon_name else 30), 44)

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        primary, ghost = self.objectName() == 'primary', self.property('variant') == 'ghost' or self.objectName() == 'link'
        if not self.isEnabled():
            background, bottom, border, foreground = '#243448', '#213043', '#2c4055', '#72869b'
        elif primary:
            background = _mix('#5bd3b6', '#82e4cd', self._hover)
            bottom, border, foreground = '#35ad96', '#61d1bb', '#092e2a'
        elif self.isChecked():
            background, bottom, border, foreground = '#345f65', '#294e57', '#518985', '#b4f0df'
        elif self.property('danger'):
            background = _mix('#493344', '#624151', self._hover)
            bottom, border, foreground = '#392b3d', '#735064', '#f3bdc7'
        else:
            background = _mix('#344c65' if not ghost else '#24384d', '#435f79', self._hover)
            bottom, border, foreground = '#263c52', '#425b73', '#dfebf4'
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#10202e'))
        painter.drawRoundedRect(rect.translated(0, 3), 12, 12)
        rect.translate(0, self._press * 2.5)
        painter.setPen(QPen(QColor(border), 1))
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0, QColor(background))
        gradient.setColorAt(1, QColor(bottom))
        painter.setBrush(gradient)
        painter.drawRoundedRect(rect, 12, 12)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor('#97e7d4'), 2))
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 8, 8)
        content = rect.adjusted(10, 0, -10, 0)
        icon_width = 24 if self.icon_name else 0
        text_width = painter.fontMetrics().horizontalAdvance(self.text())
        total_width = min(content.width(), text_width + icon_width)
        left = content.left() + (content.width() - total_width) / 2
        if self.icon_name:
            icon(self.icon_name, foreground).paint(painter, int(left), int(content.center().y() - 9), 18, 18)
        painter.setPen(QColor(foreground))
        text_rect = QRectF(left + icon_width, content.top(), total_width - icon_width, content.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter,
                         painter.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, max(0, int(text_rect.width()))))
        painter.end()


def label(text="", name=""):
    item = QLabel(text)
    item.setObjectName(name)
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setWordWrap(True)
    return item


def button(text, callback, primary=False, icon_name=None, ghost=False):
    item = ActionButton(text, icon_name or ACTION_ICONS.get(text.removesuffix(' ↗')))
    if primary:
        item.setObjectName("primary")
    if ghost:
        item.setProperty('variant', 'ghost')
    item.clicked.connect(callback)
    return item


def icon_tile(name, background='#24453f', foreground='#87d9bf', size=34):
    tile = QLabel()
    tile.setFixedSize(size, size)
    tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
    tile.setPixmap(icon(name, foreground).pixmap(20, 20))
    tile.setStyleSheet(f'background: {background}; border-radius: 10px;')
    return tile


def badge(text='', tone='neutral'):
    item = label(text, 'badge')
    item.setProperty('tone', tone)
    item.setWordWrap(False)
    item.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return item


class Backdrop(QWidget):
    def paintEvent(self, event):
        painter = QPainter(self)
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0, QColor('#172238'))
        gradient.setColorAt(.52, QColor('#1b3045'))
        gradient.setColorAt(1, QColor('#163b40'))
        painter.fillRect(self.rect(), gradient)
