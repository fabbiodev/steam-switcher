"""Small local vector icons; no network requests or icon font dependency."""
from functools import lru_cache
from PyQt6.QtCore import QByteArray, QRectF, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

GLYPHS = {
    'switch': '<path d="M4 7h15m-4-4 4 4-4 4M20 17H5m4-4-4 4 4 4"/>',
    'search': '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/>',
    'plus': '<path d="M12 5v14M5 12h14"/>',
    'import': '<path d="M12 3v12m-4-4 4 4 4-4M4 15v5h16v-5"/>',
    'export': '<path d="M12 15V3m-4 4 4-4 4 4M4 15v5h16v-5"/>',
    'refresh': '<path d="M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5M4 16a8 8 0 0 0 14 3l3-3m0 5v-5h-5"/>',
    'settings': '<path d="m9 3-.7 2.4-2.4 1-2.2-.6-1.5 2.6 1.6 1.8-.3 2.6L2 15l1.5 2.6 2.4-.4 2.1 1.6.8 2.2h3l1-2.2 2.5-.8 2.1.7 1.5-2.6-1.4-2 .2-2.5 1.8-1.8L20 7.2l-2.5.2-2.1-1.5L14 3Z"/><circle cx="11" cy="12" r="3"/>',
    'steam': '<circle cx="16.5" cy="7.5" r="4"/><circle cx="16.5" cy="7.5" r="1.5"/><path d="m13.5 10.5-5 6m3-1L3 11"/><circle cx="7" cy="17" r="3"/>',
    'external': '<path d="M14 3h7v7M21 3 11 13M10 5H4v15h15v-6"/>',
    'copy': '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
    'check': '<path d="m5 12 4 4L19 6"/>',
    'shield': '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
    'key': '<circle cx="8" cy="9" r="5"/><path d="m12 13 8 8m-4-4 3-3m-6 0 3-3"/>',
    'clock': '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    'chevron': '<path d="m9 5 7 7-7 7"/>',
    'down': '<path d="m6 9 6 6 6-6"/>',
    'info': '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
    'alert': '<path d="m12 3 10 18H2Z M12 9v4m0 4h.01"/>',
    'close': '<path d="m6 6 12 12M18 6 6 18"/>',
    'grid': '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    'activity': '<path d="M2 12h5l3-8 4 16 3-8h5"/>',
    'user': '<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
    'folder': '<path d="M3 6h7l2 3h9v11H3Z"/>',
    'lock': '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/>',
    'file': '<path d="M14 3H5v18h14V8Zm0 0v5h5M8 12h8m-8 4h6"/>',
    'monitor': '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M12 17v4m-5 0h10"/>',
    'spark': '<path d="m12 2 2.8 7.2L22 12l-7.2 2.8L12 22l-2.8-7.2L2 12l7.2-2.8Z"/>',
    'undo': '<path d="m8 4-5 5 5 5M3 9h11a7 7 0 0 1 0 14"/>',
    'trash': '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
}


@lru_cache(maxsize=160)
def icon(name, color='#b5a8c8', size=20):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
           f'stroke="{color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'
           + GLYPHS.get(name, GLYPHS['info']) + '</svg>')
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, size * 2, size * 2))
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)
