"""Rendering 1080x1920 vertical clips."""
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path

from .errors import Cancelled, FriendlyError
from .ffmpeg import ENCODER_ARGS, ffmpeg_path, find_tool, popen, working_encoders

log = logging.getLogger(__name__)

OUT_W, OUT_H = 1080, 1920


def vertical_filter(fmt, fps_str=None):
    """ffmpeg filter graph from input 0 to [v]. fmt: 'crop' or 'blur'."""
    tail = (f",fps={fps_str}" if fps_str else "") + ",format=yuv420p[v]"
    if fmt == "crop":
        return (f"[0:v:0]crop=trunc(ih*9/16/2)*2:ih,scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1" + tail)
    # Full frame in the middle, a blurred, slightly darkened zoomed copy filling the background.
    return (f"[0:v:0]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
            f"boxblur=10:2,eq=brightness=-0.06,scale={OUT_W}:{OUT_H},setsar=1[bg];"
            f"[fgsrc]scale={OUT_W}:-2:flags=lanczos,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2" + tail)


def recording_date(path):
    """OBS names files like '2026-09-25 20-11-03.mkv'; otherwise use the file's date."""
    m = re.search(r"(20\d\d)-(\d\d)-(\d\d)", Path(path).stem)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return datetime.fromtimestamp(Path(path).stat().st_mtime).strftime("%Y-%m-%d")


def output_paths(source, out_dir, count):
    out_dir = Path(out_dir)
    date = recording_date(source)
    taken = [int(m.group(1)) for p in out_dir.glob(f"deadlock_{date}_clip*.mp4")
             if (m := re.search(r"_clip(\d+)\.mp4$", p.name))]
    first = max(taken, default=0) + 1
    return [out_dir / f"deadlock_{date}_clip{first + i:02d}.mp4" for i in range(count)]


def _encode(info, clip, fmt, out_path, encoder, progress, cancel):
    duration = clip.end - clip.start
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-y",
           "-progress", "pipe:1", "-nostats",
           "-ss", f"{clip.start:.3f}", "-i", info.path, "-t", f"{duration:.3f}",
           "-filter_complex", vertical_filter(fmt, info.fps_str), "-map", "[v]"]
    if info.has_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    cmd += [*ENCODER_ARGS[encoder], "-pix_fmt", "yuv420p", "-map_metadata", "-1",
            "-movflags", "+faststart", str(out_path)]
    proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                 encoding="utf-8", errors="replace")
    try:
        for line in proc.stdout:
            if cancel.is_set():
                raise Cancelled()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    progress(min(1.0, int(line.split("=")[1]) / 1e6 / duration))
                except ValueError:
                    pass
    finally:
        if proc.poll() is None and cancel.is_set():
            proc.kill()
        proc.wait()
        err = proc.stderr.read()
    if cancel.is_set():
        Path(out_path).unlink(missing_ok=True)
        raise Cancelled()
    if proc.returncode != 0:
        log.error("Encoding with %s failed: %s", encoder, err[-3000:])
        Path(out_path).unlink(missing_ok=True)
        return False, err
    return True, ""


def export_clips(info, clips, settings, progress, cancel):
    """progress(fraction, message). Returns list of written files."""
    out_dir = Path(settings.output_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise FriendlyError(f"Can't create the output folder:\n{out_dir}\nPick a different folder.", str(e)) from e

    encoders = list(working_encoders())
    log.info("Exporting %d clips, encoders available: %s", len(clips), encoders)
    paths = output_paths(info.path, out_dir, len(clips))
    written = []
    for i, (clip, path) in enumerate(zip(clips, paths)):
        def clip_progress(f, i=i):
            progress((i + f) / len(clips), f"Exporting clip {i + 1} of {len(clips)}")
        clip_progress(0)
        last_err = ""
        for encoder in list(encoders):
            ok, last_err = _encode(info, clip, settings.vertical_format, path, encoder, clip_progress, cancel)
            if ok:
                log.info("Wrote %s with %s", path, encoder)
                break
            # Don't try a broken GPU encoder again for the remaining clips.
            if encoder != "libx264":
                encoders.remove(encoder)
        else:
            raise FriendlyError(f"Couldn't export clip {i + 1}. See the log for details.", last_err[-2000:])
        written.append(str(path))
    progress(1.0, "Done")
    return written


def preview_clip(info, clip):
    """Play a clip (with sound) in a small ffplay window. Returns the process."""
    ffplay = find_tool("ffplay")
    if not ffplay:
        raise FriendlyError("The clip player (ffplay) is missing. Run Install.bat again to repair the app.")
    cmd = [ffplay, "-hide_banner", "-loglevel", "error", "-autoexit",
           "-window_title", f"Clip preview  {clip.start:.1f}s - {clip.end:.1f}s  (close window to stop)",
           "-x", "960", "-y", "540",
           "-ss", f"{clip.start:.3f}", "-t", f"{clip.end - clip.start:.3f}", info.path]
    # Only the console is hidden; ffplay's video window still shows.
    return popen(cmd)
