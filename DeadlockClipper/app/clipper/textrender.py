"""Draws text layers. The same code paints the live preview and the PNGs used for export,
so the exported clip looks exactly like the editor."""
import logging
import math
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QFontMetricsF, QImage, QPainter, QPainterPath, QPen,
)

from .paths import APP_ROOT

log = logging.getLogger(__name__)

FONT_DIR = APP_ROOT / "app" / "fonts"
# Good-looking system fonts offered too, if this PC has them.
SYSTEM_FONTS = ["Impact", "Arial Black", "Segoe UI Black", "Bahnschrift", "Arial", "Verdana"]
_loaded = None


def font_families():
    """Bundled fonts first, then useful system fonts that are installed."""
    global _loaded
    if _loaded is None:
        bundled = []
        for f in sorted(Path(FONT_DIR).glob("*.ttf")):
            fid = QFontDatabase.addApplicationFont(str(f))
            if fid >= 0:
                for fam in QFontDatabase.applicationFontFamilies(fid):
                    if fam not in bundled and "Thin" not in fam:
                        bundled.append(fam)
            else:
                log.warning("Could not load font %s", f)
        installed = set(QFontDatabase.families())
        _loaded = bundled + [f for f in SYSTEM_FONTS if f in installed and f not in bundled]
        log.info("Fonts available: %s", _loaded)
    return _loaded


def _font(layer, px):
    font_families()
    font = QFont(layer.get("font") or "Anton")
    font.setPixelSize(max(4, int(px)))
    if layer.get("bold"):
        font.setWeight(QFont.Black)
    if hasattr(font, "setVariableAxis"):  # variable fonts (Montserrat, Oswald): pick the weight
        font.setVariableAxis(QFont.Tag("wght"), 850.0 if layer.get("bold") else 600.0)
    font.setHintingPreference(QFont.PreferNoHinting)
    return font


def text_path(layer, cw, ch):
    """The text outline, centred at the layer's position, in canvas pixels."""
    px = layer.get("size", 0.06) * max(cw, ch)  # size is a fraction of the canvas's long side
    font = _font(layer, px)
    fm = QFontMetricsF(font)
    lines = str(layer.get("text") or "").split("\n") or [""]
    path = QPainterPath()
    path.setFillRule(Qt.WindingFill)  # variable fonts have overlapping outlines
    y = 0.0
    for line in lines:
        w = fm.horizontalAdvance(line)
        path.addText(QPointF(-w / 2, y + fm.ascent()), font, line)
        y += fm.lineSpacing()
    box = path.boundingRect()
    if box.isEmpty():
        box = QRectF(-px, 0, 2 * px, px)
    path.translate(layer.get("cx", 0.5) * cw - box.center().x(), layer.get("cy", 0.5) * ch - box.center().y())
    return path, px


def draw_text(p, layer, cw, ch):
    """Paint one text layer on a painter that works in canvas pixels. Returns its bounding box."""
    if not str(layer.get("text") or "").strip():
        return QRectF()
    path, px = text_path(layer, cw, ch)
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    outline_w = layer.get("outline_width", 0.1) * px if layer.get("outline") else 0.0
    if layer.get("shadow"):
        strength = max(0.0, min(1.0, float(layer.get("shadow_strength", 0.6))))
        offset = px * 0.07 * strength
        soft = px * 0.06 * strength
        color = QColor(layer.get("shadow_color", "#000000"))
        steps = 10
        color.setAlpha(int(255 * min(1.0, 0.25 + strength) / steps * 2.2))
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        for k in range(steps):  # several offset copies make a soft drop shadow
            a = 2 * math.pi * k / steps
            p.drawPath(path.translated(offset + soft * math.cos(a), offset + soft * math.sin(a)))
            if outline_w:
                p.strokePath(path.translated(offset + soft * math.cos(a), offset + soft * math.sin(a)),
                             QPen(color, outline_w * 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    if outline_w:
        p.strokePath(path, QPen(QColor(layer.get("outline_color", "#000000")), outline_w * 2,
                                Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.fillPath(path, QColor(layer.get("color", "#ffffff")))
    p.restore()
    return path.boundingRect().adjusted(-outline_w, -outline_w, outline_w, outline_w)


def render_text_png(layer, cw, ch, out_path):
    """A full-canvas transparent PNG with just this text layer (overlaid by ffmpeg on export)."""
    img = QImage(cw, ch, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    draw_text(p, layer, cw, ch)
    p.end()
    img.save(str(out_path))
    return out_path
