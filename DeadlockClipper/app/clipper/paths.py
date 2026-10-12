import os
from pathlib import Path

# DeadlockClipper/ (the folder that contains Install.bat)
APP_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = APP_ROOT / "tools"


def _is_writable(folder):
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


def _data_dir():
    if _is_writable(APP_ROOT):
        return APP_ROOT
    fallback = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "DeadlockClipper"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


DATA_DIR = _data_dir()
LOG_DIR = DATA_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache"
DOWNLOAD_DIR = DATA_DIR / "downloads"
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = LOG_DIR / "deadlock_clipper.log"
ANALYSIS_REPORT = LOG_DIR / "last_analysis.txt"

for _d in (LOG_DIR, CACHE_DIR, DOWNLOAD_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def videos_folder():
    home = Path.home()
    for candidate in (home / "Videos", home / "OneDrive" / "Videos"):
        if candidate.is_dir():
            return candidate
    return home


def open_in_explorer(path):
    path = Path(path)
    if os.name == "nt":
        os.startfile(str(path))  # noqa: S606 - opens a folder for the user
    else:
        import subprocess
        subprocess.Popen(["xdg-open", str(path)])
