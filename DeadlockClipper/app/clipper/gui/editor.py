"""Clip editor: video preview on top, a trim timeline below (a small CapCut-style editor)."""
import logging
import shutil

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QGraphicsObject, QGraphicsScene, QGraphicsView, QHBoxLayout, QPushButton, QRadioButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from ..ffmpeg import ffmpeg_path, run
from ..highlights import fmt_time
from ..paths import CACHE_DIR
from .common import start_task
from .pages import button, label

log = logging.getLogger(__name__)

MIN_LEN, MAX_LEN = 3.0, 60.0
STRIP_FRAMES = 16


def fmt_precise(t):
    t = max(0.0, t)
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:04.1f}"


# ---------------------------------------------------------------- crop box over the video

class CropOverlay(QGraphicsObject):
    """Shows which part of the frame "crop to fill" keeps. Drag it along the axis that gets cut."""
    moved = Signal(float)

    def __init__(self):
        super().__init__()
        self.size = QSizeF(16, 9)
        self.aspect = 9 / 16  # output width / height
        self.crop_x = 0.5
        self._drag = None
        self.setCursor(Qt.SizeAllCursor)

    def boundingRect(self):
        return QRectF(QPointF(0, 0), self.size)

    def _free(self):
        """(free pixels along x, free pixels along y) - only one of them is non-zero."""
        w, h = self.size.width(), self.size.height()
        if w / h > self.aspect:
            return w - h * self.aspect, 0.0
        return 0.0, h - w / self.aspect

    def box(self):
        w, h = self.size.width(), self.size.height()
        fx, fy = self._free()
        return QRectF(fx * self.crop_x, fy * self.crop_x, w - fx, h - fy)

    def paint(self, p, *_):
        b = self.box()
        w, h = self.size.width(), self.size.height()
        dim = QColor(0, 0, 0, 150)
        p.fillRect(QRectF(0, 0, w, b.top()), dim)
        p.fillRect(QRectF(0, b.bottom(), w, h - b.bottom()), dim)
        p.fillRect(QRectF(0, b.top(), b.left(), b.height()), dim)
        p.fillRect(QRectF(b.right(), b.top(), w - b.right(), b.height()), dim)
        p.setPen(QPen(QColor("#f0b35a"), max(2.0, min(w, h) / 200)))
        p.drawRect(b)

    def mousePressEvent(self, e):
        self._drag = (e.pos(), self.crop_x)

    def mouseMoveEvent(self, e):
        if self._drag is None:
            return
        fx, fy = self._free()
        p0, c0 = self._drag
        if fx > 1:
            self.crop_x = c0 + (e.pos().x() - p0.x()) / fx
        elif fy > 1:
            self.crop_x = c0 + (e.pos().y() - p0.y()) / fy
        self.crop_x = min(1.0, max(0.0, self.crop_x))
        self.update()
        self.moved.emit(self.crop_x)

    def mouseReleaseEvent(self, e):
        self._drag = None


class VideoView(QGraphicsView):
    def __init__(self, overlay=None):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor("#0d0e11"))
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.video = QGraphicsVideoItem()
        self.overlay = overlay or CropOverlay()
        self.scene().addItem(self.video)
        self.scene().addItem(self.overlay)
        self.video.nativeSizeChanged.connect(self._native_size)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_frame_size(self, w, h):
        size = QSizeF(w, h)
        self.video.setSize(size)
        self.overlay.prepareGeometryChange()
        self.overlay.size = size
        self.scene().setSceneRect(QRectF(QPointF(0, 0), size))
        self._fit()

    def _native_size(self, size):
        if size.isValid() and not size.isEmpty():
            self.set_frame_size(size.width(), size.height())

    def _fit(self):
        self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()


# ---------------------------------------------------------------- timeline

class Timeline(QWidget):
    """Filmstrip with draggable start/end handles and a playhead. Click anywhere to jump there."""
    seek = Signal(float)
    trimmed = Signal(float, float)

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

        # time ruler
        p.setPen(QColor("#9a9ca5"))
        span = self.v1 - self.v0
        step = next(s for s in (1, 2, 5, 10, 15, 30, 60, 120, 300) if span / s <= 12)
        t = (int(self.v0 // step) + 1) * step
        while t < self.v1:
            x = self._x(t)
            p.drawLine(QPointF(x, sh), QPointF(x, sh + 5))
            p.drawText(QRectF(x - 30, sh + 6, 60, 16), Qt.AlignCenter, fmt_time(t))
            t += step

        # playhead
        px = self._x(self.pos_t)
        p.setPen(QPen(QColor("white"), 2))
        p.drawLine(QPointF(px, 0), QPointF(px, sh + 4))
        p.setBrush(QColor("white"))
        p.drawEllipse(QPointF(px, sh + 4), 4, 4)
        p.end()

    def _hit(self, x):
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


# ---------------------------------------------------------------- editor page

class EditorDialog(QDialog):
    """Opens in its own window. Closing it (X, Esc or Cancel) just discards the changes."""
    saved = Signal(object)  # the clip

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModal(True)
        self.resize(1150, 800)
        self.info = None
        self.clip = None
        self.stop_at = None
        self._prime = False
        self._strip_job = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 12, 20, 12)
        top = QHBoxLayout()
        self.title = label("", "title")
        self.reasons = label("", "reason", wrap=True)
        top.addWidget(self.title)
        top.addWidget(self.reasons, 1)
        lay.addLayout(top)

        self.view = VideoView()
        lay.addWidget(self.view, 1)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.view.video)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)

        # transport controls
        ctl = QHBoxLayout()
        self.back5 = button("⏪ 5s")
        self.prev_f = button("◀ Frame")
        self.play_btn = button("▶ Play", primary=True)
        self.play_btn.setMinimumWidth(110)
        self.next_f = button("Frame ▶")
        self.fwd5 = button("5s ⏩")
        self.time_lbl = label("", "subtle")
        self.sel_btn = button("▶ Play clip")
        for wdg in (self.back5, self.prev_f, self.play_btn, self.next_f, self.fwd5):
            ctl.addWidget(wdg)
        ctl.addSpacing(12)
        ctl.addWidget(self.time_lbl)
        ctl.addStretch(1)
        ctl.addWidget(self.sel_btn)
        lay.addLayout(ctl)

        self.back5.clicked.connect(lambda: self._step(-5))
        self.fwd5.clicked.connect(lambda: self._step(5))
        self.prev_f.clicked.connect(lambda: self._frame(-1))
        self.next_f.clicked.connect(lambda: self._frame(1))
        self.play_btn.clicked.connect(self.toggle_play)
        self.sel_btn.clicked.connect(self.play_selection)

        # timeline
        self.timeline = Timeline()
        self.timeline.seek.connect(self.seek)
        self.timeline.trimmed.connect(self._trimmed)
        lay.addWidget(self.timeline)

        trim = QHBoxLayout()
        self.set_in = button("[ Set start here (I)")
        self.set_out = button("Set end here (O) ]")
        self.sel_lbl = label("")
        self.widen = button("↔ Show more timeline")
        self.set_in.clicked.connect(self.mark_in)
        self.set_out.clicked.connect(self.mark_out)
        self.widen.clicked.connect(self._widen)
        trim.addWidget(self.set_in)
        trim.addWidget(self.set_out)
        trim.addSpacing(12)
        trim.addWidget(self.sel_lbl)
        trim.addStretch(1)
        trim.addWidget(self.widen)
        lay.addLayout(trim)

        fmt_row = QHBoxLayout()
        fmt_row.addWidget(label("Format for this clip:"))
        self.fmt_blur = QRadioButton("Full frame + blurred background")
        self.fmt_crop = QRadioButton("Crop to fill")
        group = QButtonGroup(self)
        group.addButton(self.fmt_blur)
        group.addButton(self.fmt_crop)
        self.fmt_blur.toggled.connect(self._fmt_changed)
        fmt_row.addWidget(self.fmt_blur)
        fmt_row.addWidget(self.fmt_crop)
        self.crop_hint = label("Drag the box on the video to choose what part is shown.", "subtle")
        fmt_row.addWidget(self.crop_hint)
        fmt_row.addStretch(1)
        lay.addLayout(fmt_row)

        nav = QHBoxLayout()
        cancel = button("Cancel")
        cancel.clicked.connect(self._cancel)
        save = button("Save changes", primary=True)
        save.clicked.connect(self._save)
        nav.addWidget(label("Shortcuts: Space play/pause • ←/→ 1s • "
                            "Shift+←/→ one frame • I / O set start / end", "subtle"))
        nav.addStretch(1)
        nav.addWidget(cancel)
        nav.addWidget(save)
        lay.addLayout(nav)

        # Keep keyboard focus off buttons so Space/arrows always control the video.
        for wdg in self.findChildren(QPushButton) + [self.fmt_blur, self.fmt_crop]:
            wdg.setFocusPolicy(Qt.NoFocus)
        self.setFocusPolicy(Qt.StrongFocus)

        for key, fn in ((Qt.Key_Space, self.toggle_play), (Qt.Key_Left, lambda: self._step(-1)),
                        (Qt.Key_Right, lambda: self._step(1)),
                        (QKeySequence("Shift+Left"), lambda: self._frame(-1)),
                        (QKeySequence("Shift+Right"), lambda: self._frame(1)),
                        (Qt.Key_I, self.mark_in), (Qt.Key_O, self.mark_out)):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)

    # ------------------------------------------------------------ open / close

    def open(self, info, clip, index, default_fmt, orientation="vertical"):
        self.info, self.clip = info, clip
        self.s0, self.s1 = clip.start, clip.end
        self.title.setText(f"Edit clip {index}")
        self.setWindowTitle(f"Edit clip {index} - close this window to cancel")
        self.reasons.setText(" • ".join(clip.reasons))
        self.view.set_frame_size(info.width, info.height)
        self.view.overlay.crop_x = clip.crop_x
        self.view.overlay.aspect = 16 / 9 if orientation == "horizontal" else 9 / 16
        fx, fy = self.view.overlay._free()
        self.same_shape = fx < 2 and fy < 2  # input already has the output's shape
        self.fmt_blur.setEnabled(not self.same_shape)
        self.fmt_crop.setEnabled(not self.same_shape)
        fmt = clip.fmt or default_fmt
        (self.fmt_crop if fmt == "crop" else self.fmt_blur).setChecked(True)
        self._fmt_changed()
        pad = max(20.0, (clip.end - clip.start) * 0.6)
        self._set_view(max(0.0, clip.start - pad), min(info.duration, clip.end + pad))
        self.timeline.set_selection(self.s0, self.s1)
        self.timeline.set_position(self.s0)
        self._update_labels(self.s0)
        self.stop_at = None
        self._prime = True
        self.audio.setMuted(True)
        self.player.setSource(QUrl.fromLocalFile(info.path))
        self.setFocus()

    def stop(self):
        self.player.stop()
        self.player.setSource(QUrl())

    def _cancel(self):
        self.reject()

    def done(self, result):
        self.stop()  # stop sound whichever way the window is closed
        super().done(result)

    def _save(self):
        self.clip.start, self.clip.end = round(self.s0, 2), round(self.s1, 2)
        self.clip.fmt = "crop" if self.fmt_crop.isChecked() else "blur"
        self.clip.crop_x = self.view.overlay.crop_x
        if not self.clip.start <= self.clip.focus <= self.clip.end:
            self.clip.focus = (self.clip.start + self.clip.end) / 2
        log.info("Clip edited: %.2f-%.2f fmt=%s crop_x=%.2f", self.clip.start, self.clip.end,
                 self.clip.fmt, self.clip.crop_x)
        self.saved.emit(self.clip)
        self.accept()

    # ------------------------------------------------------------ playback

    def _on_status(self, status):
        if status == QMediaPlayer.LoadedMedia and self._prime:
            # Start then immediately pause so the first frame is shown.
            self.player.setPosition(int(self.s0 * 1000))
            self.player.play()

    def _on_state(self, state):
        playing = state == QMediaPlayer.PlayingState
        self.play_btn.setText("⏸ Pause" if playing else "▶ Play")

    def _on_error(self, error, message):
        log.error("Media player error %s: %s", error, message)
        self.time_lbl.setText("Preview can't play this file here. Trimming still works; "
                              "the exported clip is not affected.")

    def _on_position(self, ms):
        t = ms / 1000.0
        if self._prime and self.player.playbackState() == QMediaPlayer.PlayingState:
            self._prime = False
            self.player.pause()
            self.player.setPosition(int(self.s0 * 1000))
            self.audio.setMuted(False)
            return
        if self.stop_at is not None and t >= self.stop_at:
            self.stop_at = None
            self.player.pause()
        if t > self.timeline.v1:
            self._set_view(self.timeline.v0, min(self.info.duration, t + 20))
        self.timeline.set_position(t)
        self._update_labels(t)

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.stop_at = None
            self.player.play()

    def play_selection(self):
        self.player.setPosition(int(self.s0 * 1000))
        self.stop_at = self.s1
        self.player.play()

    def seek(self, t):
        t = min(max(0.0, t), self.info.duration)
        self.player.setPosition(int(t * 1000))
        self.timeline.set_position(t)
        self._update_labels(t)

    def _now(self):
        return self.player.position() / 1000.0

    def _step(self, seconds):
        self.seek(self._now() + seconds)

    def _frame(self, n):
        self.player.pause()
        self.seek(self._now() + n / max(1.0, self.info.fps))

    # ------------------------------------------------------------ trimming

    def mark_in(self):
        t = self._now()
        self.s0 = min(max(t, self.s1 - MAX_LEN), self.s1 - MIN_LEN)
        self.timeline.set_selection(self.s0, self.s1)
        self._update_labels(t)

    def mark_out(self):
        t = self._now()
        self.s1 = max(min(t, self.s0 + MAX_LEN), self.s0 + MIN_LEN)
        self.s1 = min(self.s1, self.info.duration)
        self.timeline.set_selection(self.s0, self.s1)
        self._update_labels(t)

    def _trimmed(self, s0, s1):
        self.s0, self.s1 = s0, s1
        self._update_labels(self._now())

    def _update_labels(self, t):
        self.time_lbl.setText(f"{fmt_precise(t)} / {fmt_time(self.info.duration)}")
        self.sel_lbl.setText(f"Clip: {fmt_precise(self.s0)} → {fmt_precise(self.s1)}  "
                             f"({self.s1 - self.s0:.1f}s)")

    def _widen(self):
        v0, v1 = self.timeline.v0, self.timeline.v1
        self._set_view(max(0.0, v0 - 30), min(self.info.duration, v1 + 30))

    def _fmt_changed(self):
        crop = self.fmt_crop.isChecked() and not getattr(self, "same_shape", False)
        self.view.overlay.setVisible(crop)
        self.crop_hint.setVisible(crop)

    # ------------------------------------------------------------ filmstrip

    def _set_view(self, v0, v1):
        self.timeline.set_view(v0, v1)
        self._strip_job += 1
        job = self._strip_job
        info = self.info

        def work(progress, cancel):
            root = CACHE_DIR / "strip"
            for old in root.glob("*"):
                if old.name != str(job):
                    shutil.rmtree(old, ignore_errors=True)
            folder = root / str(job)
            folder.mkdir(parents=True, exist_ok=True)
            span = v1 - v0
            run([ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-y",
                 "-ss", f"{v0:.3f}", "-t", f"{span:.3f}", "-i", info.path, "-an",
                 "-vf", f"fps={STRIP_FRAMES / span:.5f},scale=160:90",
                 "-frames:v", str(STRIP_FRAMES), str(folder / f"f{job}_%02d.jpg")], timeout=120)
            return sorted(folder.glob(f"f{job}_*.jpg"))

        def done(paths):
            if job == self._strip_job:
                self.timeline.set_thumbs([QPixmap(str(p)) for p in paths])

        start_task(self, work, on_done=done,
                   on_failed=lambda m, d: log.warning("Filmstrip failed: %s %s", m, d))
