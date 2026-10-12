import faulthandler
import logging
import platform
import sys
from logging.handlers import RotatingFileHandler

from . import APP_VERSION
from .paths import LOG_DIR, LOG_FILE

_fault_file = None


def setup_logging():
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.DEBUG)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(handler)
    if sys.stderr is not None:  # pythonw has no console
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        root.addHandler(console)

    # Hard crashes (e.g. inside OpenCV or Qt) are written here.
    global _fault_file
    _fault_file = open(LOG_DIR / "crash.log", "a", encoding="utf-8")
    faulthandler.enable(_fault_file)

    def excepthook(exc_type, exc, tb):
        logging.getLogger("crash").critical("Unhandled error", exc_info=(exc_type, exc, tb))

    sys.excepthook = excepthook
    log = logging.getLogger("startup")
    log.info("=" * 70)
    log.info("Deadlock Clipper %s starting", APP_VERSION)
    log.info("Python %s on %s", sys.version.split()[0], platform.platform())
