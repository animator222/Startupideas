"""Main window: moves between the screens and runs the work in the background."""
import logging
from dataclasses import replace

from PySide6.QtWidgets import QMainWindow, QMessageBox, QStackedWidget

from .. import APP_NAME, APP_VERSION
from ..analysis import analyze, write_report
from ..errors import FriendlyError
from ..composition import clean_comp, comp_for_clip, simple_comp, template_key
from ..export import export_clips, render_preview
from ..ffmpeg import probe
from ..highlights import find_events, pick_clips
from ..paths import CACHE_DIR
from ..settings import badge_region, load_settings, save_settings
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
        self._editing = None

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
        self.setup.zoom_changed.connect(lambda z: self._make_previews(("zoom",), z))
        self.setup.edit_layout.connect(self.edit_my_layout)
        self.progress.cancel.connect(self._cancel)
        self.review.repick.connect(self.repick)
        self.review.edit.connect(self.edit_clip)
        self.editor.saved.connect(self._clip_saved)
        self.editor.apply_all.connect(self._apply_to_all)
        self.editor.template_saved.connect(self._save_my_layout)
        self.review.export.connect(self.run_export)
        self.review.back.connect(self.go_home)
        self.review.back_settings.connect(self.show_settings)
        self.done.again.connect(self.go_home)
        self.done.back.connect(lambda: self.stack.setCurrentWidget(self.review))

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

        self._make_previews(("blur", "zoom", "custom"), self.settings.zoom_level)

    def _make_previews(self, formats, zoom):
        """Finished-frame previews for the settings screen, for both export shapes."""
        info = self.info
        self._preview_job = getattr(self, "_preview_job", 0) + 1
        job = self._preview_job
        comps = {}
        for orientation in ("vertical", "horizontal"):
            for fmt in formats:
                st = replace(self.setup._settings, vertical_format=fmt, output_orientation=orientation,
                             zoom_level=zoom)
                comps[(orientation, fmt)] = comp_for_clip(None, st, info)

        def previews(progress, cancel):
            t = min(info.duration * 0.4, max(0.0, info.duration - 1))
            return {key: render_preview(info, comp, t, CACHE_DIR / f"preview_{key[0]}_{key[1]}_{job}.png",
                                        tag=f"preview_{key[0]}_{key[1]}")
                    for key, comp in comps.items()}

        def done(paths):
            if job == self._preview_job:
                self.setup.set_previews(paths)

        start_task(self, previews, on_done=done,
                   on_failed=lambda m, d: log.warning("Preview failed: %s %s", m, d))

    def edit_my_layout(self):
        """Open the editor on 'My layout' for this video shape and the chosen export shape."""
        st = self.setup.current()
        key = template_key(self.info, st.output_orientation)
        comp = clean_comp((st.my_layouts or {}).get(key)) or simple_comp(self.info, st.output_orientation,
                                                                        st.zoom_level)
        self.editor.open_template(self.info, comp, at=self.info.duration * 0.4)
        self.editor.show()
        self.editor.raise_()

    def _save_my_layout(self, comp):
        comp = clean_comp(comp)
        if not comp:
            return
        key = template_key(self.info, comp["canvas"])
        self.settings.my_layouts = dict(self.settings.my_layouts or {})
        self.settings.my_layouts[key] = comp
        self.setup._settings.my_layouts = self.settings.my_layouts
        if self.stack.currentWidget() is self.setup or self.editor.template:
            self.setup.opt_layout.radio.setChecked(True)
            self.settings.vertical_format = "custom"
        save_settings(self.settings)
        log.info("Saved my layout %s with %d layers", key, len(comp["layers"]))
        self.setup._update_layout_label()
        self._make_previews(("custom",), self.settings.zoom_level)

    def _apply_to_all(self, comp):
        """Every clip gets this layout's video layers and keeps its own text."""
        videos = [lay for lay in comp["layers"] if lay["type"] == "video"]
        for row in self.review.rows:
            own_text = [lay for lay in (row.clip.comp or {}).get("layers", []) if lay["type"] == "text"]
            if row is self._editing:
                continue
            row.clip.comp = {"canvas": comp["canvas"], "layers": [dict(v) for v in videos] + own_text}
            row.refresh()
        self._refresh_thumbs([row for row in self.review.rows if row is not self._editing])

    # ------------------------------------------------------------ analysis

    def run_analysis(self, settings):
        self.settings = settings
        save_settings(settings)
        info = self.info

        def work(progress, cancel):
            analysis = analyze(info, settings.quick_scan, badge_region(settings, info), progress, cancel)
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
        self._refresh_thumbs(self.review.rows)

    def _refresh_thumbs(self, rows):
        """Thumbnails show the finished look (layout, text) of each clip."""
        info, settings = self.info, self.settings
        jobs = [(row, comp_for_clip(row.clip, settings, info), row.clip.focus + 0.5) for row in rows]
        self._thumb_job = getattr(self, "_thumb_job", 0) + 1
        job = self._thumb_job

        def thumbs(progress, cancel):
            folder = CACHE_DIR / "thumbs"
            folder.mkdir(exist_ok=True)
            out = []
            for k, (row, comp, t) in enumerate(jobs):
                try:
                    out.append(render_preview(info, comp, t, folder / f"thumb_{job}_{k}.jpg", tag=f"thumb{k}"))
                except Exception:
                    log.exception("Thumbnail failed")
                    out.append(None)
            return out

        def done(paths):
            for (row, _, _), path in zip(jobs, paths):
                try:
                    if path and row in self.review.rows:
                        row.set_thumb(path)
                except RuntimeError:  # the row was deleted meanwhile
                    pass

        start_task(self, thumbs, on_done=done)

    def edit_clip(self, row):
        self._editing = row
        comp = comp_for_clip(row.clip, self.settings, self.info)
        self.editor.open_clip(self.info, row.clip, row.index, comp)
        self.editor.show()
        self.editor.raise_()

    def _clip_saved(self, clip):
        self._editing.refresh()
        self._refresh_thumbs([self._editing])

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
