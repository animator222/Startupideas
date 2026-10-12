"""Live preview of the finished clip, with direct manipulation of its layers.

Canvas view: the output exactly as it will be exported (blurred background, video layers,
text). Drag a layer to move it, drag a corner to resize it, mouse wheel to zoom a video layer.
Source view: the original video; each video layer's box marks which part of the video it shows.
"""
from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..composition import canvas_size, video_rect
from ..textrender import draw_text, text_path

LAYER_COLORS = ["#f0b35a", "#4fc3f7", "#81c784", "#e57373", "#ba68c8", "#fff176", "#4db6ac", "#ff8a65"]
HANDLE = 9  # px
SNAP = 0.012  # snap to the centre lines within this fraction of the canvas


def layer_color(i):
    return QColor(LAYER_COLORS[i % len(LAYER_COLORS)])


class CompositionCanvas(QWidget):
    selected = Signal(int)
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.setMinimumSize(320, 320)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.info = None
        self.comp = None
        self.frame = QImage()
        self._blur = None
        self.mode = "canvas"
        self.sel = -1
        self._drag = None
        self._guides = (False, False)

    # ------------------------------------------------------------ data

    def set_comp(self, info, comp):
        self.info, self.comp = info, comp
        self.sel = min(self.sel, len(comp["layers"]) - 1)
        self.update()

    def set_frame(self, img):
        self.frame = img
        self._blur = None
        self.update()

    def set_mode(self, mode):
        self.mode = mode
        if mode == "source" and (self.sel < 0 or self.comp["layers"][self.sel]["type"] != "video"):
            self.select(next((i for i, l in enumerate(self.comp["layers"]) if l["type"] == "video"), -1))
        self.update()

    def select(self, i):
        self.sel = i
        self.selected.emit(i)
        self.update()

    # ------------------------------------------------------------ geometry

    def _canvas_rect(self):
        cw, ch = canvas_size(self.comp)
        avail = QRectF(self.rect()).adjusted(16, 16, -16, -16)
        k = min(avail.width() / cw, avail.height() / ch)
        r = QRectF(0, 0, cw * k, ch * k)
        r.moveCenter(avail.center())
        return r, k

    def _source_rect(self):
        w, h = self.info.width, self.info.height
        avail = QRectF(self.rect()).adjusted(16, 16, -16, -16)
        k = min(avail.width() / w, avail.height() / h)
        r = QRectF(0, 0, w * k, h * k)
        r.moveCenter(avail.center())
        return r, k

    def _layer_box(self, i):
        """Layer bounds in canvas pixels."""
        lay = self.comp["layers"][i]
        if lay["type"] == "video":
            return QRectF(*video_rect(lay, self.info, self.comp))
        cw, ch = canvas_size(self.comp)
        if not str(lay.get("text") or "").strip():
            return QRectF(lay["cx"] * cw - 40, lay["cy"] * ch - 20, 80, 40)
        path, px = text_path(lay, cw, ch)
        pad = px * 0.15
        return path.boundingRect().adjusted(-pad, -pad, pad, pad)

    def _to_widget(self, r):
        cr, k = self._canvas_rect()
        return QRectF(cr.left() + r.left() * k, cr.top() + r.top() * k, r.width() * k, r.height() * k)

    def _src_box(self, i):
        sr, k = self._source_rect()
        x, y, w, h = self.comp["layers"][i]["src"]
        return QRectF(sr.left() + x * sr.width(), sr.top() + y * sr.height(), w * sr.width(), h * sr.height())

    @staticmethod
    def _corners(r):
        return {"tl": r.topLeft(), "tr": r.topRight(), "bl": r.bottomLeft(), "br": r.bottomRight()}

    # ------------------------------------------------------------ painting

    def _blurred(self, cw, ch):
        if self._blur is None and not self.frame.isNull():
            small = self.frame.scaled(max(8, cw // 20), max(8, ch // 20), Qt.KeepAspectRatioByExpanding,
                                      Qt.SmoothTransformation)
            tiny = small.scaled(max(4, small.width() // 2), max(4, small.height() // 2), Qt.IgnoreAspectRatio,
                                Qt.SmoothTransformation)
            self._blur = tiny
        return self._blur

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor("#0b0c10"))
        if not self.comp or not self.info:
            return
        if self.mode == "source":
            self._paint_source(p)
        else:
            self._paint_canvas(p)
        p.end()

    def _paint_canvas(self, p):
        cw, ch = canvas_size(self.comp)
        cr, k = self._canvas_rect()
        p.save()
        p.setClipRect(cr)
        p.translate(cr.topLeft())
        p.scale(k, k)
        blur = self._blurred(cw, ch)
        if blur is not None:
            # Fill the canvas with the blurred frame (cropped to cover), slightly darkened.
            scale = max(cw / blur.width(), ch / blur.height())
            bw, bh = blur.width() * scale, blur.height() * scale
            p.drawImage(QRectF((cw - bw) / 2, (ch - bh) / 2, bw, bh), blur)
            p.fillRect(QRectF(0, 0, cw, ch), QColor(0, 0, 0, 40))
        else:
            g = QLinearGradient(0, 0, 0, ch)
            g.setColorAt(0, QColor("#1c1f2b"))
            g.setColorAt(1, QColor("#0f1117"))
            p.fillRect(QRectF(0, 0, cw, ch), g)
        fw, fh = self.frame.width(), self.frame.height()
        for lay in self.comp["layers"]:
            if lay.get("hidden"):
                continue
            if lay["type"] == "video":
                r = QRectF(*video_rect(lay, self.info, self.comp))
                if not self.frame.isNull():
                    sx, sy, sw, sh = lay["src"]
                    p.drawImage(r, self.frame, QRectF(sx * fw, sy * fh, sw * fw, sh * fh))
                else:
                    p.fillRect(r, QColor(255, 255, 255, 20))
            else:
                draw_text(p, lay, cw, ch)
        p.restore()

        # Canvas frame and guides
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(cr)
        gx, gy = self._guides
        p.setPen(QPen(QColor("#ff4fd8"), 1, Qt.DashLine))
        if gx:
            p.drawLine(QPointF(cr.center().x(), cr.top()), QPointF(cr.center().x(), cr.bottom()))
        if gy:
            p.drawLine(QPointF(cr.left(), cr.center().y()), QPointF(cr.right(), cr.center().y()))

        if 0 <= self.sel < len(self.comp["layers"]) and not self.comp["layers"][self.sel].get("hidden"):
            r = self._to_widget(self._layer_box(self.sel))
            self._paint_selection(p, r, layer_color(self.sel), self.comp["layers"][self.sel]["name"])

    def _paint_selection(self, p, r, color, name):
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(color, 2, Qt.DashLine))
        p.drawRect(r)
        p.setPen(QPen(QColor("#0b0c10"), 1))
        p.setBrush(color)
        for c in self._corners(r).values():
            p.drawRoundedRect(QRectF(c.x() - HANDLE / 2, c.y() - HANDLE / 2, HANDLE, HANDLE), 2, 2)
        tag = QRectF(r.left(), r.top() - 22, max(60.0, p.fontMetrics().horizontalAdvance(name) + 14), 20)
        p.fillRect(tag, color)
        p.setPen(QColor("#0b0c10"))
        p.drawText(tag, Qt.AlignCenter, name)

    def _paint_source(self, p):
        sr, _ = self._source_rect()
        if not self.frame.isNull():
            p.drawImage(sr, self.frame)
        p.fillRect(sr, QColor(0, 0, 0, 90))
        for i, lay in enumerate(self.comp["layers"]):
            if lay["type"] != "video":
                continue
            r = self._src_box(i)
            color = layer_color(i)
            if not self.frame.isNull():
                x, y, w, h = lay["src"]
                fw, fh = self.frame.width(), self.frame.height()
                p.drawImage(r, self.frame, QRectF(x * fw, y * fh, w * fw, h * fh))  # undimmed inside
            if i == self.sel:
                self._paint_selection(p, r, color, lay["name"])
            else:
                p.setPen(QPen(color, 2))
                p.setBrush(Qt.NoBrush)
                p.drawRect(r)
                p.setPen(color)
                p.drawText(r.adjusted(6, 4, 0, 0), Qt.AlignLeft | Qt.AlignTop, lay["name"])

    # ------------------------------------------------------------ mouse

    def _hit_corner(self, r, pos):
        for name, c in self._corners(r).items():
            if abs(pos.x() - c.x()) <= HANDLE and abs(pos.y() - c.y()) <= HANDLE:
                return name
        return None

    def mousePressEvent(self, e):
        if not self.comp:
            return
        pos = e.position()
        if self.mode == "source":
            self._press_source(pos)
            return
        if 0 <= self.sel < len(self.comp["layers"]):
            r = self._to_widget(self._layer_box(self.sel))
            corner = self._hit_corner(r, pos)
            if corner:
                self._drag = ("resize", corner, pos, self._snapshot(self.sel), r)
                return
        for i in reversed(range(len(self.comp["layers"]))):  # top-most first
            if not self.comp["layers"][i].get("hidden") and self._to_widget(self._layer_box(i)).contains(pos):
                if i != self.sel:
                    self.select(i)
                self._drag = ("move", None, pos, self._snapshot(i), None)
                return
        self._drag = None

    def _snapshot(self, i):
        import copy
        return copy.deepcopy(self.comp["layers"][i])

    def _press_source(self, pos):
        if 0 <= self.sel < len(self.comp["layers"]) and self.comp["layers"][self.sel]["type"] == "video":
            r = self._src_box(self.sel)
            corner = self._hit_corner(r, pos)
            if corner:
                self._drag = ("src-resize", corner, pos, self._snapshot(self.sel), r)
                return
        for i in reversed(range(len(self.comp["layers"]))):
            if self.comp["layers"][i]["type"] == "video" and self._src_box(i).contains(pos):
                if i != self.sel:
                    self.select(i)
                self._drag = ("src-move", None, pos, self._snapshot(i), self._src_box(i))
                return
        self._drag = None

    def mouseMoveEvent(self, e):
        pos = e.position()
        if not self._drag:
            self._hover_cursor(pos)
            return
        kind, corner, start, orig, r0 = self._drag
        lay = self.comp["layers"][self.sel]
        if kind.startswith("src"):
            self._drag_source(kind, corner, start, orig, r0, pos, lay)
        else:
            self._drag_canvas(kind, corner, start, orig, r0, pos, lay)
        self.update()
        self.changed.emit()

    def _drag_canvas(self, kind, corner, start, orig, r0, pos, lay):
        cw, ch = canvas_size(self.comp)
        _, k = self._canvas_rect()
        dx, dy = (pos.x() - start.x()) / k / cw, (pos.y() - start.y()) / k / ch
        if kind == "move":
            key = ("dst", 0, 1) if lay["type"] == "video" else (None, "cx", "cy")
            if lay["type"] == "video":
                cx, cy = orig["dst"][0] + dx, orig["dst"][1] + dy
            else:
                cx, cy = orig["cx"] + dx, orig["cy"] + dy
            snap_x, snap_y = abs(cx - 0.5) < SNAP, abs(cy - 0.5) < SNAP
            cx, cy = (0.5 if snap_x else cx), (0.5 if snap_y else cy)
            self._guides = (snap_x, snap_y)
            if key[0]:
                lay["dst"] = [cx, cy, lay["dst"][2]]
            else:
                lay["cx"], lay["cy"] = cx, cy
            return
        # Resize from a corner; the opposite corner stays put, the shape is kept.
        opp = {"tl": r0.bottomRight(), "tr": r0.bottomLeft(), "bl": r0.topRight(), "br": r0.topLeft()}[corner]
        scale = max(abs(pos.x() - opp.x()) / max(1.0, r0.width()), abs(pos.y() - opp.y()) / max(1.0, r0.height()))
        scale = max(0.05, scale)
        new_w, new_h = r0.width() * scale, r0.height() * scale
        sx = -1 if corner in ("tl", "bl") else 1
        sy = -1 if corner in ("tl", "tr") else 1
        center = QPointF(opp.x() + sx * new_w / 2, opp.y() + sy * new_h / 2)
        cr, _ = self._canvas_rect()
        cx, cy = (center.x() - cr.left()) / k / cw, (center.y() - cr.top()) / k / ch
        if lay["type"] == "video":
            lay["dst"] = [cx, cy, max(0.03, orig["dst"][2] * scale)]
        else:
            lay["size"] = max(0.008, min(0.4, orig["size"] * scale))
            lay["cx"], lay["cy"] = cx, cy

    def _drag_source(self, kind, corner, start, orig, r0, pos, lay):
        sr, _ = self._source_rect()
        x, y, w, h = orig["src"]
        if kind == "src-move":
            nx = min(max(0.0, x + (pos.x() - start.x()) / sr.width()), 1 - w)
            ny = min(max(0.0, y + (pos.y() - start.y()) / sr.height()), 1 - h)
            lay["src"] = [nx, ny, w, h]
            return
        px = min(max(0.0, (pos.x() - sr.left()) / sr.width()), 1.0)
        py = min(max(0.0, (pos.y() - sr.top()) / sr.height()), 1.0)
        x0, y0, x1, y1 = x, y, x + w, y + h
        if "l" in corner:
            x0 = min(px, x1 - 0.02)
        else:
            x1 = max(px, x0 + 0.02)
        if "t" in corner:
            y0 = min(py, y1 - 0.02)
        else:
            y1 = max(py, y0 + 0.02)
        # Keep the layer's size on the canvas roughly the same when its source area changes.
        lay["src"] = [x0, y0, x1 - x0, y1 - y0]
        lay["dst"] = [lay["dst"][0], lay["dst"][1], orig["dst"][2] * (x1 - x0) / max(1e-6, w)]

    def mouseReleaseEvent(self, e):
        self._drag = None
        self._guides = (False, False)
        self.update()

    def wheelEvent(self, e):
        """Mouse wheel over the selected video layer zooms it around its centre."""
        if self.mode != "canvas" or not (0 <= self.sel < len(self.comp["layers"])):
            return
        lay = self.comp["layers"][self.sel]
        if lay["type"] != "video":
            return
        factor = 1.05 if e.angleDelta().y() > 0 else 1 / 1.05
        lay["dst"] = [lay["dst"][0], lay["dst"][1], max(0.03, min(6.0, lay["dst"][2] * factor))]
        self.update()
        self.changed.emit()

    def _hover_cursor(self, pos):
        if not self.comp:
            return
        if 0 <= self.sel < len(self.comp["layers"]):
            r = self._src_box(self.sel) if self.mode == "source" and self.comp["layers"][self.sel]["type"] == "video" \
                else self._to_widget(self._layer_box(self.sel))
            corner = self._hit_corner(r, pos)
            if corner:
                self.setCursor(Qt.SizeFDiagCursor if corner in ("tl", "br") else Qt.SizeBDiagCursor)
                return
            if r.contains(pos):
                self.setCursor(Qt.SizeAllCursor)
                return
        self.setCursor(Qt.ArrowCursor)

    def sizeHint(self):
        return QSizeF(600, 700).toSize()
