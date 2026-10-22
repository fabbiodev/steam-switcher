"""Short, interruptible Qt animations. No loops or background animation timer."""
from PyQt6.QtCore import QEasingCurve, QPropertyAnimation
from PyQt6.QtWidgets import QApplication, QGraphicsOpacityEffect, QStackedWidget


def enabled():
    app = QApplication.instance()
    return app is not None and app.property('animationsEnabled') is not False


class TransitionStack(QStackedWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.effect = QGraphicsOpacityEffect(self)
        self.effect.setOpacity(1)
        self.setGraphicsEffect(self.effect)
        self.animation = QPropertyAnimation(self.effect, b'opacity', self)
        self.animation.setDuration(170)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def reveal(self):
        self.animation.stop()
        if enabled() and self.isVisible():
            self.animation.setStartValue(0.3)
            self.animation.setEndValue(1)
            self.animation.start()
        else:
            self.effect.setOpacity(1)

    def go(self, index):
        if index != self.currentIndex():
            self.setCurrentIndex(index)
            self.updateGeometry()
            self.reveal()

    def sizeHint(self):
        page = self.currentWidget()
        return page.sizeHint() if page is not None else super().sizeHint()

    def minimumSizeHint(self):
        page = self.currentWidget()
        return page.minimumSizeHint() if page is not None else super().minimumSizeHint()
