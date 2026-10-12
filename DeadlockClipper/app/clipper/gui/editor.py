"""Clip editor: a live preview of the finished clip with layers (gameplay, webcam/items video
layers, text), a properties panel, playback controls and a trim timeline.

Also used without a clip ("template" mode) to edit "My layout", the default for all clips."""
import copy
import logging
import shutil

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPixmap, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QSlider, QStackedWidget, QVBoxLayout, QWidget,
)

from ..composition import canvas_size, new_text_layer, new_video_layer, simple_comp
from ..ffmpeg import ffmpeg_path, run
from ..highlights import fmt_time
from ..paths import CACHE_DIR
from ..textrender import font_families
from .canvas import CompositionCanvas, layer_color
from .common import start_task
from .timeline import MAX_LEN, MIN_LEN, Timeline, fmt_precise

log = logging.getLogger(__name__)
STRIP_FRAMES = 16
ACCENT = "#ff5a36"

EDITOR_STYLE = f"""
QDialog#editor {{ background: #0f1116; }}
QFrame#topbar {{ background: #14171f; border-bottom: 1px solid #232838; }}
QFrame#panel {{ background: #14171f; border: 1px solid #232838; border-radius: 10px; }}
QFrame#stage {{ background: #0b0c10; border: 1px solid #232838; border-radius: 10px; }}
QFrame#transport {{ background: #14171f; border-top: 1px solid #232838; }}
QLabel {{ background: transparent; }}
QLabel#h1 {{ font-size: 17px; font-weight: 700; color: #f2f3f7; }}
QLabel#chip {{ background: #2a2216; color: #f0b35a; border-radius: 10px; padding: 3px 10px; font-size: 12px; }}
QLabel#section {{ color: #7d8499; font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
QLabel#muted {{ color: #8a90a2; font-size: 12px; }}
QLabel#time {{ color: #dfe2ea; font-family: Consolas, 'DejaVu Sans Mono', monospace; font-size: 13px; }}
QPushButton {{ background: #1d2130; border: 1px solid #2c3246; border-radius: 7px; padding: 6px 12px; color: #e6e8ee; }}
QPushButton:hover {{ background: #262b3d; border-color: #3a4260; }}
QPushButton:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 700; }}
QPushButton#primary:hover {{ background: #ff7352; }}
QPushButton#ghost {{ background: transparent; border-color: transparent; color: #c9ccd6; }}
QPushButton#ghost:hover {{ background: #1d2130; }}
QPushButton#play {{ background: {ACCENT}; border: none; border-radius: 20px; color: white; font-size: 16px;
                    min-width: 40px; min-height: 40px; max-width: 40px; max-height: 40px; padding: 0; }}
QPushButton#seg {{ border-radius: 0; padding: 6px 18px; }}
QPushButton#segL {{ border-top-right-radius: 0; border-bottom-right-radius: 0; padding: 6px 18px; }}
QPushButton#segR {{ border-top-left-radius: 0; border-bottom-left-radius: 0; padding: 6px 18px; }}
QListWidget {{ background: #0f1116; border: 1px solid #232838; border-radius: 8px; padding: 4px; outline: none; }}
QListWidget::item {{ padding: 8px 6px; border-radius: 6px; color: #e6e8ee; }}
QListWidget::item:selected {{ background: #2a2f44; }}
QLineEdit, QPlainTextEdit, QComboBox {{ background: #0f1116; border: 1px solid #2c3246; border-radius: 7px;
                                        padding: 6px; color: #f2f3f7; }}
QComboBox QAbstractItemView {{ background: #14171f; color: #f2f3f7; selection-background-color: #2a2f44; }}
QSlider::groove:horizontal {{ height: 4px; background: #2c3246; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: white; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
"""


def _label(text, name=None, wrap=False):
    w = QLabel(text)
    if name:
        w.setObjectName(name)
    w.setWordWrap(wrap)
    return w


def _button(text, name=None, tip=None, checkable=False):
    b = QPushButton(text)
    b.setCursor(Qt.PointingHandCursor)
    b.setFocusPolicy(Qt.NoFocus)
    if name:
        b.setObjectName(name)
    if tip:
        b.setToolTip(tip)
    b.setCheckable(checkable)
    return b


def _panel():
    f = QFrame()
    f.setObjectName("panel")
    return f


class ColorButton(QPushButton):
    """A swatch that opens a colour picker."""
    color_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.setFixedSize(46, 30)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.clicked.connect(self._pick)
        self.set_color("#ffffff")

    def set_color(self, color):
        self._color = color
        self.setStyleSheet(f"background: {color}; border: 2px solid #3a4260; border-radius: 7px;")

    def _pick(self):
        c = QColorDialog.getColor(QColor(self._color), self, "Choose a colour")
        if c.isValid():
            self.set_color(c.name())
            self.color_changed.emit(c.name())


class LabeledSlider(QWidget):
    changed = Signal(float)

    def __init__(self, lo, hi, fmt):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(lo, hi)
        self.slider.setFocusPolicy(Qt.NoFocus)
        self.value_lbl = _label("", "muted")
        self.value_lbl.setFixedWidth(48)
        self.value_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.fmt = fmt
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.value_lbl)
        self.slider.valueChanged.connect(self._changed)

    def _changed(self, v):
        self.value_lbl.setText(self.fmt(v))
        self.changed.emit(v)

    def set(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(v)))
        self.slider.blockSignals(False)
        self.value_lbl.setText(self.fmt(self.slider.value()))


class EditorDialog(QDialog):
    saved = Signal(object)  # the clip (clip mode)
    apply_all = Signal(object)  # layout to apply to every clip
    template_saved = Signal(object)  # layout saved as "My layout"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("editor")
        self.setWindowTitle("Clip editor")
        self.setWindowFlags(Qt.Window | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.setStyleSheet(EDITOR_STYLE)
        self.resize(1440, 900)
        self.info = self.clip = self.comp = None
        self.template = False
        self.stop_at = None
        self._prime = False
        self._strip_job = 0
        self._sync = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_topbar())

        body = QHBoxLayout()
        body.setContentsMargins(14, 14, 14, 10)
        body.setSpacing(12)
        body.addWidget(self._build_layers_panel())
        body.addWidget(self._build_stage(), 1)
        body.addWidget(self._build_properties_panel())
        root.addLayout(body, 1)
        root.addWidget(self._build_transport())

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.sink = QVideoSink(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)

        for key, fn in ((Qt.Key_Space, self.toggle_play), (Qt.Key_Left, lambda: self._step(-1)),
                        (Qt.Key_Right, lambda: self._step(1)),
                        (QKeySequence("Shift+Left"), lambda: self._frame(-1)),
                        (QKeySequence("Shift+Right"), lambda: self._frame(1)),
                        (Qt.Key_I, self.mark_in), (Qt.Key_O, self.mark_out),
                        (Qt.Key_Delete, self._delete_layer)):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WindowShortcut)
            sc.activated.connect(lambda fn=fn: None if self._typing() else fn())

    # ================================================================ building the UI

    def _build_topbar(self):
        bar = QFrame()
        bar.setObjectName("topbar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(18, 10, 14, 10)
        self.title = _label("", "h1")
        self.chip = _label("", "chip")
        lay.addWidget(self.title)
        lay.addSpacing(10)
        lay.addWidget(self.chip)
        lay.addStretch(1)
        self.save_tpl_btn = _button("★ Save as my layout", "ghost",
                                    "Use this layout (without per-clip text) as the default for every clip")
        self.apply_all_btn = _button("↻ Apply layout to all clips", "ghost",
                                     "Give every clip in the list this layout (each keeps its own text)")
        cancel = _button("Cancel")
        self.save_btn = _button("Save", "primary")
        self.save_tpl_btn.clicked.connect(self._save_template)
        self.apply_all_btn.clicked.connect(self._apply_all)
        cancel.clicked.connect(self.reject)
        self.save_btn.clicked.connect(self._save)
        for w in (self.save_tpl_btn, self.apply_all_btn, cancel, self.save_btn):
            lay.addWidget(w)
        return bar

    def _build_layers_panel(self):
        panel = _panel()
        panel.setFixedWidth(250)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.addWidget(_label("LAYERS", "section"))
        lay.addWidget(_label("Top of the list = in front. Untick to hide.", "muted", wrap=True))
        self.layers = QListWidget()
        self.layers.currentRowChanged.connect(self._list_selected)
        self.layers.itemChanged.connect(self._list_item_changed)
        lay.addWidget(self.layers, 1)
        add = QHBoxLayout()
        add_video = _button("+ Video", tip="Add another part of the video (webcam, items, minimap...)")
        add_text = _button("+ Text")
        add_video.clicked.connect(self._add_video)
        add_text.clicked.connect(self._add_text)
        add.addWidget(add_video)
        add.addWidget(add_text)
        lay.addLayout(add)
        order = QHBoxLayout()
        up = _button("▲", tip="Bring forward")
        down = _button("▼", tip="Send backward")
        dup = _button("⎘", tip="Duplicate")
        delete = _button("✕", tip="Delete layer (Del)")
        up.clicked.connect(lambda: self._move_layer(1))
        down.clicked.connect(lambda: self._move_layer(-1))
        dup.clicked.connect(self._duplicate_layer)
        delete.clicked.connect(self._delete_layer)
        for b in (up, down, dup, delete):
            order.addWidget(b)
        lay.addLayout(order)
        lay.addSpacing(8)
        lay.addWidget(_label("QUICK START", "section"))
        quick = QHBoxLayout()
        full = _button("Full frame")
        zoomed = _button("Zoomed")
        full.clicked.connect(lambda: self._reset_layout(1.0))
        zoomed.clicked.connect(lambda: self._reset_layout(1.6))
        quick.addWidget(full)
        quick.addWidget(zoomed)
        lay.addLayout(quick)
        return panel

    def _build_stage(self):
        stage = QFrame()
        stage.setObjectName("stage")
        lay = QVBoxLayout(stage)
        lay.setContentsMargins(10, 10, 10, 10)
        top = QHBoxLayout()
        self.view_canvas = _button("▣  Canvas", "segL", checkable=True)
        self.view_source = _button("▢  Source video", "segR", checkable=True)
        group = QButtonGroup(self)
        group.addButton(self.view_canvas)
        group.addButton(self.view_source)
        self.view_canvas.setChecked(True)
        self.view_canvas.clicked.connect(lambda: self._set_mode("canvas"))
        self.view_source.clicked.connect(lambda: self._set_mode("source"))
        top.addStretch(1)
        top.addWidget(self.view_canvas)
        top.addWidget(self.view_source)
        top.addStretch(1)
        lay.addLayout(top)
        self.mode_hint = _label("", "muted")
        self.mode_hint.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.mode_hint)
        self.canvas = CompositionCanvas()
        self.canvas.selected.connect(self._canvas_selected)
        self.canvas.changed.connect(self._refresh_properties)
        lay.addWidget(self.canvas, 1)
        return stage

    def _build_properties_panel(self):
        panel = _panel()
        panel.setFixedWidth(310)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.addWidget(_label("PROPERTIES", "section"))
        self.props = QStackedWidget()
        self.props.addWidget(_label("Select a layer to change it.", "muted", wrap=True))
        self.props.addWidget(self._build_video_props())
        self.props.addWidget(self._build_text_props())
        lay.addWidget(self.props, 1)
        return panel

    def _build_video_props(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.addWidget(_label("Name"))
        self.v_name = QLineEdit()
        self.v_name.textEdited.connect(self._set_name)
        lay.addWidget(self.v_name)
        lay.addSpacing(6)
        lay.addWidget(_label("Size on the canvas"))
        self.v_size = LabeledSlider(5, 600, lambda v: f"{v}%")
        self.v_size.changed.connect(self._set_video_size)
        lay.addWidget(self.v_size)
        row = QHBoxLayout()
        for text, fn in (("Fit", self._fit_width), ("Fill", self._fill_canvas), ("Center", self._center_layer)):
            b = _button(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        lay.addSpacing(10)
        lay.addWidget(_label("Which part of the video?"))
        self.v_src_btn = _button("▢  Mark it on the source video")
        self.v_src_btn.clicked.connect(lambda: self._set_mode("source"))
        lay.addWidget(self.v_src_btn)
        lay.addWidget(_label("Switch to Source video and drag/resize this layer's box over the webcam, "
                             "item bar or whatever it should show. Back on Canvas, drag it anywhere and "
                             "resize it from a corner. Mouse wheel zooms.", "muted", wrap=True))
        lay.addStretch(1)
        return w

    def _build_text_props(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.addWidget(_label("Text"))
        self.t_text = QPlainTextEdit()
        self.t_text.setFixedHeight(70)
        self.t_text.textChanged.connect(self._set_text)
        lay.addWidget(self.t_text)
        lay.addWidget(_label("Font"))
        self.t_font = QComboBox()
        self.t_font.currentTextChanged.connect(lambda f: self._set_text_prop("font", f))
        lay.addWidget(self.t_font)
        row = QHBoxLayout()
        self.t_bold = _button("B", checkable=True, tip="Extra bold (for Montserrat, Oswald, Poppins...)")
        self.t_bold.setFont(QFont("", -1, QFont.Bold))
        self.t_bold.toggled.connect(lambda on: self._set_text_prop("bold", on))
        self.t_color = ColorButton()
        self.t_color.color_changed.connect(lambda c: self._set_text_prop("color", c))
        row.addWidget(_label("Colour"))
        row.addWidget(self.t_color)
        row.addStretch(1)
        row.addWidget(self.t_bold)
        lay.addLayout(row)
        lay.addWidget(_label("Size"))
        self.t_size = LabeledSlider(8, 250, lambda v: f"{v}")
        self.t_size.changed.connect(lambda v: self._set_text_prop("size", v / 1000))
        lay.addWidget(self.t_size)

        lay.addSpacing(6)
        srow = QHBoxLayout()
        self.t_shadow = QCheckBox("Shadow")
        self.t_shadow.toggled.connect(lambda on: self._set_text_prop("shadow", on))
        self.t_shadow_color = ColorButton()
        self.t_shadow_color.color_changed.connect(lambda c: self._set_text_prop("shadow_color", c))
        srow.addWidget(self.t_shadow)
        srow.addStretch(1)
        srow.addWidget(self.t_shadow_color)
        lay.addLayout(srow)
        self.t_shadow_strength = LabeledSlider(0, 100, lambda v: f"{v}%")
        self.t_shadow_strength.changed.connect(lambda v: self._set_text_prop("shadow_strength", v / 100))
        lay.addWidget(self.t_shadow_strength)

        orow = QHBoxLayout()
        self.t_outline = QCheckBox("Outline")
        self.t_outline.toggled.connect(lambda on: self._set_text_prop("outline", on))
        self.t_outline_color = ColorButton()
        self.t_outline_color.color_changed.connect(lambda c: self._set_text_prop("outline_color", c))
        orow.addWidget(self.t_outline)
        orow.addStretch(1)
        orow.addWidget(self.t_outline_color)
        lay.addLayout(orow)
        self.t_outline_width = LabeledSlider(1, 30, lambda v: f"{v}")
        self.t_outline_width.changed.connect(lambda v: self._set_text_prop("outline_width", v / 100))
        lay.addWidget(self.t_outline_width)

        lay.addSpacing(6)
        center = _button("Center horizontally")
        center.clicked.connect(lambda: self._set_text_prop("cx", 0.5))
        lay.addWidget(center)
        lay.addWidget(_label("Drag the text on the canvas to move it; drag a corner to resize.", "muted", True))
        lay.addStretch(1)
        return w

    def _build_transport(self):
        bar = QFrame()
        bar.setObjectName("transport")
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(16, 10, 16, 12)
        row = QHBoxLayout()
        self.back5 = _button("⏪", tip="5 seconds back")
        self.prev_f = _button("◀|", tip="One frame back (Shift+←)")
        self.play_btn = _button("▶", "play", tip="Play / pause (Space)")
        self.next_f = _button("|▶", tip="One frame forward (Shift+→)")
        self.fwd5 = _button("⏩", tip="5 seconds forward")
        self.back5.clicked.connect(lambda: self._step(-5))
        self.fwd5.clicked.connect(lambda: self._step(5))
        self.prev_f.clicked.connect(lambda: self._frame(-1))
        self.next_f.clicked.connect(lambda: self._frame(1))
        self.play_btn.clicked.connect(self.toggle_play)
        self.time_lbl = _label("0:00.0", "time")
        self.sel_btn = _button("▶ Play clip", tip="Play from the clip start to its end")
        self.sel_btn.clicked.connect(self.play_selection)
        self.mute = _button("\U0001F50A", checkable=True, tip="Mute")
        self.mute.toggled.connect(lambda on: (self.audio.setMuted(on), self.mute.setText("\U0001F507" if on else "\U0001F50A")))
        for w in (self.back5, self.prev_f, self.play_btn, self.next_f, self.fwd5):
            row.addWidget(w)
        row.addSpacing(12)
        row.addWidget(self.time_lbl)
        row.addStretch(1)
        self.sel_lbl = _label("", "muted")
        row.addWidget(self.sel_lbl)
        row.addSpacing(10)
        row.addWidget(self.sel_btn)
        row.addWidget(self.mute)
        lay.addLayout(row)

        self.timeline = Timeline()
        self.timeline.seek.connect(self.seek)
        self.timeline.trimmed.connect(self._trimmed)
        lay.addWidget(self.timeline)

        trim = QHBoxLayout()
        self.set_in = _button("[  Set start here  (I)")
        self.set_out = _button("Set end here  (O)  ]")
        self.widen = _button("↔ Show more timeline")
        self.set_in.clicked.connect(self.mark_in)
        self.set_out.clicked.connect(self.mark_out)
        self.widen.clicked.connect(self._widen)
        trim.addWidget(self.set_in)
        trim.addWidget(self.set_out)
        trim.addStretch(1)
        trim.addWidget(_label("Space play/pause • ←/→ 1s • Shift+←/→ frame • "
                              "I/O start/end • Del delete layer", "muted"))
        trim.addStretch(1)
        trim.addWidget(self.widen)
        lay.addLayout(trim)
        return bar

    # ================================================================ open / close

    def open_clip(self, info, clip, index, comp):
        """Edit one clip: its time range and its layout."""
        self.template = False
        self.clip = clip
        self.s0, self.s1 = clip.start, clip.end
        self.title.setText(f"Edit clip {index}")
        self.chip.setText(" • ".join(clip.reasons) or "Clip")
        self.chip.setVisible(True)
        self.apply_all_btn.setVisible(True)
        self.save_tpl_btn.setVisible(True)
        self.save_btn.setText("Save")
        for w in (self.set_in, self.set_out, self.sel_btn, self.sel_lbl):
            w.setVisible(True)
        self._open(info, comp, clip.start, clip.end)

    def open_template(self, info, comp, at):
        """Edit "My layout" on a frame of the video (no trimming)."""
        self.template = True
        self.clip = None
        self.s0, self.s1 = at, min(info.duration, at + 20)
        self.title.setText("My layout")
        self.chip.setVisible(False)
        self.apply_all_btn.setVisible(False)
        self.save_tpl_btn.setVisible(False)
        self.save_btn.setText("Save layout")
        for w in (self.set_in, self.set_out, self.sel_btn, self.sel_lbl):
            w.setVisible(False)
        self._open(info, comp, self.s0, self.s1)

    def _open(self, info, comp, start, end):
        self.info = info
        self.comp = copy.deepcopy(comp)
        cw, ch = canvas_size(self.comp)
        self.setWindowTitle(f"Clip editor — {cw}x{ch}")
        if self.t_font.count() == 0:
            for fam in font_families():
                self.t_font.addItem(fam)
                self.t_font.setItemData(self.t_font.count() - 1, QFont(fam, 13), Qt.FontRole)
        self.canvas.set_comp(info, self.comp)
        self._rebuild_list(select=0)
        self._set_mode("canvas")
        pad = max(20.0, (end - start) * 0.6)
        self._set_view(max(0.0, start - pad), min(info.duration, end + pad))
        self.timeline.set_selection(self.s0, self.s1)
        self.timeline.set_position(self.s0)
        self._update_labels(self.s0)
        self.stop_at = None
        self._prime = True
        self.audio.setMuted(True)
        self.player.setSource(QUrl.fromLocalFile(info.path))

    def stop(self):
        self.player.stop()
        self.player.setSource(QUrl())

    def done(self, result):
        self.stop()
        super().done(result)

    def _typing(self):
        return isinstance(self.focusWidget(), (QLineEdit, QPlainTextEdit))

    def _save(self):
        if self.template:
            self.template_saved.emit(copy.deepcopy(self.comp))
        else:
            self.clip.start, self.clip.end = round(self.s0, 2), round(self.s1, 2)
            self.clip.comp = copy.deepcopy(self.comp)
            if not self.clip.start <= self.clip.focus <= self.clip.end:
                self.clip.focus = (self.clip.start + self.clip.end) / 2
            log.info("Clip edited: %.2f-%.2f, %d layers", self.clip.start, self.clip.end, len(self.comp["layers"]))
            self.saved.emit(self.clip)
        self.accept()

    def _save_template(self):
        comp = copy.deepcopy(self.comp)
        comp["layers"] = [lay for lay in comp["layers"] if lay["type"] == "video"]  # text is per clip
        self.template_saved.emit(comp)
        self.chip.setText("★ Saved as my layout")
        self.chip.setVisible(True)

    def _apply_all(self):
        self.apply_all.emit(copy.deepcopy(self.comp))
        self.chip.setText("↻ Layout applied to all clips")

    # ================================================================ layers

    def _rebuild_list(self, select=None):
        """The list shows the front-most layer first (the composition stores back-to-front)."""
        self._sync = True
        self.layers.clear()
        n = len(self.comp["layers"])
        for row in range(n):
            i = n - 1 - row
            lay = self.comp["layers"][i]
            icon = "\U0001F3AE" if lay["type"] == "video" else "T"
            name = lay["name"] if lay["type"] == "video" else (str(lay.get("text") or "Text").split("\n")[0][:22] or "Text")
            item = QListWidgetItem(f"{icon}   {name}")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked if lay.get("hidden") else Qt.Checked)
            item.setForeground(layer_color(i) if not lay.get("hidden") else QColor("#555b6e"))
            self.layers.addItem(item)
        self._sync = False
        if select is not None:
            self._select_layer(select)

    def _row_to_index(self, row):
        return len(self.comp["layers"]) - 1 - row

    def _select_layer(self, i):
        i = max(-1, min(i, len(self.comp["layers"]) - 1))
        self._sync = True
        self.layers.setCurrentRow(self._row_to_index(i) if i >= 0 else -1)
        self._sync = False
        self.canvas.sel = i
        self.canvas.update()
        self._refresh_properties()

    def _list_selected(self, row):
        if self._sync:
            return
        self.canvas.select(self._row_to_index(row) if row >= 0 else -1)

    def _canvas_selected(self, i):
        self._sync = True
        self.layers.setCurrentRow(self._row_to_index(i) if i >= 0 else -1)
        self._sync = False
        self._refresh_properties()

    def _list_item_changed(self, item):
        if self._sync:
            return
        i = self._row_to_index(self.layers.row(item))
        self.comp["layers"][i]["hidden"] = item.checkState() != Qt.Checked
        self._rebuild_list(select=i)
        self.canvas.update()

    def _current(self):
        i = self.canvas.sel
        return self.comp["layers"][i] if 0 <= i < len(self.comp["layers"]) else None

    def _add_video(self):
        n = sum(1 for lay in self.comp["layers"] if lay["type"] == "video")
        name = "Webcam" if n == 1 else "Items" if n == 2 else f"Video {n}"
        self.comp["layers"].append(new_video_layer(self.info, self.comp, name))
        self._rebuild_list(select=len(self.comp["layers"]) - 1)
        self._set_mode("source")  # first job: mark which part of the video it shows

    def _add_text(self):
        self.comp["layers"].append(new_text_layer())
        self._rebuild_list(select=len(self.comp["layers"]) - 1)
        self._set_mode("canvas")
        self.t_text.setFocus()
        self.t_text.selectAll()

    def _delete_layer(self):
        i = self.canvas.sel
        if not (0 <= i < len(self.comp["layers"])):
            return
        if self.comp["layers"][i]["type"] == "video" and \
                sum(1 for lay in self.comp["layers"] if lay["type"] == "video") == 1:
            return  # keep at least one video layer
        del self.comp["layers"][i]
        self._rebuild_list(select=min(i, len(self.comp["layers"]) - 1))
        self.canvas.update()

    def _duplicate_layer(self):
        lay = self._current()
        if lay:
            dup = copy.deepcopy(lay)
            if dup["type"] == "video":
                dup["dst"] = [dup["dst"][0] + 0.03, dup["dst"][1] + 0.03, dup["dst"][2]]
                dup["name"] += " copy"
            else:
                dup["cy"] = min(0.95, dup["cy"] + 0.05)
            self.comp["layers"].insert(self.canvas.sel + 1, dup)
            self._rebuild_list(select=self.canvas.sel + 1)

    def _move_layer(self, step):
        i = self.canvas.sel
        j = i + step
        if 0 <= i < len(self.comp["layers"]) and 0 <= j < len(self.comp["layers"]):
            layers = self.comp["layers"]
            layers[i], layers[j] = layers[j], layers[i]
            self._rebuild_list(select=j)
            self.canvas.update()

    def _reset_layout(self, zoom):
        """Replace the gameplay layer's placement with full frame / zoomed, keeping other layers."""
        base = simple_comp(self.info, self.comp["canvas"], zoom)["layers"][0]
        for lay in self.comp["layers"]:
            if lay["type"] == "video":
                lay["src"], lay["dst"] = base["src"], base["dst"]
                break
        self.canvas.update()
        self._refresh_properties()

    # ================================================================ properties

    def _refresh_properties(self):
        lay = self._current()
        self._sync = True
        if lay is None:
            self.props.setCurrentIndex(0)
        elif lay["type"] == "video":
            self.props.setCurrentIndex(1)
            if self.v_name.text() != lay["name"]:
                self.v_name.setText(lay["name"])
            self.v_size.set(lay["dst"][2] * 100)
        else:
            self.props.setCurrentIndex(2)
            if self.t_text.toPlainText() != lay["text"]:
                self.t_text.setPlainText(lay["text"])
            idx = self.t_font.findText(lay["font"])
            self.t_font.blockSignals(True)
            self.t_font.setCurrentIndex(max(0, idx))
            self.t_font.blockSignals(False)
            self.t_bold.blockSignals(True)
            self.t_bold.setChecked(bool(lay.get("bold")))
            self.t_bold.blockSignals(False)
            self.t_color.set_color(lay["color"])
            self.t_size.set(lay["size"] * 1000)
            for box, key in ((self.t_shadow, "shadow"), (self.t_outline, "outline")):
                box.blockSignals(True)
                box.setChecked(bool(lay[key]))
                box.blockSignals(False)
            self.t_shadow_color.set_color(lay["shadow_color"])
            self.t_outline_color.set_color(lay["outline_color"])
            self.t_shadow_strength.set(lay["shadow_strength"] * 100)
            self.t_outline_width.set(lay["outline_width"] * 100)
        self._sync = False

    def _set_name(self, text):
        lay = self._current()
        if lay and lay["type"] == "video":
            lay["name"] = text.strip() or "Video"
            self._rebuild_list(select=self.canvas.sel)
            self.canvas.update()

    def _set_video_size(self, pct):
        lay = self._current()
        if lay and lay["type"] == "video" and not self._sync:
            lay["dst"] = [lay["dst"][0], lay["dst"][1], max(0.05, pct / 100)]
            self.canvas.update()

    def _fit_width(self):
        lay = self._current()
        if lay and lay["type"] == "video":
            cw, ch = canvas_size(self.comp)
            sw, sh = lay["src"][2] * self.info.width, lay["src"][3] * self.info.height
            lay["dst"] = [lay["dst"][0], lay["dst"][1], min(1.0, (ch / sh) * sw / cw) if sw / sh < cw / ch else 1.0]
            self.canvas.update()
            self._refresh_properties()

    def _fill_canvas(self):
        lay = self._current()
        if lay and lay["type"] == "video":
            cw, ch = canvas_size(self.comp)
            sw, sh = lay["src"][2] * self.info.width, lay["src"][3] * self.info.height
            lay["dst"] = [0.5, 0.5, max(cw / sw, ch / sh) * sw / cw]
            self.canvas.update()
            self._refresh_properties()

    def _center_layer(self):
        lay = self._current()
        if lay and lay["type"] == "video":
            lay["dst"] = [0.5, 0.5, lay["dst"][2]]
            self.canvas.update()

    def _set_text(self):
        lay = self._current()
        if lay and lay["type"] == "text" and not self._sync:
            lay["text"] = self.t_text.toPlainText()
            row = self.layers.currentRow()
            item = self.layers.item(row)
            if item:
                self._sync = True
                item.setText("T   " + (lay["text"].split("\n")[0][:22] or "Text"))
                self._sync = False
            self.canvas.update()

    def _set_text_prop(self, key, value):
        lay = self._current()
        if lay and lay["type"] == "text" and not self._sync:
            lay[key] = value
            self.canvas.update()

    def _set_mode(self, mode):
        (self.view_source if mode == "source" else self.view_canvas).setChecked(True)
        self.canvas.set_mode(mode)
        self.mode_hint.setText(
            "Source video: drag/resize the selected layer's box to choose what part of the video it shows"
            if mode == "source" else
            "Canvas: this is your finished clip. Drag layers to move them, drag a corner to resize")

    # ================================================================ playback

    def _on_frame(self, frame):
        if frame.isValid():
            img = frame.toImage()
            if not img.isNull():
                self.canvas.set_frame(img)

    def _on_status(self, status):
        if status == QMediaPlayer.LoadedMedia and self._prime:
            self.player.setPosition(int(self.s0 * 1000))
            self.player.play()

    def _on_state(self, state):
        self.play_btn.setText("⏸" if state == QMediaPlayer.PlayingState else "▶")

    def _on_error(self, error, message):
        log.error("Media player error %s: %s", error, message)
        self.mode_hint.setText("The preview can't play this file. Editing still works; the export is not affected.")

    def _on_position(self, ms):
        t = ms / 1000.0
        if self._prime and self.player.playbackState() == QMediaPlayer.PlayingState:
            self._prime = False
            self.player.pause()
            self.player.setPosition(int(self.s0 * 1000))
            self.audio.setMuted(self.mute.isChecked())
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

    # ================================================================ trimming

    def mark_in(self):
        if self.template:
            return
        self.s0 = min(max(self._now(), self.s1 - MAX_LEN), self.s1 - MIN_LEN)
        self.timeline.set_selection(self.s0, self.s1)
        self._update_labels(self._now())

    def mark_out(self):
        if self.template:
            return
        self.s1 = min(max(min(self._now(), self.s0 + MAX_LEN), self.s0 + MIN_LEN), self.info.duration)
        self.timeline.set_selection(self.s0, self.s1)
        self._update_labels(self._now())

    def _trimmed(self, s0, s1):
        if self.template:
            self.timeline.set_selection(self.s0, self.s1)
            return
        self.s0, self.s1 = s0, s1
        self._update_labels(self._now())

    def _update_labels(self, t):
        self.time_lbl.setText(f"{fmt_precise(t)} / {fmt_time(self.info.duration)}")
        self.sel_lbl.setText(f"Clip {fmt_precise(self.s0)} → {fmt_precise(self.s1)}  •  "
                             f"{self.s1 - self.s0:.1f}s")

    def _widen(self):
        v0, v1 = self.timeline.v0, self.timeline.v1
        self._set_view(max(0.0, v0 - 30), min(self.info.duration, v1 + 30))

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
