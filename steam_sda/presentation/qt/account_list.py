"""Paint account rows directly: inexpensive avatars and no per-row widget tree."""
import hashlib
from PyQt6.QtCore import Qt, QSize, QRectF
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QStyledItemDelegate, QStyle

PAIRS = (('#293c64', '#b8c9ff'), ('#234b43', '#a4e5ce'), ('#4b3b2c', '#f1cf94'),
         ('#423354', '#d8b9f2'), ('#244153', '#a8d8ef'))


def avatar_colors(identity):
    return PAIRS[hashlib.sha256(identity.encode()).digest()[0] % len(PAIRS)]


class AccountDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(220, 74)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        row = index.data(Qt.ItemDataRole.UserRole + 1) or {}
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        rect = QRectF(option.rect).adjusted(1, 3, -1, -3)
        painter.setPen(Qt.PenStyle.NoPen)
        if selected or hovered:
            painter.setBrush(QColor('#294557' if selected else '#24374d'))
            painter.drawRoundedRect(rect, 12, 12)
        if selected:
            painter.setBrush(QColor('#72d6b9'))
            painter.drawRoundedRect(QRectF(rect.left(), rect.top() + 20, 3, 28), 1.5, 1.5)
        background, foreground = avatar_colors(row.get('steam_id', ''))
        avatar = QRectF(rect.left() + 12, rect.top() + 14, 38, 38)
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(avatar, 12, 12)
        font = QFont(option.font); font.setPixelSize(15); font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font); painter.setPen(QColor(foreground))
        painter.drawText(avatar, Qt.AlignmentFlag.AlignCenter, (row.get('name') or '?')[:2].upper())
        x, width = avatar.right() + 12, max(20, rect.width() - 76)
        font.setPixelSize(13); font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font); painter.setPen(QColor('#e7eff7'))
        painter.drawText(QRectF(x, rect.top() + 15, width, 20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(row.get('name', ''), Qt.TextElideMode.ElideRight, int(width)))
        font.setPixelSize(11); font.setWeight(QFont.Weight.Normal)
        painter.setFont(font); painter.setPen(QColor('#94a8bd'))
        text = row.get('login', '') + (' · открыт' if row.get('active') else '')
        painter.drawText(QRectF(x, rect.top() + 36, width, 17), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, int(width)))
        if row.get('active'):
            painter.setBrush(QColor('#64d4aa')); painter.setPen(QPen(QColor('#1c2c40'), 2))
            painter.drawEllipse(QRectF(avatar.right() - 5, avatar.bottom() - 5, 9, 9))
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setPen(QPen(QColor('#92baa9'), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 10, 10)
        painter.restore()
