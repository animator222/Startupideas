"""YouTube downloads with yt-dlp (best available quality)."""
import logging
from pathlib import Path

from .errors import Cancelled, FriendlyError
from .ffmpeg import ffmpeg_path
from .paths import DOWNLOAD_DIR

log = logging.getLogger(__name__)

FAILED = "YouTube download failed — try the local file instead."


class _Logger:
    def debug(self, msg):
        if not msg.startswith("[download]"):
            log.debug("yt-dlp: %s", msg)

    def info(self, msg):
        log.info("yt-dlp: %s", msg)

    def warning(self, msg):
        log.warning("yt-dlp: %s", msg)

    def error(self, msg):
        log.error("yt-dlp: %s", msg)


def download(url, progress, cancel):
    try:
        import yt_dlp
    except ImportError as e:
        raise FriendlyError(FAILED + "\n(yt-dlp isn't installed - run Install.bat again.)", str(e)) from e

    def hook(d):
        if cancel.is_set():
            raise Cancelled()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            frac = d.get("downloaded_bytes", 0) / total if total else 0.0
            progress(min(0.99, frac), "Downloading from YouTube")
        elif d.get("status") == "finished":
            progress(0.99, "Joining video and audio")

    opts = {
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mkv",
        "outtmpl": str(DOWNLOAD_DIR / "%(title).80s [%(id)s].%(ext)s"),
        "ffmpeg_location": str(Path(ffmpeg_path()).parent),
        "noplaylist": True,
        "restrictfilenames": True,
        "progress_hooks": [hook],
        "logger": _Logger(),
        "quiet": True,
        "no_warnings": True,
    }
    log.info("Downloading %s", url)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info.get("_type") == "playlist":
                info = info["entries"][0]
            downloads = info.get("requested_downloads") or []
            path = downloads[0].get("filepath") if downloads else ydl.prepare_filename(info)
    except Cancelled:
        raise
    except Exception as e:
        if isinstance(getattr(e, "exc_info", [None, None])[1], Cancelled) or "Cancelled" in repr(e):
            raise Cancelled() from e
        log.exception("YouTube download failed")
        raise FriendlyError(FAILED, str(e)) from e
    if not path or not Path(path).is_file():
        raise FriendlyError(FAILED, f"Downloaded file not found: {path}")
    log.info("Downloaded to %s", path)
    return path
