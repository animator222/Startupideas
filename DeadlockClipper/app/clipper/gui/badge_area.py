"""'Kill badge area' setup: find a kill in the video, draw a box around the badge.

The frame view always shows the current position (also while paused or scrubbing), the timeline
can be clicked/dragged and zoomed with the mouse wheel, and every frame is checked live: the
status tells you whether a kill badge is inside the box and marks it on the video."""
import logging
import shutil
import time

import cv2
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from ..ffmpeg import ffmpeg_path, run
from ..highlights import fmt_time
from ..paths import CACHE_DIR
from ..highlights import BADGE_THRESHOLD
from ..vision import CROP_W, default_region, detect_badge
from .common import start_task
from .editor import EDITOR_STYLE, _button, _label
from .timeline import Timeline, fmt_precise

log = logging.getLogger(__name__)
HANDLE = 10
STRIP_FRAMES = 24
ACCENT = QColor("#f0b35a")
FOUND = QColor("#5ee07a")


class FrameView(QWidget):
    """The current video frame with the badge box on top.
    Drag inside the box to move it, drag a corner to resize, drag outside it to draw a new box."""
    region_changed = Signal()

    def __init__(self):
        super().__init__()
        self.setMinimumSize(480, 300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.frame = QImage()
        self.region = [0.35, 0.12, 0.30, 0.33]  # x, y, w, h as fractions of the frame
        self.marker = None  # detected badge centre (fractions) or None
        self._drag = None

    def _frame_rect(self):
        fw, fh = (self.frame.width(), self.frame.height()) if not self.frame.isNull() else (16, 9)
        avail = QRectF(self.rect()).adjusted(10, 10, -10, -10)
        k = min(avail.width() / fw, avail.height() / fh)
        r = QRectF(0, 0, fw * k, fh * k)
        r.moveCenter(avail.center())
        return r

    def _box(self):
        fr = self._frame_rect()
        x, y, w, h = self.region
        return QRectF(fr.left() + x * fr.width(), fr.top() + y * fr.height(), w * fr.width(), h * fr.height())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor("#0b0c10"))
        fr = self._frame_rect()
        if not self.frame.isNull():
            p.drawImage(fr, self.frame)
        b = self._box()
        dim = QColor(0, 0, 0, 130)
        p.fillRect(QRectF(fr.left(), fr.top(), fr.width(), b.top() - fr.top()), dim)
        p.fillRect(QRectF(fr.left(), b.bottom(), fr.width(), fr.bottom() - b.bottom()), dim)
        p.fillRect(QRectF(fr.left(), b.top(), b.left() - fr.left(), b.height()), dim)
        p.fillRect(QRectF(b.right(), b.top(), fr.right() - b.right(), b.height()), dim)
        color = FOUND if self.marker else ACCENT
        p.setPen(QPen(color, 2, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
        p.drawRect(b)
        p.setPen(QPen(QColor("#0b0c10"), 1))
        p.setBrush(color)
        for c in (b.topLeft(), b.topRight(), b.bottomLeft(), b.bottomRight()):
            p.drawRoundedRect(QRectF(c.x() - HANDLE / 2, c.y() - HANDLE / 2, HANDLE, HANDLE), 2, 2)
        tag = QRectF(b.left(), b.top() - 22, 150, 20)
        p.fillRect(tag, color)
        p.setPen(QColor("#0b0c10"))
        p.drawText(tag, Qt.AlignCenter, "Kill badge area")
        if self.marker:
            mx = fr.left() + self.marker[0] * fr.width()
            my = fr.top() + self.marker[1] * fr.height()
            rad = max(14.0, fr.height() * 0.05)
            p.setPen(QPen(FOUND, 3))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(mx, my), rad, rad)
        p.end()

    def _corner(self, pos):
        b = self._box()
        for name, c in (("tl", b.topLeft()), ("tr", b.topRight()), ("bl", b.bottomLeft()), ("br", b.bottomRight())):
            if abs(pos.x() - c.x()) <= HANDLE and abs(pos.y() - c.y()) <= HANDLE:
                return name
        return None

    def _to_frac(self, pos):
        fr = self._frame_rect()
        return (min(max((pos.x() - fr.left()) / fr.width(), 0.0), 1.0),
                min(max((pos.y() - fr.top()) / fr.height(), 0.0), 1.0))

    def mousePressEvent(self, e):
        pos = e.position()
        corner = self._corner(pos)
        if corner:
            self._drag = ("resize", corner, None)
        elif self._box().contains(pos):
            self._drag = ("move", self._to_frac(pos), list(self.region))
        else:
            fx, fy = self._to_frac(pos)
            self._drag = ("draw", (fx, fy), None)
            self.region = [fx, fy, 0.01, 0.01]

    def mouseMoveEvent(self, e):
        pos = e.position()
        if not self._drag:
            corner = self._corner(pos)
            self.setCursor(Qt.SizeFDiagCursor if corner in ("tl", "br") else Qt.SizeBDiagCursor if corner
                           else Qt.SizeAllCursor if self._box().contains(pos) else Qt.CrossCursor)
            return
        kind, start, orig = self._drag
        fx, fy = self._to_frac(pos)
        x, y, w, h = self.region
        if kind == "move":
            ox, oy, ow, oh = orig
            nx = min(max(0.0, ox + fx - start[0]), 1 - ow)
            ny = min(max(0.0, oy + fy - start[1]), 1 - oh)
            self.region = [nx, ny, ow, oh]
        elif kind == "resize":
            x0, y0, x1, y1 = x, y, x + w, y + h
            if "l" in start:
                x0 = min(fx, x1 - 0.03)
            else:
                x1 = max(fx, x0 + 0.03)
            if "t" in start:
                y0 = min(fy, y1 - 0.03)
            else:
                y1 = max(fy, y0 + 0.03)
            self.region = [x0, y0, x1 - x0, y1 - y0]
        else:
            sx, sy = start
            self.region = [min(sx, fx), min(sy, fy), max(0.03, abs(fx - sx)), max(0.03, abs(fy - sy))]
        self.update()
        self.region_changed.emit()

    def mouseReleaseEvent(self, e):
        self._drag = None
        self.region_changed.emit()


class BadgeAreaDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("editor")
        self.setWindowTitle("Kill badge area")
        self.setWindowFlags(Qt.Window | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.setStyleSheet(EDITOR_STYLE)
        self.resize(1360, 880)
        self.info = None
        self._prime = False
        self._frame_img = QImage()
        self._last_check = 0.0
        self._strip_job = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = QFrame()
        top.setObjectName("topbar")
        tl = QHBoxLayout(top)
        tl.setContentsMargins(18, 10, 14, 10)
        tl.addWidget(_label("Kill badge area", "h1"))
        tl.addSpacing(10)
        tl.addWidget(_label("Show the app where your red KILL / ASSIST badge appears", "muted"))
        tl.addStretch(1)
        reset = _button("Reset to default")
        reset.clicked.connect(self._reset)
        cancel = _button("Cancel")
        cancel.clicked.connect(self.reject)
        save = _button("Save", "primary")
        save.clicked.connect(self.accept)
        for w in (reset, cancel, save):
            tl.addWidget(w)
        root.addWidget(top)

        body = QHBoxLayout()
        body.setContentsMargins(14, 14, 14, 10)
        body.setSpacing(12)
        stage = QFrame()
        stage.setObjectName("stage")
        sl = QVBoxLayout(stage)
        sl.setContentsMargins(8, 8, 8, 8)
        self.view = FrameView()
        self.view.region_changed.connect(lambda: self._check(force=True))
        sl.addWidget(self.view)
        body.addWidget(stage, 1)

        side = QFrame()
        side.setObjectName("panel")
        side.setFixedWidth(300)
        sd = QVBoxLayout(side)
        sd.setContentsMargins(14, 12, 14, 12)
        sd.addWidget(_label("LIVE CHECK", "section"))
        self.status = _label("", wrap=True)
        self.status.setMinimumHeight(70)
        sd.addWidget(self.status)
        self.fit_btn = _button("⌖  Fit the box around this badge")
        self.fit_btn.setToolTip("Puts the box around the badge that was found, with room for it to move a bit")
        self.fit_btn.clicked.connect(self._fit_to_badge)
        self.fit_btn.setEnabled(False)
        sd.addWidget(self.fit_btn)
        sd.addSpacing(12)
        sd.addWidget(_label("HOW TO", "section"))
        sd.addWidget(_label(
            "1. Move through the video with the timeline below (click or drag; mouse wheel zooms in) "
            "or the play buttons, until a red KILL or ASSIST badge is on screen.\n\n"
            "2. Drag a box around the badge on the video - or press 'Fit the box around this badge' once "
            "the live check finds it.\n\n"
            "3. Leave about one badge-width of space on every side (and room for two badges side by side). "
            "Keep the crosshair out of the box.\n\n"
            "4. Save. The box is remembered for this video shape (vertical or horizontal).", "muted", wrap=True))
        sd.addStretch(1)
        body.addWidget(side)
        root.addLayout(body, 1)

        bar = QFrame()
        bar.setObjectName("transport")
        bl = QVBoxLayout(bar)
        bl.setContentsMargins(16, 10, 16, 12)
        row = QHBoxLayout()
        back5 = _button("⏪", tip="5 seconds back (← = 1 s)")
        prev_f = _button("◀|", tip="One frame back (Shift+←)")
        self.play_btn = _button("▶", "play", tip="Play / pause (Space)")
        next_f = _button("|▶", tip="One frame forward (Shift+→)")
        fwd5 = _button("⏩", tip="5 seconds forward (→ = 1 s)")
        back5.clicked.connect(lambda: self._step(-5))
        fwd5.clicked.connect(lambda: self._step(5))
        prev_f.clicked.connect(lambda: self._frame(-1))
        next_f.clicked.connect(lambda: self._frame(1))
        self.play_btn.clicked.connect(self._toggle)
        self.time_lbl = _label("0:00.0", "time")
        for w in (back5, prev_f, self.play_btn, next_f, fwd5):
            row.addWidget(w)
        row.addSpacing(12)
        row.addWidget(self.time_lbl)
        row.addStretch(1)
        row.addWidget(_label("Space play/pause • ←/→ 1 s • Shift+←/→ one frame • "
                             "mouse wheel on the timeline zooms in/out", "muted"))
        bl.addLayout(row)
        self.timeline = Timeline()
        self.timeline.show_selection = False
        self.timeline.seek.connect(self.seek)
        self.timeline.view_changed.connect(self._load_strip)
        bl.addWidget(self.timeline)
        root.addWidget(bar)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setMuted(True)
        self.sink = QVideoSink(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.player.positionChanged.connect(self._on_position)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.playbackStateChanged.connect(
            lambda st: self.play_btn.setText("⏸" if st == QMediaPlayer.PlayingState else "▶"))

        for key, fn in ((Qt.Key_Space, self._toggle), (Qt.Key_Left, lambda: self._step(-1)),
                        (Qt.Key_Right, lambda: self._step(1)),
                        (QKeySequence("Shift+Left"), lambda: self._frame(-1)),
                        (QKeySequence("Shift+Right"), lambda: self._frame(1))):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(fn)

    # ------------------------------------------------------------ open / result

    def open_video(self, info, region):
        self.info = info
        self.view.region = list(region)
        self.view.marker = None
        self.timeline.limit = info.duration
        self.timeline.set_position(0)
        self._load_strip(0.0, info.duration)
        self._set_status(None)
        self._prime = True
        self.player.setSource(QUrl.fromLocalFile(info.path))

    def region(self):
        x, y, w, h = self.view.region
        return (max(0.0, x), max(0.0, y), min(w, 1 - x), min(h, 1 - y))

    def done(self, result):
        self.player.stop()
        self.player.setSource(QUrl())
        super().done(result)

    def _reset(self):
        self.view.region = list(default_region(self.info))
        self.view.update()
        self._check(force=True)

    # ------------------------------------------------------------ playback

    def _on_status(self, status):
        if status == QMediaPlayer.LoadedMedia and self._prime:
            self.player.setPosition(int(self.info.duration * 400))  # start 40% in
            self.player.play()

    def _on_position(self, ms):
        if self._prime and self.player.playbackState() == QMediaPlayer.PlayingState:
            self._prime = False
            self.player.pause()
            self.player.setPosition(ms)
        t = ms / 1000.0
        if t > self.timeline.v1 or t < self.timeline.v0:  # keep the playhead in view
            span = self.timeline.v1 - self.timeline.v0
            v0 = min(max(0.0, t - span / 2), max(0.0, self.info.duration - span))
            self.timeline.set_view(v0, v0 + span)
            self._load_strip(v0, v0 + span)
        self.timeline.set_position(t)
        self.time_lbl.setText(f"{fmt_precise(t)} / {fmt_time(self.info.duration)}")

    def _on_frame(self, frame):
        if frame.isValid():
            img = frame.toImage()
            if not img.isNull():
                self._frame_img = img
                self.view.frame = img
                self.view.update()
                self._check()

    def _toggle(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def seek(self, t):
        t = min(max(0.0, t), self.info.duration)
        self.player.setPosition(int(t * 1000))
        self.timeline.set_position(t)
        self.time_lbl.setText(f"{fmt_precise(t)} / {fmt_time(self.info.duration)}")

    def _step(self, seconds):
        self.seek(self.player.position() / 1000.0 + seconds)

    def _frame(self, n):
        self.player.pause()
        self.seek(self.player.position() / 1000.0 + n / max(1.0, self.info.fps))

    # ------------------------------------------------------------ live check

    def _check(self, force=False):
        """Run the real badge detector on the box of the current frame (a few times a second)."""
        now = time.monotonic()
        if (not force and now - self._last_check < 0.2) or self._frame_img.isNull():
            return
        self._last_check = now
        img = self._frame_img.convertToFormat(QImage.Format_RGB888)
        w, h = img.width(), img.height()
        arr = np.frombuffer(img.constBits(), np.uint8).reshape(h, img.bytesPerLine())[:, :w * 3].reshape(h, w, 3)
        x, y, rw, rh = self.region()
        crop = arr[int(y * h):int((y + rh) * h), int(x * w):int((x + rw) * w)]
        if crop.shape[0] < 8 or crop.shape[1] < 8:
            return
        scale = CROP_W / crop.shape[1]
        small = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_RGB2BGR), (CROP_W, max(8, int(crop.shape[0] * scale))),
                           interpolation=cv2.INTER_AREA)
        score, bx, by = detect_badge(small, scale, min(w, h))
        if score >= BADGE_THRESHOLD and bx >= 0:
            self.view.marker = ((x * w + bx) / w, (y * h + by) / h)
            self._set_status(score)
        else:
            self.view.marker = None
            self._set_status(None)
        self.view.update()

    def _set_status(self, score):
        if score is None:
            self.status.setText("No kill badge inside the box on this frame.\n"
                                "Move to a moment with a KILL or ASSIST badge.")
            self.status.setStyleSheet("color: #c9ccd6; font-size: 14px;")
            self.fit_btn.setEnabled(False)
        else:
            self.status.setText(f"✔  Kill badge found in the box\n(score {score:.2f} - 0.50 or more counts)")
            self.status.setStyleSheet("color: #5ee07a; font-size: 15px; font-weight: 700;")
            self.fit_btn.setEnabled(True)

    def _fit_to_badge(self):
        """Box around the found badge with about one badge-width of room on each side."""
        if not self.view.marker or self._frame_img.isNull():
            return
        mx, my = self.view.marker
        w, h = self._frame_img.width(), self._frame_img.height()
        badge = 105 * min(w, h) / 1080  # badge diameter in pixels at this size
        half_w, half_h = 1.6 * badge / w, 1.2 * badge / h
        x0, y0 = max(0.0, mx - half_w), max(0.0, my - half_h)
        x1, y1 = min(1.0, mx + half_w), min(1.0, my + half_h)
        self.view.region = [x0, y0, x1 - x0, y1 - y0]
        self.view.update()
        self._check(force=True)

    # ------------------------------------------------------------ filmstrip

    def _load_strip(self, v0, v1):
        self.timeline.set_view(v0, v1)
        self._strip_job += 1
        job = self._strip_job
        info = self.info

        def work(progress, cancel):
            root = CACHE_DIR / "badge_strip"
            for old in root.glob("*"):
                if old.name != str(job):
                    shutil.rmtree(old, ignore_errors=True)
            folder = root / str(job)
            folder.mkdir(parents=True, exist_ok=True)
            span = max(1.0, v1 - v0)
            out = []
            for k in range(STRIP_FRAMES):  # quick seeks: fast even for long videos
                if cancel.is_set() or job != self._strip_job:
                    break
                t = v0 + (k + 0.5) * span / STRIP_FRAMES
                path = folder / f"f{k:02d}.jpg"
                try:
                    run([ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-y", "-ss", f"{t:.2f}",
                         "-i", info.path, "-frames:v", "1", "-vf", "scale=160:-2", str(path)], timeout=20)
                    out.append(path)
                except Exception:
                    pass
            return out

        def done(paths):
            if job == self._strip_job:
                self.timeline.set_thumbs([QPixmap(str(p)) for p in paths])

        start_task(self, work, on_done=done,
                   on_failed=lambda m, d: log.warning("Filmstrip failed: %s %s", m, d))
