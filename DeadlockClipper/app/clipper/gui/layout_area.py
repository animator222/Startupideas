"""'Stacked layout' setup: boxes for the gameplay, the webcam and the items on the source video."""
import logging

from PySide6.QtCore import Qt, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QSlider, QVBoxLayout

from ..highlights import fmt_time
from .badge_area import RegionBox
from .editor import VideoView
from .pages import button, label

log = logging.getLogger(__name__)

# Where new boxes start (fractions of the source frame) for horizontal and vertical videos.
DEFAULTS = {
    False: {"game": [0.0, 0.0, 1.0, 1.0], "cam": [0.02, 0.70, 0.22, 0.27], "items": [0.30, 0.86, 0.40, 0.13]},
    True: {"game": [0.0, 0.28, 1.0, 0.45], "cam": [0.0, 0.08, 1.0, 0.18], "items": [0.0, 0.74, 1.0, 0.16]},
}
BOXES = (("game", "GAMEPLAY", "#f0b35a"), ("cam", "WEBCAM", "#4fc3f7"), ("items", "ITEMS", "#81c784"))


class LayoutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Stacked layout")
        self.setModal(True)
        self.resize(1100, 860)
        self.info = None
        self._prime = False

        lay = QVBoxLayout(self)
        lay.addWidget(label("Set up the stacked layout", "title"))
        lay.addWidget(label(
            "Drag and resize the boxes over your video (drag inside a box to move it, drag its corner to "
            "resize). In the exported clip the WEBCAM goes at the top, the GAMEPLAY in the middle and "
            "the ITEMS at the bottom, on a blurred background. Untick what you don't need.",
            "subtle", wrap=True))

        ticks = QHBoxLayout()
        self.use_cam = QCheckBox("Webcam at the top")
        self.use_items = QCheckBox("Items at the bottom")
        ticks.addWidget(label("Gameplay (always used)"))
        ticks.addWidget(self.use_cam)
        ticks.addWidget(self.use_items)
        ticks.addStretch(1)
        lay.addLayout(ticks)

        self.boxes = {key: RegionBox(title, color, dim=False) for key, title, color in BOXES}
        self.view = VideoView(overlay=self.boxes["game"])
        for key in ("cam", "items"):
            self.view.scene().addItem(self.boxes[key])
        self.boxes["cam"].setZValue(2)
        self.boxes["items"].setZValue(2)
        lay.addWidget(self.view, 1)
        self.use_cam.toggled.connect(self.boxes["cam"].setVisible)
        self.use_items.toggled.connect(self.boxes["items"].setVisible)

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
        row.addWidget(label("Find a frame where your webcam and items are visible:"))
        row.addWidget(self.slider, 1)
        row.addWidget(self.time_lbl)
        lay.addLayout(row)

        bottom = QHBoxLayout()
        reset = button("Reset boxes")
        reset.clicked.connect(lambda: self._apply(DEFAULTS[self.info.height > self.info.width]))
        cancel = button("Cancel")
        cancel.clicked.connect(self.reject)
        save = button("Save", primary=True)
        save.clicked.connect(self.accept)
        bottom.addWidget(reset)
        bottom.addStretch(1)
        bottom.addWidget(cancel)
        bottom.addWidget(save)
        lay.addLayout(bottom)

    def open_video(self, info, layout):
        self.info = info
        self.view.set_frame_size(info.width, info.height)
        for box in self.boxes.values():
            box.prepareGeometryChange()
            box.size = self.view.overlay.size
        self._apply(layout or {"game": DEFAULTS[info.height > info.width]["game"]})
        self.slider.setRange(0, int(info.duration * 1000))
        self._prime = True
        self.player.setSource(QUrl.fromLocalFile(info.path))

    def _apply(self, layout):
        defaults = DEFAULTS[self.info.height > self.info.width]
        for key in ("game", "cam", "items"):
            self.boxes[key].set_region(layout.get(key) or defaults[key])
        self.use_cam.setChecked(bool(layout.get("cam")))
        self.use_items.setChecked(bool(layout.get("items")))
        self.boxes["cam"].setVisible(self.use_cam.isChecked())
        self.boxes["items"].setVisible(self.use_items.isChecked())

    def get_layout(self):
        def r(key):
            return [round(v, 4) for v in self.boxes[key].region()]
        return {"game": r("game"),
                "cam": r("cam") if self.use_cam.isChecked() else None,
                "items": r("items") if self.use_items.isChecked() else None}

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
