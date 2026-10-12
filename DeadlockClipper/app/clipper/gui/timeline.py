"""Trim timeline: filmstrip with draggable start/end handles and a playhead."""
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..highlights import fmt_time

MIN_LEN, MAX_LEN = 3.0, 60.0


def fmt_precise(t):
    t = max(0.0, t)
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:04.1f}"


class Timeline(QWidget):
    """Filmstrip with draggable start/end handles and a playhead. Click anywhere to jump there."""
    seek = Signal(float)
    trimmed = Signal(float, float)
    view_changed = Signal(float, float)  # zoomed with the mouse wheel: thumbnails need re-making

    STRIP_H = 64
    HANDLE_W = 12

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(self.STRIP_H + 34)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.v0, self.v1 = 0.0, 1.0  # visible time range
        self.s0, self.s1 = 0.0, 1.0  # selection (the clip)
        self.pos_t = 0.0
        self.thumbs = []
        self._drag = None
        self.show_selection = True  # False: no clip box, the timeline is only for moving around
        self.limit = None  # video length, for mouse-wheel zoom

    def set_view(self, v0, v1):
        self.v0, self.v1 = v0, max(v1, v0 + 1)
        self.thumbs = []
        self.update()

    def set_selection(self, s0, s1):
        self.s0, self.s1 = s0, s1
        self.update()

    def set_position(self, t):
        self.pos_t = t
        self.update()

    def set_thumbs(self, pixmaps):
        self.thumbs = pixmaps
        self.update()

    def _x(self, t):
        return (t - self.v0) / (self.v1 - self.v0) * self.width()

    def _t(self, x):
        return self.v0 + min(max(x, 0), self.width()) / max(1, self.width()) * (self.v1 - self.v0)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, sh = self.width(), self.STRIP_H
        p.fillRect(self.rect(), QColor("#15171c"))
        p.fillRect(QRectF(0, 0, w, sh), QColor("#0d0e11"))
        if self.thumbs:
            tw = w / len(self.thumbs)
            for i, pix in enumerate(self.thumbs):
                p.drawPixmap(QRectF(i * tw, 0, tw + 1, sh), pix, QRectF(pix.rect()))

        if self.show_selection:
            self._paint_selection(p, w, sh)

        # time ruler
        p.setPen(QColor("#9a9ca5"))
        span = self.v1 - self.v0
        step = next((s for s in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600) if span / s <= 12), 7200)
        t = (int(self.v0 // step) + 1) * step
        while t < self.v1:
            x = self._x(t)
            p.drawLine(QPointF(x, sh), QPointF(x, sh + 5))
            p.drawText(QRectF(x - 30, sh + 6, 60, 16), Qt.AlignCenter, fmt_time(t))
            t += step
        self._paint_playhead(p, sh)
        p.end()

    def _paint_selection(self, p, w, sh):
        x0, x1 = self._x(self.s0), self._x(self.s1)
        dim = QColor(0, 0, 0, 160)
        p.fillRect(QRectF(0, 0, max(0, x0), sh), dim)
        p.fillRect(QRectF(x1, 0, max(0, w - x1), sh), dim)
        length = self.s1 - self.s0
        color = QColor("#f0b35a") if MIN_LEN <= length <= MAX_LEN else QColor("#ff6b6b")
        p.setPen(QPen(color, 3))
        p.drawRect(QRectF(x0, 1.5, x1 - x0, sh - 3))
        p.setBrush(QBrush(color))
        p.setPen(Qt.NoPen)
        hw = self.HANDLE_W
        p.drawRoundedRect(QRectF(x0 - hw, 0, hw, sh), 3, 3)
        p.drawRoundedRect(QRectF(x1, 0, hw, sh), 3, 3)
        p.setPen(QPen(QColor("#15171c"), 2))
        for hx in (x0 - hw / 2, x1 + hw / 2):
            p.drawLine(QPointF(hx, sh * 0.35), QPointF(hx, sh * 0.65))

    def _paint_playhead(self, p, sh):
        # playhead
        px = self._x(self.pos_t)
        p.setPen(QPen(QColor("white"), 2))
        p.drawLine(QPointF(px, 0), QPointF(px, sh + 4))
        p.setBrush(QColor("white"))
        p.drawEllipse(QPointF(px, sh + 4), 4, 4)

    def _hit(self, x):
        if not self.show_selection:
            return "seek"
        x0, x1 = self._x(self.s0), self._x(self.s1)
        if x0 - self.HANDLE_W - 4 <= x <= x0 + 4:
            return "start"
        if x1 - 4 <= x <= x1 + self.HANDLE_W + 4:
            return "end"
        return "seek"

    def mousePressEvent(self, e):
        self._drag = self._hit(e.position().x())
        self.mouseMoveEvent(e)

    def mouseMoveEvent(self, e):
        x = e.position().x()
        if self._drag is None:
            self.setCursor(Qt.SizeHorCursor if self._hit(x) != "seek" else Qt.PointingHandCursor)
            return
        t = self._t(x)
        if self._drag == "start":
            self.s0 = min(max(t, self.s1 - MAX_LEN, self.v0), self.s1 - MIN_LEN)
            self.trimmed.emit(self.s0, self.s1)
            self.seek.emit(self.s0)
        elif self._drag == "end":
            self.s1 = max(min(t, self.s0 + MAX_LEN, self.v1), self.s0 + MIN_LEN)
            self.trimmed.emit(self.s0, self.s1)
            self.seek.emit(self.s1)
        else:
            self.seek.emit(t)
        self.update()

    def mouseReleaseEvent(self, e):
        self._drag = None

    def wheelEvent(self, e):
        """Mouse wheel zooms the timeline in/out around the mouse pointer."""
        if not self.limit:
            return
        t = self._t(e.position().x())
        factor = 0.8 if e.angleDelta().y() > 0 else 1.25
        span = min(self.limit, max(10.0, (self.v1 - self.v0) * factor))
        frac = (t - self.v0) / max(1e-6, self.v1 - self.v0)
        v0 = min(max(0.0, t - frac * span), max(0.0, self.limit - span))
        self.set_view(v0, v0 + span)
        self.view_changed.emit(self.v0, self.v1)
