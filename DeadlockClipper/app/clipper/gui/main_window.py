"""Main window: moves between the screens and runs the work in the background."""
import logging
from dataclasses import replace

from PySide6.QtWidgets import QMainWindow, QMessageBox, QStackedWidget

from .. import APP_NAME, APP_VERSION
from ..analysis import analyze, write_report
from ..errors import FriendlyError
from ..export import export_clips, vertical_filter
from ..ffmpeg import extract_frame, probe
from ..highlights import find_events, pick_clips
from ..paths import CACHE_DIR
from ..settings import load_settings, save_settings
from ..youtube import download
from .common import show_error, start_task
from .editor import EditorDialog
from .pages import DonePage, HomePage, ProgressPage, ReviewPage, SettingsPage

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1100, 760)
        self.settings = load_settings()
        self.info = None
        self.analysis = None
        self.events = None
        self.task = None

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.home, self.setup, self.progress = HomePage(), SettingsPage(), ProgressPage()
        self.review, self.done = ReviewPage(), DonePage()
        self.editor = EditorDialog(self)
        for page in (self.home, self.setup, self.progress, self.review, self.done):
            self.stack.addWidget(page)

        self.home.file_chosen.connect(self.open_file)
        self.home.url_chosen.connect(self.open_url)
        self.setup.back.connect(lambda: self.stack.setCurrentWidget(self.home))
        self.setup.start.connect(self.run_analysis)
        self.progress.cancel.connect(self._cancel)
        self.review.repick.connect(self.repick)
        self.review.edit.connect(self.edit_clip)
        self.editor.saved.connect(self._clip_saved)
        self.review.export.connect(self.run_export)
        self.review.back.connect(self.go_home)
        self.done.again.connect(self.go_home)

    # ------------------------------------------------------------ helpers

    def go_home(self):
        self.stack.setCurrentWidget(self.home)

    def _cancel(self):
        if self.task:
            self.task.cancel()

    def _run(self, title, fn, on_done, back_to):
        self.progress.begin(title)
        self.stack.setCurrentWidget(self.progress)

        def failed(message, details):
            self.stack.setCurrentWidget(back_to)
            show_error(self, message, details)

        self.task = start_task(self, fn, on_progress=self.progress.update, on_done=on_done,
                               on_failed=failed, on_cancelled=lambda: self.stack.setCurrentWidget(back_to))

    # ------------------------------------------------------------ input

    def open_file(self, path):
        log.info("Opening file %s", path)
        try:
            self.info = probe(path)
        except FriendlyError as e:
            show_error(self, e.message, e.details)
            return
        self.show_settings()

    def open_url(self, url):
        def work(progress, cancel):
            return probe(download(url, progress, cancel))

        def done(info):
            self.info = info
            self.show_settings()

        self._run("Downloading from YouTube", work, done, self.home)

    def show_settings(self):
        self.setup.load(self.settings, self.info)
        self.stack.setCurrentWidget(self.setup)
        info = self.info

        def previews(progress, cancel):
            t = min(info.duration * 0.4, max(0.0, info.duration - 1))
            out = {}
            for fmt in ("blur", "crop"):
                out[fmt] = extract_frame(info.path, t, CACHE_DIR / f"preview_{fmt}.png",
                                         filter_complex=vertical_filter(fmt))
            return out

        start_task(self, previews, on_done=self.setup.set_previews,
                   on_failed=lambda m, d: log.warning("Preview failed: %s %s", m, d))

    # ------------------------------------------------------------ analysis

    def run_analysis(self, settings):
        self.settings = settings
        save_settings(settings)
        info = self.info

        def work(progress, cancel):
            analysis = analyze(info, settings.quick_scan, progress, cancel)
            progress(1.0, "Picking the best moments")
            events = find_events(analysis, settings.sensitivity)
            clips = pick_clips(events, settings, info.duration)
            write_report(info, events, clips, settings, analysis["segments"])
            return analysis, events, clips

        def done(result):
            self.analysis, self.events, clips = result
            self.show_review(clips)

        self._run("Finding highlights", work, done, self.setup)

    def repick(self, sensitivity, num_clips):
        self.settings = replace(self.settings, sensitivity=sensitivity, num_clips=num_clips).validate()
        save_settings(self.settings)
        self.events = find_events(self.analysis, self.settings.sensitivity)
        clips = pick_clips(self.events, self.settings, self.info.duration)
        write_report(self.info, self.events, clips, self.settings, self.analysis["segments"])
        self.show_review(clips)

    def show_review(self, clips):
        self.review.show_results(self.info, self.events, clips, self.settings)
        self.stack.setCurrentWidget(self.review)
        info = self.info

        def thumbs(progress, cancel):
            folder = CACHE_DIR / "thumbs"
            folder.mkdir(exist_ok=True)
            out = []
            for i, c in enumerate(clips):
                try:
                    out.append(extract_frame(info.path, c.focus + 0.5, folder / f"clip_{i}.jpg", width=384))
                except Exception:
                    log.exception("Thumbnail failed for clip %d", i)
                    out.append(None)
            return out

        start_task(self, thumbs, on_done=self.review.set_thumbs)

    def edit_clip(self, row):
        self._editing = row
        self.editor.open(self.info, row.clip, row.index, self.settings.vertical_format)
        self.editor.show()
        self.editor.raise_()

    def _clip_saved(self, clip):
        row = self._editing
        row.refresh()
        info = self.info
        start_task(self, lambda progress, cancel: extract_frame(
            info.path, clip.focus + 0.5, CACHE_DIR / "thumbs" / f"edited_{id(clip)}.jpg", width=384),
            on_done=row.set_thumb)

    # ------------------------------------------------------------ export

    def run_export(self, clips):
        info, settings = self.info, self.settings

        def work(progress, cancel):
            return export_clips(info, clips, settings, progress, cancel)

        def done(files):
            self.done.show_files(files)
            self.stack.setCurrentWidget(self.done)

        self._run("Exporting clips", work, done, self.review)

    def closeEvent(self, event):
        on_review = self.stack.currentWidget() is self.review and self.review.rows
        if on_review and QMessageBox.question(
                self, "Quit Deadlock Clipper?",
                "Quit the app? Clips you haven't exported yet will be lost.\n"
                "(The scan is saved, so reopening the same video is quick.)") != QMessageBox.Yes:
            event.ignore()
            return
        if self.task:
            self.task.cancel()
        self.editor.stop()
        super().closeEvent(event)
