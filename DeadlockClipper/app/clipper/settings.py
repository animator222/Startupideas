import json
import logging
from dataclasses import asdict, dataclass, fields

from .paths import SETTINGS_FILE, videos_folder

log = logging.getLogger(__name__)


@dataclass
class Settings:
    num_clips: int = 5  # 0 = find all highlights (no limit)
    min_len: int = 15
    max_len: int = 45
    vertical_format: str = "blur"  # "blur" full frame, "zoom" zoomed gameplay, "custom" = my layout
    zoom_level: float = 1.4  # how much "zoom" enlarges the gameplay (1.4 = 140%)
    my_layouts: dict = None  # saved layouts, keyed "landscape>vertical" etc. (source shape > output shape)
    output_orientation: str = "vertical"  # "vertical" 1080x1920 or "horizontal" 1920x1080
    badge_region_landscape: list = None  # kill badge search area for horizontal videos (x, y, w, h)
    badge_region_portrait: list = None  # ... and for vertical videos
    want_kills: bool = True
    want_multikills: bool = True
    want_teamfights: bool = True
    want_loud_only: bool = False  # loud fights with no kill badge (can include creep farming)
    want_objectives: bool = False
    exclude_deaths: bool = True
    output_dir: str = ""
    sensitivity: int = 5  # 1 = only the biggest fights, 10 = small skirmishes too
    quick_scan: bool = False  # only scan the louder parts of the video (faster, can miss kills)
    pre_roll: float = 4.0  # seconds of context before the action
    post_roll: float = 3.0  # seconds after the action

    def __post_init__(self):
        if not self.output_dir:
            self.output_dir = str(videos_folder() / "Deadlock Clips")

    def validate(self):
        self.num_clips = max(0, min(200, int(self.num_clips)))
        self.max_len = max(5, min(60, int(self.max_len)))
        self.min_len = max(5, min(self.max_len, int(self.min_len)))
        self.sensitivity = max(1, min(10, int(self.sensitivity)))
        if self.vertical_format == "crop":  # "crop to fill" was replaced by zoom
            self.vertical_format = "zoom"
        from .composition import clean_comp
        layouts = self.my_layouts if isinstance(self.my_layouts, dict) else {}
        self.my_layouts = {k: c for k, c in ((k, clean_comp(v)) for k, v in layouts.items()) if c}
        if self.vertical_format == "layout":  # the old stacked layout became "my layout"
            self.vertical_format = "custom"
        if self.vertical_format not in ("blur", "zoom", "custom"):
            self.vertical_format = "blur"
        try:
            self.zoom_level = round(max(1.1, min(2.5, float(self.zoom_level))), 2)
        except (TypeError, ValueError):
            self.zoom_level = 1.4
        if self.output_orientation not in ("vertical", "horizontal"):
            self.output_orientation = "vertical"
        for name in ("badge_region_landscape", "badge_region_portrait"):
            r = getattr(self, name)
            setattr(self, name, [float(v) for v in r] if _valid_box(r) else None)
        return self


def _valid_box(r):
    ok = isinstance(r, (list, tuple)) and len(r) == 4 and all(isinstance(v, (int, float)) for v in r)
    return ok and r[2] > 0.02 and r[3] > 0.02 and r[0] >= 0 and r[1] >= 0 and \
        r[0] + r[2] <= 1.001 and r[1] + r[3] <= 1.001


def load_settings():
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        known = {f.name for f in fields(Settings)}
        return Settings(**{k: v for k, v in data.items() if k in known}).validate()
    except FileNotFoundError:
        return Settings()
    except Exception:
        log.exception("Could not read saved settings, using defaults")
        return Settings()


def save_settings(settings):
    try:
        SETTINGS_FILE.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    except OSError:
        log.exception("Could not save settings")


def badge_region(settings, info):
    """The kill badge search area to use for this video (saved one, or the default)."""
    from .vision import default_region
    saved = settings.badge_region_portrait if info.height > info.width else settings.badge_region_landscape
    return tuple(saved) if saved else default_region(info)

