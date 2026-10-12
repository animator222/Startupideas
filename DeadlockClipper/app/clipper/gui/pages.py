"""The app's screens: Home, Settings, Progress, Review and Done."""
import logging
from dataclasses import replace
from pathlib import Path

import numpy as np
from PySide6.QtCore import QLineF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QProgressBar, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
    QVBoxLayout, QWidget,
)

from .. import APP_VERSION
from ..highlights import fmt_time
from ..paths import LOG_DIR, open_in_explorer, videos_folder

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".flv", ".avi", ".webm", ".ts", ".m4v"}


def label(text, name=None, wrap=False):
    w = QLabel(text)
    if name:
        w.setObjectName(name)
    w.setWordWrap(wrap)
    return w


def button(text, primary=False):
    b = QPushButton(text)
    b.setCursor(Qt.PointingHandCursor)
    if primary:
        b.setObjectName("primary")
    return b


def refresh_style(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


# ---------------------------------------------------------------- Home

class DropZone(QFrame):
    file_dropped = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("drop")
        self.setAcceptDrops(True)
        self.setMinimumHeight(220)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.addWidget(label("Drop a recording here", "title"), alignment=Qt.AlignCenter)
        lay.addWidget(label("MP4, MKV, MOV, FLV ...", "subtle"), alignment=Qt.AlignCenter)

    def _set_hover(self, on):
        self.setProperty("hover", "true" if on else "false")
        refresh_style(self)

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile():
            event.acceptProposedAction()
            self._set_hover(True)

    def dragLeaveEvent(self, event):
        self._set_hover(False)

    def dropEvent(self, event):
        self._set_hover(False)
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile():
            self.file_dropped.emit(urls[0].toLocalFile())


class HomePage(QWidget):
    file_chosen = Signal(str)
    url_chosen = Signal(str)

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 30, 40, 30)
        lay.setSpacing(16)
        title_row = QHBoxLayout()
        title_row.addWidget(label("Deadlock Clipper", "title"))
        title_row.addStretch(1)
        title_row.addWidget(label(f"Version {APP_VERSION}", "subtle"))
        lay.addLayout(title_row)
        lay.addWidget(label("Turn a Deadlock recording into vertical highlight clips for Shorts and Reels.",
                            "subtle", wrap=True))

        drop = DropZone()
        drop.file_dropped.connect(self._check_and_emit)
        lay.addWidget(drop)

        browse = button("Choose a video file...", primary=True)
        browse.clicked.connect(self._browse)
        lay.addWidget(browse, alignment=Qt.AlignCenter)

        lay.addSpacing(10)
        lay.addWidget(label("Or paste a YouTube link (local files work best):", "subtle"))
        row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://www.youtube.com/watch?v=...")
        self.url.returnPressed.connect(self._url)
        dl = button("Download")
        dl.clicked.connect(self._url)
        row.addWidget(self.url, 1)
        row.addWidget(dl)
        lay.addLayout(row)
        lay.addStretch(1)

        logs = button("Open logs folder")
        logs.clicked.connect(lambda: open_in_explorer(LOG_DIR))
        lay.addWidget(logs, alignment=Qt.AlignRight)

    def _browse(self):
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))
        path, _ = QFileDialog.getOpenFileName(self, "Choose a recording", str(videos_folder()),
                                              f"Videos ({exts});;All files (*)")
        if path:
            self._check_and_emit(path)

    def _check_and_emit(self, path):
        self.file_chosen.emit(path)

    def _url(self):
        text = self.url.text().strip()
        if text:
            self.url_chosen.emit(text)


# ---------------------------------------------------------------- Settings

class FormatOption(QFrame):
    clicked = Signal()

    def __init__(self, title, subtitle):
        super().__init__()
        self.setObjectName("option")
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        self.image = QLabel("Making preview...")
        self.image.setFixedSize(162, 288)
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setStyleSheet("background: #0d0e11; border-radius: 4px;")
        self.radio = QRadioButton(title)
        lay.addWidget(self.image, alignment=Qt.AlignCenter)
        lay.addWidget(self.radio)
        lay.addWidget(label(subtitle, "subtle", wrap=True))
        self.radio.toggled.connect(self._update)

    def mousePressEvent(self, event):
        self.radio.setChecked(True)
        self.clicked.emit()

    def _update(self):
        self.setProperty("selected", "true" if self.radio.isChecked() else "false")
        refresh_style(self)

    def set_shape(self, orientation):
        self.image.setFixedSize(*((288, 162) if orientation == "horizontal" else (162, 288)))
        self.image.setPixmap(QPixmap())
        self.image.setText("Making preview...")

    def set_image(self, path):
        pix = QPixmap(str(path))
        if not pix.isNull():
            self.image.setPixmap(pix.scaled(self.image.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.image.setText("No preview")


class SettingsPage(QWidget):
    start = Signal(object)  # Settings
    back = Signal()
    zoom_changed = Signal(float)  # new zoom level: the zoom preview needs re-making
    edit_layout = Signal()  # open the editor on "My layout"

    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(30, 20, 30, 20)
        self.heading = label("Settings", "title")
        self.video_label = label("", "subtle", wrap=True)
        outer.addWidget(self.heading)
        outer.addWidget(self.video_label)

        body = QHBoxLayout()
        outer.addLayout(body, 1)

        # Left: numbers and checkboxes
        form = QGridLayout()
        form.setVerticalSpacing(10)
        r = 0
        self.num = QSpinBox(); self.num.setRange(1, 200)
        self.all = QCheckBox("All highlights (no limit)")
        self.all.toggled.connect(lambda on: self.num.setEnabled(not on))
        nums = QHBoxLayout(); nums.addWidget(self.num); nums.addWidget(self.all); nums.addStretch(1)
        form.addWidget(label("Number of clips"), r, 0); form.addLayout(nums, r, 1); r += 1
        self.min_len = QSpinBox(); self.min_len.setRange(5, 60); self.min_len.setSuffix(" s")
        self.max_len = QSpinBox(); self.max_len.setRange(5, 60); self.max_len.setSuffix(" s")
        self.min_len.valueChanged.connect(lambda v: self.max_len.setValue(max(v, self.max_len.value())))
        self.max_len.valueChanged.connect(lambda v: self.min_len.setValue(min(v, self.min_len.value())))
        lens = QHBoxLayout(); lens.addWidget(self.min_len); lens.addWidget(label("to")); lens.addWidget(self.max_len)
        form.addWidget(label("Clip length"), r, 0); form.addLayout(lens, r, 1); r += 1

        form.addWidget(label("What counts as a highlight:"), r, 0, 1, 2); r += 1
        self.kills = QCheckBox("My kills and assists")
        self.multi = QCheckBox("Multikills")
        self.fights = QCheckBox("Teamfights (kills during a big fight score higher)")
        self.loud_only = QCheckBox("Also include loud fights where no kill was seen (may pick up creep farming)")
        self.objectives = QCheckBox("Big objective moments (coming soon)")
        self.objectives.setEnabled(False)
        self.objectives.setToolTip("Needs a screenshot of a Guardian/Walker/Mid Boss going down "
                                   "to build this. Send one to Claude.")
        for cb in (self.kills, self.multi, self.fights, self.loud_only, self.objectives):
            form.addWidget(cb, r, 0, 1, 2); r += 1
        self.no_deaths = QCheckBox("Skip moments where I die (clips end before the death)")
        form.addWidget(self.no_deaths, r, 0, 1, 2); r += 1

        form.addWidget(label("Fight sensitivity"), r, 0)
        sens = QHBoxLayout()
        self.sens = QSlider(Qt.Horizontal); self.sens.setRange(1, 10)
        sens.addWidget(label("Low", "subtle")); sens.addWidget(self.sens, 1); sens.addWidget(label("High", "subtle"))
        form.addLayout(sens, r, 1); r += 1
        form.addWidget(label("Low = only the biggest fights. High = small skirmishes count too.", "subtle", True),
                       r, 0, 1, 2); r += 1
        self.full = QCheckBox("Quick scan (faster, but can miss kills during laning or quiet moments)")
        form.addWidget(self.full, r, 0, 1, 2); r += 1

        form.addWidget(label("Save clips to"), r, 0)
        out = QHBoxLayout()
        self.out_dir = QLineEdit()
        pick = button("Browse...")
        pick.clicked.connect(self._pick_folder)
        out.addWidget(self.out_dir, 1); out.addWidget(pick)
        form.addLayout(out, r, 1); r += 1
        form.setRowStretch(r, 1)
        left = QWidget(); left.setLayout(form)
        body.addWidget(left, 3)

        # Right: vertical format previews
        right = QVBoxLayout()
        right.addWidget(label("Export as:"))
        orient = QHBoxLayout()
        self.out_vertical = QRadioButton("Vertical 9:16 (Shorts / Reels)")
        self.out_horizontal = QRadioButton("Horizontal 16:9 (YouTube)")
        og = QButtonGroup(self)
        og.addButton(self.out_vertical); og.addButton(self.out_horizontal)
        orient.addWidget(self.out_vertical); orient.addWidget(self.out_horizontal); orient.addStretch(1)
        right.addLayout(orient)
        self.out_vertical.toggled.connect(self._orientation_changed)

        right.addWidget(label("Format (click one):"))
        opts = QHBoxLayout()
        self.opt_blur = FormatOption("Full frame + blurred background", "Shows the whole picture.")
        self.opt_zoom = FormatOption("Zoomed gameplay + blurred background",
                                     "Gameplay is bigger; its edges are cut off.")
        self.opt_layout = FormatOption("My layout", "Your own layers: gameplay, webcam, items, text...")
        group = QButtonGroup(self)
        for opt in (self.opt_blur, self.opt_zoom, self.opt_layout):
            group.addButton(opt.radio)
            opts.addWidget(opt)
        right.addLayout(opts)
        layout_row = QHBoxLayout()
        self.layout_btn = button("\u270E Edit my layout...")
        self.layout_btn.clicked.connect(self.edit_layout.emit)
        self.layout_lbl = label("", "subtle", wrap=True)
        layout_row.addWidget(self.layout_btn)
        layout_row.addWidget(self.layout_lbl, 1)
        right.addLayout(layout_row)
        zoom_row = QHBoxLayout()
        zoom_row.addWidget(label("Zoom (gameplay)"))
        self.zoom = QSlider(Qt.Horizontal)
        self.zoom.setRange(110, 250)
        self.zoom.setSingleStep(5)
        self.zoom_lbl = label("", "subtle")
        self.zoom.valueChanged.connect(lambda v: self.zoom_lbl.setText(f"{v}%"))
        self.zoom.sliderReleased.connect(lambda: self.zoom_changed.emit(self.zoom.value() / 100))
        zoom_row.addWidget(self.zoom, 1)
        zoom_row.addWidget(self.zoom_lbl)
        right.addLayout(zoom_row)
        self.same_shape_lbl = label("Your video already has this shape: full frame is just resized, "
                                    "zoom cuts in on the middle.", "subtle", wrap=True)
        right.addWidget(self.same_shape_lbl)

        right.addSpacing(8)
        right.addWidget(label("Kill badge area:"))
        self.badge_lbl = label("", "subtle", wrap=True)
        right.addWidget(self.badge_lbl)
        badge_btn = button("Set kill badge area...")
        badge_btn.clicked.connect(self._set_badge_area)
        right.addWidget(badge_btn, alignment=Qt.AlignLeft)
        right.addStretch(1)
        self._previews = {}
        body.addLayout(right, 2)

        nav = QHBoxLayout()
        back = button("Back")
        back.clicked.connect(self.back.emit)
        go = button("Find highlights", primary=True)
        go.clicked.connect(self._start)
        nav.addWidget(back); nav.addStretch(1); nav.addWidget(go)
        outer.addLayout(nav)

    def load(self, settings, info):
        self._settings = settings
        name = Path(info.path).name
        self.video_label.setText(f"{name}  •  {fmt_time(info.duration)}  •  "
                                 f"{info.width}x{info.height} @ {info.fps:.0f} fps")
        self.num.setValue(settings.num_clips or 5)
        self.all.setChecked(settings.num_clips == 0)
        self.max_len.setValue(settings.max_len)
        self.min_len.setValue(settings.min_len)
        self.kills.setChecked(settings.want_kills)
        self.multi.setChecked(settings.want_multikills)
        self.fights.setChecked(settings.want_teamfights)
        self.loud_only.setChecked(settings.want_loud_only)
        self.no_deaths.setChecked(settings.exclude_deaths)
        self.sens.setValue(settings.sensitivity)
        self.full.setChecked(settings.quick_scan)
        self.out_dir.setText(settings.output_dir)
        {"zoom": self.opt_zoom, "custom": self.opt_layout}.get(settings.vertical_format, self.opt_blur).radio.setChecked(True)
        self.zoom.setValue(int(round(settings.zoom_level * 100)))
        self._info = info
        self._previews = {}
        (self.out_horizontal if settings.output_orientation == "horizontal" else self.out_vertical).setChecked(True)
        self._orientation_changed()
        self._update_badge_label()
        self._update_layout_label()

    def _update_layout_label(self):
        from ..composition import template_key
        comp = (self._settings.my_layouts or {}).get(template_key(self._info, self._orientation()))
        if not comp:
            self.layout_lbl.setText("Not made yet for this video and export shape - click 'Edit my layout'.")
        else:
            names = [lay["name"] if lay["type"] == "video" else "text" for lay in reversed(comp["layers"])]
            self.layout_lbl.setText("Layers (front to back): " + ", ".join(names))

    def _orientation(self):
        return "horizontal" if self.out_horizontal.isChecked() else "vertical"

    def _orientation_changed(self, *_):
        o = self._orientation()
        for opt in (self.opt_blur, self.opt_zoom, self.opt_layout):
            opt.set_shape(o)
        if getattr(self, "_settings", None) is not None:
            self._update_layout_label()
        info = getattr(self, "_info", None)
        same = info is not None and (info.height > info.width) == (o == "vertical") and \
            abs(info.width / info.height - (9 / 16 if o == "vertical" else 16 / 9)) < 0.02
        self.same_shape_lbl.setVisible(same)
        self._show_previews()

    def set_previews(self, paths):
        self._previews.update(paths)
        self._show_previews()

    def _show_previews(self):
        o = self._orientation()
        if (o, "blur") in self._previews:
            self.opt_blur.set_image(self._previews[(o, "blur")])
            self.opt_zoom.set_image(self._previews[(o, "zoom")])
        if (o, "custom") in self._previews:
            self.opt_layout.set_image(self._previews[(o, "custom")])

    def _update_badge_label(self):
        info = self._info
        portrait = info.height > info.width
        saved = self._settings.badge_region_portrait if portrait else self._settings.badge_region_landscape
        shape = "vertical" if portrait else "horizontal"
        self.badge_lbl.setText(
            f"Your video is {info.width}x{info.height} ({shape}). "
            + ("Using the area you set for " + shape + " videos." if saved else
               "Using the default area. If kills are missed, set it once using a moment with a kill."))

    def _set_badge_area(self):
        from ..settings import badge_region, save_settings
        from .badge_area import BadgeAreaDialog
        dlg = BadgeAreaDialog(self)
        dlg.open_video(self._info, badge_region(self._settings, self._info))
        if dlg.exec() == QDialog.Accepted:
            region = [round(v, 4) for v in dlg.region()]
            if self._info.height > self._info.width:
                self._settings.badge_region_portrait = region
            else:
                self._settings.badge_region_landscape = region
            save_settings(self._settings)
            log.info("Kill badge area set to %s", region)
            self._update_badge_label()

    def _pick_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Where should clips be saved?", self.out_dir.text())
        if path:
            self.out_dir.setText(path)

    def current(self):
        return replace(
            self._settings,
            num_clips=0 if self.all.isChecked() else self.num.value(), min_len=self.min_len.value(), max_len=self.max_len.value(),
            vertical_format=("custom" if self.opt_layout.radio.isChecked() else
                             "zoom" if self.opt_zoom.radio.isChecked() else "blur"),
            zoom_level=self.zoom.value() / 100,
            output_orientation=self._orientation(),
            want_kills=self.kills.isChecked(), want_multikills=self.multi.isChecked(),
            want_teamfights=self.fights.isChecked(), want_loud_only=self.loud_only.isChecked(),
            want_objectives=False,
            exclude_deaths=self.no_deaths.isChecked(), sensitivity=self.sens.value(),
            quick_scan=self.full.isChecked(), output_dir=self.out_dir.text().strip(),
        ).validate()

    def _start(self):
        s = self.current()
        if not (s.want_kills or s.want_multikills or s.want_loud_only):
            from .common import show_error
            show_error(self, "Tick at least one kind of highlight.")
            return
        self.start.emit(s)


# ---------------------------------------------------------------- Progress

class ProgressPage(QWidget):
    cancel = Signal()

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(60, 60, 60, 60)
        lay.addStretch(1)
        self.title = label("", "title")
        self.step = label("", "subtle")
        self.bar = QProgressBar(); self.bar.setRange(0, 1000); self.bar.setTextVisible(False)
        self.pct = label("", "subtle")
        lay.addWidget(self.title, alignment=Qt.AlignCenter)
        lay.addSpacing(10)
        lay.addWidget(self.bar)
        lay.addWidget(self.step, alignment=Qt.AlignCenter)
        lay.addWidget(self.pct, alignment=Qt.AlignCenter)
        lay.addStretch(1)
        self.cancel_btn = button("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        lay.addWidget(self.cancel_btn, alignment=Qt.AlignCenter)

    def begin(self, title):
        self.title.setText(title)
        self.step.setText("Starting...")
        self.pct.setText("")
        self.bar.setValue(0)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Cancel")

    def update(self, fraction, message):
        self.bar.setValue(int(fraction * 1000))
        if message:
            self.step.setText(message)
        self.pct.setText(f"{fraction * 100:.0f}%")

    def _cancel(self):
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("Cancelling...")
        self.cancel.emit()


# ---------------------------------------------------------------- Review

class LoudnessGraph(QWidget):
    """The whole match at a glance: loudness, the fight threshold, kills, deaths and chosen clips."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(90)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.data = None

    def set_data(self, events, clips, duration):
        self.data = (events, clips, duration)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#1e2027"))
        if not self.data:
            return
        events, clips, duration = self.data
        curve = events.sustained
        if len(curve) == 0 or duration <= 0:
            return
        lo, hi = float(np.percentile(curve, 1)), float(curve.max())
        hi = max(hi, events.threshold + 1, lo + 1)
        y = lambda v: h - 8 - (v - lo) / (hi - lo) * (h - 20)  # noqa: E731
        x = lambda t: t / duration * w  # noqa: E731

        for c in clips:
            p.fillRect(QRectF(x(c.start), 0, max(2.0, x(c.end) - x(c.start)), h), QColor(217, 69, 43, 70))

        buckets = np.array_split(curve, max(1, min(w, len(curve))))
        p.setPen(QPen(QColor("#7aa2f7"), 1))
        prev = None
        for i, b in enumerate(buckets):
            pt = (i / len(buckets) * w, y(float(b.max())))
            if prev:
                p.drawLine(QLineF(*prev, *pt))
            prev = pt

        p.setPen(QPen(QColor("#f0b35a"), 1, Qt.DashLine))
        p.drawLine(QLineF(0, y(events.threshold), w, y(events.threshold)))

        p.setPen(QPen(QColor("#ff4d4d"), 2))
        for t in events.takedowns:
            p.drawLine(QLineF(x(t), 0, x(t), 10))
        p.setPen(QPen(QColor("#bbbbbb"), 2))
        for t in events.deaths:
            p.drawLine(QLineF(x(t), h - 10, x(t), h))
        p.end()


class ClickableLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, e):
        self.clicked.emit()


class ClipRow(QFrame):
    edit = Signal(object)
    delete = Signal(object)

    def __init__(self, index, clip):
        super().__init__()
        self.setObjectName("card")
        self.clip = clip
        self.index = index
        lay = QHBoxLayout(self)
        self.check = QCheckBox()
        self.check.setChecked(True)
        self.check.setToolTip("Export this clip")
        lay.addWidget(self.check)
        self.thumb = ClickableLabel()
        self.thumb.setFixedSize(192, 108)
        self.thumb.setCursor(Qt.PointingHandCursor)
        self.thumb.setToolTip("Click to play and edit")
        self.thumb.setStyleSheet("background: #0d0e11; border-radius: 4px;")
        self.thumb.clicked.connect(lambda: self.edit.emit(self))
        lay.addWidget(self.thumb)

        mid = QVBoxLayout()
        self.title = label("")
        self.title.setStyleSheet("font-weight: 600; background: transparent;")
        mid.addWidget(self.title)
        reasons = label(" \u2022 ".join(clip.reasons), "reason", wrap=True)
        reasons.setStyleSheet("background: transparent;")
        mid.addWidget(reasons)
        self.detail = label("", "subtle")
        self.detail.setStyleSheet("background: transparent;")
        mid.addWidget(self.detail)
        mid.addStretch(1)
        lay.addLayout(mid, 1)

        btns = QVBoxLayout()
        edit = button("\u25B6 Play / Edit", primary=True)
        edit.clicked.connect(lambda: self.edit.emit(self))
        rm = button("\u2715 Delete")
        rm.clicked.connect(lambda: self.delete.emit(self))
        btns.addWidget(edit)
        btns.addWidget(rm)
        lay.addLayout(btns)
        self.refresh()

    def refresh(self):
        c = self.clip
        self.title.setText(f"Clip {self.index}   {fmt_time(c.start)} \u2192 {fmt_time(c.end)}"
                           f"   \u2022   {c.length:.1f}s   \u2022   score {c.score:g}")
        if c.comp:
            texts = sum(1 for lay in c.comp["layers"] if lay["type"] == "text")
            videos = sum(1 for lay in c.comp["layers"] if lay["type"] == "video")
            self.detail.setText(f"\u270E Edited \u2022 {videos} video layer{'s' if videos != 1 else ''}"
                                + (f" \u2022 {texts} text" if texts else ""))
        else:
            self.detail.setText("")

    def valid(self):
        return 3 <= self.clip.length <= 60

    def set_thumb(self, path):
        pix = QPixmap(str(path))
        if not pix.isNull():
            self.thumb.setFixedSize(*((84, 150) if pix.height() > pix.width() else (192, 108)))
            self.thumb.setPixmap(pix.scaled(self.thumb.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))


class ReviewPage(QWidget):
    repick = Signal(int, int)  # sensitivity, number of clips (0 = all)
    edit = Signal(object)  # ClipRow
    export = Signal(list)
    back = Signal()
    back_settings = Signal()

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 16)
        lay.addWidget(label("Review your clips", "title"))
        self.summary = label("", "subtle", wrap=True)
        lay.addWidget(self.summary)

        self.graph = LoudnessGraph()
        lay.addWidget(self.graph)
        lay.addWidget(label("Blue = game loudness, dashed = fight threshold, red ticks = your kills/assists, "
                            "grey ticks = deaths, shaded = chosen clips", "subtle", wrap=True))

        tune = QHBoxLayout()
        tune.addWidget(label("Fight sensitivity"))
        self.sens = QSlider(Qt.Horizontal); self.sens.setRange(1, 10); self.sens.setFixedWidth(200)
        tune.addWidget(self.sens)
        tune.addWidget(label("Clips"))
        self.num = QSpinBox(); self.num.setRange(1, 200)
        tune.addWidget(self.num)
        self.all = QCheckBox("All highlights")
        self.all.toggled.connect(lambda on: self.num.setEnabled(not on))
        tune.addWidget(self.all)
        again = button("Re-pick clips")
        again.clicked.connect(lambda: self.repick.emit(self.sens.value(),
                                                      0 if self.all.isChecked() else self.num.value()))
        tune.addWidget(again)
        tune.addStretch(1)
        lay.addLayout(tune)

        pick = QHBoxLayout()
        sel_all = button("Select all")
        sel_none = button("Select none")
        sel_all.clicked.connect(lambda: self._select(True))
        sel_none.clicked.connect(lambda: self._select(False))
        pick.addWidget(sel_all)
        pick.addWidget(sel_none)
        pick.addWidget(label("Only ticked clips are exported.", "subtle"))
        pick.addStretch(1)
        seen = button("See what was detected")
        seen.setToolTip("A picture of every moment that looked like your kill badge (logs\\detections.jpg)")
        seen.clicked.connect(self._open_detections)
        pick.addWidget(seen)
        lay.addLayout(pick)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.list_host = QWidget()
        self.list = QVBoxLayout(self.list_host)
        self.list.addStretch(1)
        self.scroll.setWidget(self.list_host)
        lay.addWidget(self.scroll, 1)

        nav = QHBoxLayout()
        back_settings = button("\u2190 Back to settings")
        back_settings.clicked.connect(self.back_settings.emit)
        back = button("Start over")
        back.clicked.connect(self.back.emit)
        nav.addWidget(back_settings)
        self.export_btn = button("Export clips", primary=True)
        self.export_btn.clicked.connect(self._export)
        nav.addWidget(back); nav.addStretch(1); nav.addWidget(self.export_btn)
        lay.addLayout(nav)
        self.rows = []

    def show_results(self, info, events, clips, settings):
        self.sens.setValue(settings.sensitivity)
        self.all.setChecked(settings.num_clips == 0)
        self.num.setValue(settings.num_clips or 5)
        for row in self.rows:
            row.setParent(None)
            row.deleteLater()
        self.rows = []
        for i, clip in enumerate(clips, 1):
            row = ClipRow(i, clip)
            row.edit.connect(self.edit.emit)
            row.check.toggled.connect(self._update_export)
            row.delete.connect(self._delete)
            self.list.insertWidget(self.list.count() - 1, row)
            self.rows.append(row)
        n = lambda count, word: f"{count} {word}{'' if count == 1 else 's'}"  # noqa: E731
        found = (f"Found {n(len(events.takedowns), 'kill/assist badge')}, {n(len(events.deaths), 'death')} and "
                 f"{n(len(events.fights), 'loud fight')} in {fmt_time(info.duration)} of gameplay.")
        if not clips:
            found += "  No clips matched."
        self.summary.setText(found)
        self.graph.set_data(events, clips, info.duration)
        self._update_export()

    def set_thumbs(self, paths):
        for row, path in zip(self.rows, paths):
            if path:
                row.set_thumb(path)

    def _delete(self, row):
        self.rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        self._update_export()

    def _open_detections(self):
        from ..analysis import DETECTIONS_IMAGE
        if DETECTIONS_IMAGE.exists():
            open_in_explorer(DETECTIONS_IMAGE)
        else:
            from .common import show_error
            show_error(self, "Nothing that looked like a kill badge was found in this video.")

    def _select(self, on):
        for row in self.rows:
            row.check.setChecked(on)

    def _checked(self):
        return [r for r in self.rows if r.check.isChecked()]

    def _update_export(self, *_):
        n = len(self._checked())
        self.export_btn.setText(f"Export {n} of {len(self.rows)} clip{'s' if len(self.rows) != 1 else ''}")
        self.export_btn.setEnabled(n > 0)

    def _export(self):
        rows = self._checked()
        bad = [r.index for r in rows if not r.valid()]
        if bad:
            from .common import show_error
            show_error(self, f"Clip {', '.join(map(str, bad))} must be between 3 and 60 seconds long.")
            return
        self.export.emit([r.clip for r in rows])


# ---------------------------------------------------------------- Done

class DonePage(QWidget):
    again = Signal()
    back = Signal()

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(60, 40, 60, 40)
        lay.addWidget(label("Your clips are ready!", "title"))
        self.files = label("", "subtle", wrap=True)
        self.files.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.files)
        lay.addStretch(1)
        row = QHBoxLayout()
        self.open_btn = button("Open clips folder", primary=True)
        back = button("\u2190 Back to clips")
        back.clicked.connect(self.back.emit)
        again = button("Make more clips")
        again.clicked.connect(self.again.emit)
        row.addWidget(back); row.addWidget(again); row.addStretch(1); row.addWidget(self.open_btn)
        lay.addLayout(row)
        self._folder = None
        self.open_btn.clicked.connect(lambda: self._folder and open_in_explorer(self._folder))

    def show_files(self, files):
        self._folder = str(Path(files[0]).parent) if files else None
        self.files.setText("\n".join(Path(f).name for f in files) + (f"\n\nSaved in: {self._folder}" if files else ""))
