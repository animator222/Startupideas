"""'Stacked layout' setup: any number of sections (gameplay, webcam, items, chat...) marked on the
source video and stacked top to bottom in the exported vertical clip."""
import logging

from PySide6.QtCore import Qt, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLineEdit, QListWidget, QSlider, QVBoxLayout, QWidget,
)

from ..highlights import fmt_time
from .badge_area import RegionBox
from .editor import VideoView
from .pages import button, label

log = logging.getLogger(__name__)

DEFAULT_LAYOUT = {"sections": [{"name": "Gameplay", "box": [0.0, 0.0, 1.0, 1.0], "main": True}]}
# Starting point for vertical recordings: the gameplay is usually a band in the middle.
DEFAULT_LAYOUT_PORTRAIT = {"sections": [{"name": "Gameplay", "box": [0.0, 0.28, 1.0, 0.45], "main": True}]}
COLORS = ["#f0b35a", "#4fc3f7", "#81c784", "#e57373", "#ba68c8", "#fff176", "#4db6ac", "#ff8a65"]
NEW_SECTION_BOX = [0.35, 0.35, 0.30, 0.20]


class LayoutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Stacked layout")
        self.setModal(True)
        self.resize(1250, 860)
        self.info = None
        self._prime = False
        self.sections = []  # [{"name", "main", "box": RegionBox}] in order, top to bottom

        lay = QVBoxLayout(self)
        lay.addWidget(label("Set up the stacked layout", "title"))
        lay.addWidget(label(
            "Each section is a part of your video (gameplay, webcam, item bar, chat...). In the exported "
            "clip they are stacked from top to bottom in the order of the list, on a blurred background. "
            "The MAIN section (gameplay) fills the space the others leave. Drag inside a box to move it, "
            "drag its corner to resize.", "subtle", wrap=True))

        body = QHBoxLayout()
        lay.addLayout(body, 1)
        hidden = RegionBox(dim=False)
        self.view = VideoView(overlay=hidden)
        hidden.setVisible(False)
        body.addWidget(self.view, 1)

        side = QVBoxLayout()
        side.addWidget(label("Sections (top to bottom)"))
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._selected)
        side.addWidget(self.list, 1)
        side.addWidget(label("Name"))
        self.name = QLineEdit()
        self.name.textEdited.connect(self._rename)
        side.addWidget(self.name)
        self.main = QCheckBox("Main section (gameplay)")
        self.main.setToolTip("Fills the space the other sections leave; the Zoom slider sizes it.")
        self.main.clicked.connect(self._make_main)
        side.addWidget(self.main)
        for text, fn in (("+ Add section", self._add), ("Remove section", self._remove),
                         ("▲ Move up", lambda: self._move(-1)), ("▼ Move down", lambda: self._move(1))):
            b = button(text, primary=text.startswith("+"))
            b.clicked.connect(fn)
            side.addWidget(b)
        panel = QWidget()
        panel.setLayout(side)
        panel.setFixedWidth(250)
        body.addWidget(panel)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setMuted(True)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.view.video)
        self.player.positionChanged.connect(self._on_position)
        self.player.mediaStatusChanged.connect(self._on_status)

        row = QHBoxLayout()
        self.slider = QSlider(Qt.Horizontal)
        self.slider.sliderMoved.connect(lambda v: self.player.setPosition(v))
        self.time_lbl = label("", "subtle")
        row.addWidget(label("Find a frame where everything is visible:"))
        row.addWidget(self.slider, 1)
        row.addWidget(self.time_lbl)
        lay.addLayout(row)

        bottom = QHBoxLayout()
        reset = button("Reset")
        reset.clicked.connect(lambda: self._apply(self._default()))
        cancel = button("Cancel")
        cancel.clicked.connect(self.reject)
        save = button("Save", primary=True)
        save.clicked.connect(self.accept)
        bottom.addWidget(reset)
        bottom.addStretch(1)
        bottom.addWidget(cancel)
        bottom.addWidget(save)
        lay.addLayout(bottom)

    # ------------------------------------------------------------ sections

    def _default(self):
        return DEFAULT_LAYOUT_PORTRAIT if self.info.height > self.info.width else DEFAULT_LAYOUT

    def _new_box(self, region):
        used = {s["box"].color.name() for s in self.sections}
        color = next((c for c in COLORS if c not in used), COLORS[len(self.sections) % len(COLORS)])
        box = RegionBox("", color, dim=False)
        box.size = self.view.overlay.size
        box.set_region(region)
        self.view.scene().addItem(box)
        return box

    def _apply(self, layout):
        for sec in self.sections:
            self.view.scene().removeItem(sec["box"])
        self.sections = []
        for s in layout["sections"]:  # one at a time, so each box gets its own colour
            self.sections.append({"name": s["name"], "main": s["main"], "box": self._new_box(s["box"])})
        self._refresh(0)

    def _refresh(self, select=None):
        row = self.list.currentRow() if select is None else select
        self.list.blockSignals(True)
        self.list.clear()
        for i, sec in enumerate(self.sections):
            self.list.addItem(f"{i + 1}. {sec['name']}" + ("   ★ main" if sec["main"] else ""))
            sec["box"].title = f"{i + 1}. {sec['name']}" + (" ★" if sec["main"] else "")
            sec["box"].update()
        self.list.blockSignals(False)
        self.list.setCurrentRow(max(0, min(row, len(self.sections) - 1)))
        self._selected(self.list.currentRow())

    def _current(self):
        row = self.list.currentRow()
        return self.sections[row] if 0 <= row < len(self.sections) else None

    def _selected(self, row):
        for i, sec in enumerate(self.sections):
            sec["box"].setZValue(10 if i == row else 1)  # selected box on top, easy to grab
        sec = self._current()
        if sec:
            self.name.setText(sec["name"])
            self.main.setChecked(sec["main"])

    def _rename(self, text):
        sec = self._current()
        if sec:
            sec["name"] = text.strip() or "Section"
            self._refresh()

    def _make_main(self):
        sec = self._current()
        if sec:
            for other in self.sections:
                other["main"] = other is sec
            self._refresh()

    def _add(self):
        names = {s["name"] for s in self.sections}
        n = 1
        while f"Section {n}" in names:
            n += 1
        self.sections.append({"name": f"Section {n}", "main": False, "box": self._new_box(NEW_SECTION_BOX)})
        self._refresh(len(self.sections) - 1)

    def _remove(self):
        sec = self._current()
        if not sec or len(self.sections) == 1:
            return  # keep at least one section
        self.view.scene().removeItem(sec["box"])
        self.sections.remove(sec)
        if sec["main"]:
            biggest = max(self.sections, key=lambda s: s["box"].rect.width() * s["box"].rect.height())
            biggest["main"] = True
        self._refresh()

    def _move(self, step):
        row = self.list.currentRow()
        new = row + step
        if 0 <= row < len(self.sections) and 0 <= new < len(self.sections):
            self.sections[row], self.sections[new] = self.sections[new], self.sections[row]
            self._refresh(new)

    def get_layout(self):
        return {"sections": [{"name": s["name"], "main": s["main"],
                              "box": [round(v, 4) for v in s["box"].region()]} for s in self.sections]}

    # ------------------------------------------------------------ video

    def open_video(self, info, layout):
        self.info = info
        self.view.set_frame_size(info.width, info.height)
        self._apply(layout or self._default())
        self.slider.setRange(0, int(info.duration * 1000))
        self._prime = True
        self.player.setSource(QUrl.fromLocalFile(info.path))

    def done(self, result):
        self.player.stop()
        self.player.setSource(QUrl())
        super().done(result)

    def _on_status(self, status):
        if status == QMediaPlayer.LoadedMedia and self._prime:
            self.player.setPosition(int(self.info.duration * 400))
            self.player.play()

    def _on_position(self, ms):
        if self._prime and self.player.playbackState() == QMediaPlayer.PlayingState:
            self._prime = False
            self.player.pause()
            self.player.setPosition(ms)  # make sure the paused frame is drawn
        if not self.slider.isSliderDown():
            self.slider.setValue(ms)
        self.time_lbl.setText(f"{fmt_time(ms / 1000)} / {fmt_time(self.info.duration)}")
