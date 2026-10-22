"""Accessible Qt controls with short hover transitions and local vector icons."""
from PyQt6.QtCore import Qt, QSize, QRectF, QPropertyAnimation, QEasingCurve, pyqtProperty
from PyQt6.QtGui import QColor, QPainter, QPen, QLinearGradient
from PyQt6.QtWidgets import QLabel, QPushButton, QSizePolicy, QWidget, QLineEdit
from .icons import icon
from .motion import enabled
from .theme import COLORS

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
            background, bottom, border, foreground = '#24212d', '#211e29', '#34303f', '#81778e'
        elif primary:
            background = _mix(COLORS['action'], '#ffdabd', self._hover)
            bottom, border, foreground = '#d6a17c', '#f6cbaa', COLORS['action_text']
        elif self.isChecked():
            background, bottom, border, foreground = _mix('#403354', '#57416f', self._hover), '#342b46', '#776197', '#e1d4ff'
        elif self.property('danger'):
            background = _mix('#432731', '#57313e', self._hover)
            bottom, border, foreground = '#36222d', '#7c4c5d', '#f6bfca'
        else:
            background = _mix('#34303f' if not ghost else '#221f2d', '#463c56', self._hover)
            bottom, border, foreground = '#28232f', '#4c425c', COLORS['text']
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#0c0a11'))
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
            painter.setPen(QPen(QColor(COLORS['accent']), 2))
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


class AnimatedLineEdit(QLineEdit):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._glow = 0.0
        self.glow_animation = QPropertyAnimation(self, b'glowProgress', self)
        self.glow_animation.setDuration(140)
        self.glow_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    @pyqtProperty(float)
    def glowProgress(self):
        return self._glow

    @glowProgress.setter
    def glowProgress(self, value):
        self._glow = value
        self.update()

    def _animate_glow(self, amount):
        self.glow_animation.stop()
        if enabled():
            self.glow_animation.setStartValue(self._glow)
            self.glow_animation.setEndValue(amount)
            self.glow_animation.start()
        else:
            self.glowProgress = amount

    def enterEvent(self, event):
        self._animate_glow(1.0 if self.hasFocus() else .6)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate_glow(1.0 if self.hasFocus() else 0.0)
        super().leaveEvent(event)

    def focusInEvent(self, event):
        self._animate_glow(1.0)
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self._animate_glow(.6 if self.underMouse() else 0.0)
        super().focusOutEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._glow > .001 and self.isEnabled():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            color = QColor(COLORS['accent'])
            color.setAlphaF(self._glow * .85)
            painter.setPen(QPen(color, 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 11, 11)


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


def icon_tile(name, background='#342b48', foreground='#c9b7ff', size=34):
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
        gradient.setColorAt(0, QColor('#111017'))
        gradient.setColorAt(.55, QColor('#201a2e'))
        gradient.setColorAt(1, QColor('#32222e'))
        painter.fillRect(self.rect(), gradient)
