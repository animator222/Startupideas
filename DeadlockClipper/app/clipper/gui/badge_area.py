"""'Kill badge area' setup: the user scrubs to a kill and draws a box around the badge."""
import logging

import cv2
import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt, QUrl
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QDialog, QGraphicsObject, QGraphicsScene, QGraphicsView, QHBoxLayout, QSizePolicy, QSlider, QVBoxLayout,
)

from ..highlights import fmt_time
from ..vision import BADGE_DIAMETER_1080P, CROP_W, default_region, detect_badge
from .pages import button, label

log = logging.getLogger(__name__)


class VideoView(QGraphicsView):
    """The source video with an overlay item (e.g. a RegionBox) on top."""

    def __init__(self, overlay):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor("#0d0e11"))
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.video = QGraphicsVideoItem()
        self.overlay = overlay
        self.scene().addItem(self.video)
        self.scene().addItem(self.overlay)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_frame_size(self, w, h):
        size = QSizeF(w, h)
        self.video.setSize(size)
        self.overlay.prepareGeometryChange()
        self.overlay.size = size
        self.scene().setSceneRect(QRectF(QPointF(0, 0), size))
        self._fit()

    def _fit(self):
        self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()


class RegionBox(QGraphicsObject):
    """A box on top of the video. Drag inside to move, drag the corner to resize,
    drag anywhere outside to draw a new box."""
    HANDLE = 0.035  # corner handle size, as a fraction of the frame's short side

    def __init__(self, title="", color="#f0b35a", dim=True):
        super().__init__()
        self.size = QSizeF(16, 9)
        self.rect = QRectF(0, 0, 1, 1)
        self.title, self.color = title, QColor(color)
        self.dim = dim  # darken everything outside the box (single-box mode, can draw a new box)
        self._mode = None
        self.setAcceptHoverEvents(True)

    def boundingRect(self):
        if self.dim:
            return QRectF(QPointF(0, 0), self.size)
        m = 2 * self.HANDLE * min(self.size.width(), self.size.height())
        return self.rect.normalized().adjusted(-m, -m * 2, m, m)

    def _changed(self):
        if not self.dim:
            self.prepareGeometryChange()

    def set_region(self, region):
        w, h = self.size.width(), self.size.height()
        x, y, rw, rh = region
        self._changed()
        self.rect = QRectF(x * w, y * h, rw * w, rh * h)
        self.update()

    def region(self):
        w, h = self.size.width(), self.size.height()
        r = self.rect.normalized().intersected(QRectF(0, 0, w, h))
        return (r.x() / w, r.y() / h, r.width() / w, r.height() / h)

    def _handle(self):
        s = self.HANDLE * min(self.size.width(), self.size.height())
        return QRectF(self.rect.right() - s, self.rect.bottom() - s, 2 * s, 2 * s)

    def paint(self, p, *_):
        w, h = self.size.width(), self.size.height()
        b = self.rect.normalized()
        if self.dim:
            dim = QColor(0, 0, 0, 140)
            p.fillRect(QRectF(0, 0, w, b.top()), dim)
            p.fillRect(QRectF(0, b.bottom(), w, h - b.bottom()), dim)
            p.fillRect(QRectF(0, b.top(), b.left(), b.height()), dim)
            p.fillRect(QRectF(b.right(), b.top(), w - b.right(), b.height()), dim)
        else:
            fill = QColor(self.color)
            fill.setAlpha(40)
            p.fillRect(b, fill)
        p.setPen(QPen(self.color, max(2.0, min(w, h) / 250)))
        p.drawRect(b)
        p.fillRect(self._handle(), self.color)
        if self.title:
            font = p.font()
            font.setPixelSize(max(12, int(min(w, h) / 30)))
            font.setBold(True)
            p.setFont(font)
            p.drawText(b.adjusted(8, 4, 0, 0), Qt.AlignLeft | Qt.AlignTop, self.title)

    def hoverMoveEvent(self, e):
        pos = e.pos()
        if self._handle().contains(pos):
            self.setCursor(Qt.SizeFDiagCursor)
        elif self.rect.contains(pos):
            self.setCursor(Qt.SizeAllCursor)
        else:
            self.setCursor(Qt.CrossCursor)

    def mousePressEvent(self, e):
        pos = e.pos()
        if self._handle().contains(pos):
            self._mode = ("resize", pos, QRectF(self.rect))
        elif self.rect.contains(pos):
            self._mode = ("move", pos, QRectF(self.rect))
        elif self.dim:
            self._mode = ("draw", pos, None)
            self.rect = QRectF(pos, pos)
        else:
            e.ignore()

    def mouseMoveEvent(self, e):
        if not self._mode:
            return
        kind, start, orig = self._mode
        pos = e.pos()
        w, h = self.size.width(), self.size.height()
        pos = QPointF(min(max(pos.x(), 0), w), min(max(pos.y(), 0), h))
        self._changed()
        if kind == "move":
            r = orig.translated(pos - start)
            r.moveLeft(min(max(r.left(), 0), w - r.width()))
            r.moveTop(min(max(r.top(), 0), h - r.height()))
            self.rect = r
        elif kind == "resize":
            self.rect = QRectF(orig.topLeft(), pos)
        else:
            self.rect = QRectF(start, pos)
        self.update()

    def mouseReleaseEvent(self, e):
        self._mode = None
        self._changed()
        self.rect = self.rect.normalized()
        min_side = 0.03 * min(self.size.width(), self.size.height())
        if self.rect.width() < min_side or self.rect.height() < min_side:
            self.rect.setSize(QSizeF(max(self.rect.width(), min_side * 4), max(self.rect.height(), min_side * 4)))
        self.update()


class BadgeAreaDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kill badge area")
        self.setModal(True)
        self.resize(1100, 820)
        self.info = None
        self._prime = False
        self._frame = None

        lay = QVBoxLayout(self)
        lay.addWidget(label("Show the app where your kill badge appears", "title"))
        lay.addWidget(label(
            "1. Use the slider or Play to find a moment where the red KILL / KILL ASSIST badge is on screen.\n"
            "2. Drag a box around the badge, leaving a little space around it (drag the corner to resize).\n"
            "3. Press 'Check this frame' to make sure it's found, then Save. The badge always appears in "
            "the same place, so you only need to do this once per layout.", "subtle", wrap=True))

        self.view = VideoView(overlay=RegionBox())
        lay.addWidget(self.view, 1)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setMuted(True)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.view.video)
        self.player.positionChanged.connect(self._on_position)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.view.video.videoSink().videoFrameChanged.connect(self._on_frame)

        row = QHBoxLayout()
        self.play_btn = button("▶ Play")
        self.play_btn.clicked.connect(self._toggle)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.sliderMoved.connect(lambda v: self.player.setPosition(v))
        self.time_lbl = label("", "subtle")
        row.addWidget(self.play_btn)
        row.addWidget(self.slider, 1)
        row.addWidget(self.time_lbl)
        lay.addLayout(row)

        bottom = QHBoxLayout()
        check = button("Check this frame")
        check.clicked.connect(self._check)
        self.result_lbl = label("", wrap=True)
        self.result_lbl.setMinimumWidth(420)
        reset = button("Reset to default")
        reset.clicked.connect(lambda: self.view.overlay.set_region(default_region(self.info)))
        cancel = button("Cancel")
        cancel.clicked.connect(self.reject)
        save = button("Save", primary=True)
        save.clicked.connect(self.accept)
        bottom.addWidget(check)
        bottom.addWidget(self.result_lbl, 1)
        for wdg in (reset, cancel, save):
            bottom.addWidget(wdg)
        lay.addLayout(bottom)

    def open_video(self, info, region):
        self.info = info
        self.view.set_frame_size(info.width, info.height)
        self.view.overlay.set_region(region)
        self.slider.setRange(0, int(info.duration * 1000))
        self.result_lbl.setText("")
        self._prime = True
        self.player.setSource(QUrl.fromLocalFile(info.path))

    def region(self):
        return self.view.overlay.region()

    def done(self, result):
        self.player.stop()
        self.player.setSource(QUrl())
        super().done(result)

    def _on_status(self, status):
        if status == QMediaPlayer.LoadedMedia and self._prime:
            self.player.setPosition(int(self.info.duration * 400))  # start 40% in
            self.player.play()

    def _on_position(self, ms):
        if self._prime and self.player.playbackState() == QMediaPlayer.PlayingState:
            self._prime = False
            self.player.pause()
            self.player.setPosition(ms)  # make sure the paused frame is drawn
        if not self.slider.isSliderDown():
            self.slider.setValue(ms)
        self.time_lbl.setText(f"{fmt_time(ms / 1000)} / {fmt_time(self.info.duration)}")

    def _toggle(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.play_btn.setText("▶ Play")
        else:
            self.player.play()
            self.play_btn.setText("⏸ Pause")

    def _on_frame(self, frame):
        self._frame = frame

    def _check(self):
        img = self._frame.toImage() if self._frame is not None and self._frame.isValid() else QImage()
        if img.isNull():
            self.result_lbl.setText("No frame to check yet - press Play first.")
            return
        img = img.convertToFormat(QImage.Format_RGB888)
        w, h = img.width(), img.height()
        arr = np.frombuffer(img.constBits(), np.uint8).reshape(h, img.bytesPerLine())[:, :w * 3].reshape(h, w, 3)
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        x, y, rw, rh = self.region()
        crop = bgr[int(y * h):int((y + rh) * h), int(x * w):int((x + rw) * w)]
        if crop.size == 0:
            return
        scale = CROP_W / crop.shape[1]
        crop = cv2.resize(crop, (CROP_W, max(2, int(crop.shape[0] * scale))), interpolation=cv2.INTER_AREA)
        score, _, _ = detect_badge(crop, scale, min(w, h))
        log.info("Badge area check: score %.2f (region %s, expected size %d px)", score,
                 self.region(), BADGE_DIAMETER_1080P)
        if score >= 0.5:
            self.result_lbl.setText(f"✔ Kill badge found (score {score:.2f})")
            self.result_lbl.setStyleSheet("color: #6bd96b;")
        else:
            self.result_lbl.setText(f"✖ No kill badge found here (score {score:.2f}). Is the badge on "
                                    "screen and inside the box?")
            self.result_lbl.setStyleSheet("color: #ff6b6b;")
