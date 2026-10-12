"""Background tasks, error dialogs and styling shared by all screens."""
import logging
from pathlib import Path
import threading
import time
import traceback

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QApplication, QMessageBox

from ..errors import Cancelled, FriendlyError
from ..paths import LOG_DIR, LOG_FILE, open_in_explorer

log = logging.getLogger(__name__)

STYLE = """
QWidget { background: #15171c; color: #e8e8ea; font-size: 14px; }
QLabel#title { font-size: 22px; font-weight: 600; }
QLabel#subtle { color: #9a9ca5; }
QLabel#reason { color: #f0b35a; }
QPushButton { background: #2a2d36; border: 1px solid #3a3e4a; border-radius: 6px; padding: 7px 14px; }
QPushButton:hover { background: #343844; }
QPushButton:disabled { color: #666; }
QPushButton#primary { background: #d9452b; border-color: #d9452b; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #e85a40; }
QLineEdit, QSpinBox, QDoubleSpinBox { background: #1e2027; border: 1px solid #3a3e4a; border-radius: 5px; padding: 5px; }
QFrame#card { background: #1e2027; border: 1px solid #2c2f39; border-radius: 8px; }
QFrame#drop { border: 2px dashed #4a4f5e; border-radius: 12px; background: #1a1c22; }
QFrame#drop[hover="true"] { border-color: #d9452b; background: #221a1a; }
QFrame#option { border: 2px solid #2c2f39; border-radius: 8px; }
QFrame#option[selected="true"] { border-color: #d9452b; }
QProgressBar { border: 1px solid #3a3e4a; border-radius: 6px; text-align: center; height: 24px; background: #1e2027; }
QProgressBar::chunk { background: #d9452b; border-radius: 5px; }
QScrollArea { border: none; }
QCheckBox, QRadioButton { spacing: 8px; background: transparent; }
QCheckBox::indicator { width: 18px; height: 18px; border: 2px solid #6b7080; border-radius: 4px; background: #1e2027; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: #d9452b; }
QCheckBox::indicator:checked { background: #d9452b; border-color: #d9452b; image: url("CHECK_SVG"); }
QCheckBox::indicator:disabled { border-color: #3a3e4a; background: #15171c; }
QRadioButton::indicator { width: 14px; height: 14px; border: 2px solid #6b7080; border-radius: 9px; background: #1e2027; }
QRadioButton::indicator:checked { border-color: #d9452b; background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5, stop:0 #d9452b, stop:0.55 #d9452b, stop:0.65 #1e2027, stop:1 #1e2027); }
"""
STYLE = STYLE.replace("CHECK_SVG", str(Path(__file__).with_name("check.svg")).replace("\\", "/"))


def show_error(parent, message, details=""):
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("Something went wrong")
    box.setText(message)
    box.setInformativeText(f"Details were saved to the log file:\n{LOG_FILE}\n\n"
                           "If this keeps happening, send that file to Claude.")
    if details:
        box.setDetailedText(details[-4000:])
    open_btn = box.addButton("Open logs folder", QMessageBox.ActionRole)
    copy_btn = box.addButton("Copy error details", QMessageBox.ActionRole)
    box.addButton(QMessageBox.Ok)
    box.exec()
    if box.clickedButton() is open_btn:
        open_in_explorer(LOG_DIR)
    elif box.clickedButton() is copy_btn:
        QApplication.clipboard().setText(f"{message}\n\n{details}")


class Task(QObject):
    """Runs fn(progress, cancel_event) on a background thread."""
    progress = Signal(float, str)
    done = Signal(object)
    failed = Signal(str, str)
    cancelled = Signal()

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.cancel_event = threading.Event()
        self._last = 0.0

    def report(self, fraction, message=""):
        now = time.monotonic()
        if now - self._last >= 0.1 or fraction >= 1.0:
            self._last = now
            self.progress.emit(float(fraction), message)

    def cancel(self):
        self.cancel_event.set()

    @Slot()
    def run(self):
        try:
            result = self.fn(self.report, self.cancel_event)
        except Cancelled:
            log.info("Task cancelled")
            self.cancelled.emit()
        except FriendlyError as e:
            log.error("%s | %s", e.message, e.details)
            self.failed.emit(e.message, e.details)
        except Exception as e:
            log.exception("Unexpected error in background task")
            self.failed.emit(f"Something unexpected went wrong: {e}", traceback.format_exc())
        else:
            self.done.emit(result)


class _Relay(QObject):
    """Lives on the main thread so task callbacks safely run there (they touch widgets)."""

    def __init__(self, parent, on_progress, on_done, on_failed, on_cancelled):
        super().__init__(parent)
        self._cb = (on_progress, on_done, on_failed, on_cancelled)
        self.cleanup_cb = None

    @Slot(float, str)
    def progress(self, fraction, message):
        if self._cb[0]:
            self._cb[0](fraction, message)

    @Slot(object)
    def done(self, result):
        if self._cb[1]:
            self._cb[1](result)

    @Slot(str, str)
    def failed(self, message, details):
        if self._cb[2]:
            self._cb[2](message, details)

    @Slot()
    def cancelled(self):
        if self._cb[3]:
            self._cb[3]()

    @Slot()
    def cleanup(self):
        if self.cleanup_cb:
            self.cleanup_cb()


def start_task(owner, fn, on_progress=None, on_done=None, on_failed=None, on_cancelled=None):
    thread = QThread(owner)
    task = Task(fn)
    task.moveToThread(thread)
    relay = _Relay(owner, on_progress, on_done, on_failed, on_cancelled)
    thread.started.connect(task.run)
    task.progress.connect(relay.progress)
    task.done.connect(relay.done)
    task.failed.connect(relay.failed)
    task.cancelled.connect(relay.cancelled)
    for signal in (task.done, task.failed, task.cancelled):
        signal.connect(thread.quit)
    keep = owner.__dict__.setdefault("_running_tasks", set())
    entry = (thread, task, relay)
    keep.add(entry)

    def finished():
        keep.discard(entry)
        relay.deleteLater()
        thread.deleteLater()

    relay.cleanup_cb = finished
    thread.finished.connect(relay.cleanup)
    thread.start()
    return task
