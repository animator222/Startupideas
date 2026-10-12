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
    vertical_format: str = "blur"  # "blur" (full frame + blurred background) or "crop" (center crop)
    want_kills: bool = True
    want_multikills: bool = True
    want_teamfights: bool = True
    want_loud_only: bool = False  # loud fights with no kill badge (can include creep farming)
    want_objectives: bool = False
    exclude_deaths: bool = True
    output_dir: str = ""
    sensitivity: int = 5  # 1 = only the biggest fights, 10 = small skirmishes too
    full_scan: bool = False  # scan every part of the video, not just the loud parts
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
        if self.vertical_format not in ("blur", "crop"):
            self.vertical_format = "blur"
        return self


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
