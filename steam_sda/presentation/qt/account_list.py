"""Paint account rows directly: inexpensive avatars and no per-row widget tree."""
import hashlib
from PyQt6.QtCore import Qt, QSize, QRectF, QPropertyAnimation, QVariantAnimation, QEasingCurve, pyqtProperty, QBuffer, QIODevice, QByteArray
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QCursor, QPixmap, QImageReader, QPainterPath
from PyQt6.QtWidgets import QStyledItemDelegate, QStyle, QListWidget, QAbstractItemView
from .motion import enabled
from .theme import COLORS

PAIRS = (('#3d3259', '#d6c0ff'), ('#37483f', '#bde4c8'), ('#513c32', '#f5cfac'),
         ('#4b3147', '#efbddd'), ('#343c55', '#c5cfff'))


def avatar_colors(identity):
    return PAIRS[hashlib.sha256(identity.encode()).digest()[0] % len(PAIRS)]


def decode_avatar(data):
    if not isinstance(data, bytes) or len(data) > 512 * 1024:
        return QPixmap()
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    size = reader.size()
    if reader.format().data() not in (b'png', b'jpeg', b'jpg') or not 0 < size.width() <= 512 or not 0 < size.height() <= 512:
        return QPixmap()
    image = reader.read()
    if image.isNull():
        return QPixmap()
    image = image.scaled(132, 132, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    return QPixmap.fromImage(image.copy((image.width() - 132) // 2, (image.height() - 132) // 2, 132, 132))


def rounded_avatar(pixmap, size=66, radius=20):
    result = QPixmap(size * 2, size * 2)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size * 2, size * 2), radius * 2, radius * 2)
    painter.setClipPath(path)
    painter.drawPixmap(result.rect(), pixmap)
    painter.end()
    result.setDevicePixelRatio(2)
    return result


class AnimatedAccountList(QListWidget):
    """Animate painting and wheel scrolling without deferring selection events."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._reveal = 1.0
        self.reveal_animation = QPropertyAnimation(self, b'revealProgress', self)
        self.reveal_animation.setDuration(240)
        self.reveal_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.scroll_animation = QPropertyAnimation(self.verticalScrollBar(), b'value', self)
        self.scroll_animation.setDuration(170)
        self.scroll_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setItemDelegate(AccountDelegate(self))
        self.setAccessibleName('Список аккаунтов Steam')
        self.avatars = {}
        self._hovered = -1
        self._hover_levels = {}
        self._hover_start = {}
        self.hover_animation = QVariantAnimation(self)
        self.hover_animation.setDuration(140)
        self.hover_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.hover_animation.valueChanged.connect(self._paint_hover)
        self.verticalScrollBar().valueChanged.connect(self._retarget_hover)

    def _paint_hover(self, progress):
        self._hover_levels = {row: start + ((1.0 if row == self._hovered else 0.0) - start) * progress
                              for row, start in self._hover_start.items()}
        self.viewport().update()

    def set_hover_row(self, row):
        if row == self._hovered:
            return
        self.hover_animation.stop()
        self._hovered = row
        self._hover_start = {index: value for index, value in self._hover_levels.items() if value > .001}
        if row >= 0:
            self._hover_start.setdefault(row, 0.0)
        if enabled() and self.isVisible():
            self.hover_animation.setStartValue(0.0)
            self.hover_animation.setEndValue(1.0)
            self.hover_animation.start()
        else:
            self._paint_hover(1.0)

    def hover_amount(self, row):
        return self._hover_levels.get(row, 0.0)

    def _retarget_hover(self):
        row = self.indexAt(self.viewport().mapFromGlobal(QCursor.pos())).row() if self.viewport().underMouse() else -1
        self.set_hover_row(row)

    def mouseMoveEvent(self, event):
        self.set_hover_row(self.indexAt(event.position().toPoint()).row())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.set_hover_row(-1)
        super().leaveEvent(event)

    @pyqtProperty(float)
    def revealProgress(self):
        return self._reveal

    @revealProgress.setter
    def revealProgress(self, value):
        self._reveal = value
        self.viewport().update()

    def reveal(self):
        self.scroll_animation.stop()
        self.reveal_animation.stop()
        self.hover_animation.stop()
        self._hovered, self._hover_levels = -1, {}
        if enabled() and self.isVisible() and self.count():
            self.reveal_animation.setStartValue(0.0)
            self.reveal_animation.setEndValue(1.0)
            self.reveal_animation.start()
        else:
            self.revealProgress = 1.0

    def smooth_scroll(self, distance):
        bar = self.verticalScrollBar()
        running = self.scroll_animation.state() == QPropertyAnimation.State.Running
        previous = self.scroll_animation.endValue() if running else bar.value()
        target = min(bar.maximum(), max(bar.minimum(), int(previous + distance)))
        self.scroll_animation.stop()
        if enabled() and self.isVisible():
            self.scroll_animation.setStartValue(bar.value())
            self.scroll_animation.setEndValue(target)
            self.scroll_animation.start()
        else:
            bar.setValue(target)

    def wheelEvent(self, event):
        if event.pixelDelta().y():
            self.scroll_animation.stop()
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - event.pixelDelta().y())
            event.accept()
        elif event.angleDelta().y() and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            self.smooth_scroll(-event.angleDelta().y() / 120 * 160)
            event.accept()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event):
        self.scroll_animation.stop()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        self.scroll_animation.stop()
        super().keyPressEvent(event)

    def hideEvent(self, event):
        self.reveal_animation.stop()
        self.scroll_animation.stop()
        self.hover_animation.stop()
        self._hovered, self._hover_levels = -1, {}
        self.revealProgress = 1.0
        super().hideEvent(event)


class AccountDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(240, 72)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        view = self.parent()
        progress = getattr(view, 'revealProgress', 1.0)
        if progress < 1:
            progress = max(0.0, min(1.0, progress * 1.28 - min(index.row(), 5) * .05))
            painter.setOpacity(progress)
            painter.translate((1 - progress) * 20, 0)
        row = index.data(Qt.ItemDataRole.UserRole + 1) or {}
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = view.hover_amount(index.row()) if hasattr(view, 'hover_amount') else float(bool(option.state & QStyle.StateFlag.State_MouseOver))
        rect = QRectF(option.rect).adjusted(1, 3, -1, -3)
        painter.setPen(Qt.PenStyle.NoPen)
        if selected or hover > .001:
            fill = QColor('#382c4a' if selected else '#28212f')
            if not selected:
                fill.setAlphaF(hover)
            painter.setBrush(fill)
            painter.setPen(QPen(QColor('#6b5584' if selected else '#44364e'), 1))
            painter.drawRoundedRect(rect, 14, 14)
        painter.setPen(Qt.PenStyle.NoPen)
        if selected:
            painter.setBrush(QColor(COLORS['accent']))
            painter.drawRoundedRect(QRectF(rect.left(), rect.top() + 19, 3, 26), 1.5, 1.5)
        background, foreground = avatar_colors(row.get('steam_id', ''))
        avatar = QRectF(rect.left() + 10, rect.top() + 15, 36, 36)
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(avatar, 10, 10)
        font = QFont(option.font); font.setPixelSize(15); font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font); painter.setPen(QColor(foreground))
        pixmap = view.avatars.get(row.get('steam_id')) if hasattr(view, 'avatars') else None
        if pixmap is not None and not pixmap.isNull():
            painter.save()
            path = QPainterPath()
            path.addRoundedRect(avatar, 10, 10)
            painter.setClipPath(path)
            painter.drawPixmap(avatar.toRect(), pixmap)
            painter.restore()
        else:
            painter.drawText(avatar, Qt.AlignmentFlag.AlignCenter, (row.get('name') or '?')[:2].upper())
        x, width = avatar.right() + 10, max(20, rect.width() - 68)
        level = row.get('level')
        level_text = f'LVL {level}' if level is not None else 'LVL —'
        font.setPixelSize(9)
        painter.setFont(font)
        badge_width = max(44, painter.fontMetrics().horizontalAdvance(level_text) + 10)
        badge = QRectF(rect.right() - badge_width - 5, rect.top() + 8, badge_width, 17)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#342b48'))
        painter.drawRoundedRect(badge, 5, 5)
        font.setPixelSize(9)
        painter.setFont(font)
        painter.setPen(QColor(COLORS['accent'] if level is not None else COLORS['muted']))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, level_text)
        font.setPixelSize(13); font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font); painter.setPen(QColor(COLORS['text']))
        name_width = max(10, width - badge_width - 8)
        painter.drawText(QRectF(x, rect.top() + 5, name_width, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(row.get('name', ''), Qt.TextElideMode.ElideRight, int(name_width)))
        font.setPixelSize(11); font.setWeight(QFont.Weight.Normal)
        painter.setFont(font); painter.setPen(QColor(COLORS['muted']))
        text = row.get('login', '') + (' · в Steam' if row.get('active') else '')
        painter.drawText(QRectF(x, rect.top() + 25, width, 17), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, int(width)))
        font.setPixelSize(9)
        painter.setFont(font)
        painter.setPen(QColor(COLORS['success'] if row.get('session_saved') else COLORS['action'] if row.get('temporary_session') else COLORS['muted']))
        painter.drawText(QRectF(x, rect.top() + 44, width, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(row.get('session_label', ''), Qt.TextElideMode.ElideRight, int(width)))
        if row.get('active'):
            painter.setBrush(QColor(COLORS['success'])); painter.setPen(QPen(QColor('#1b1623'), 2))
            painter.drawEllipse(QRectF(avatar.right() - 5, avatar.bottom() - 5, 9, 9))
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setPen(QPen(QColor(COLORS['accent']), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 10, 10)
        painter.restore()
