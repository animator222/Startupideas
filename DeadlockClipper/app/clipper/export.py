"""Rendering 1080x1920 vertical clips."""
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path

from .errors import Cancelled, FriendlyError
from .composition import build_filter, canvas_size, comp_for_clip
from .ffmpeg import ENCODER_ARGS, ffmpeg_path, gpu_decode_args, popen, run, working_encoders
from .paths import CACHE_DIR
from .textrender import render_text_png

log = logging.getLogger(__name__)

TEXT_DIR = CACHE_DIR / "text"


def text_inputs(comp, folder, tag):
    """Render the composition's text layers to PNGs. Returns ({layer index: path}, ...)."""
    folder.mkdir(parents=True, exist_ok=True)
    cw, ch = canvas_size(comp)
    pngs = {}
    for i, lay in enumerate(comp["layers"]):
        if lay["type"] == "text" and not lay.get("hidden") and str(lay.get("text") or "").strip():
            pngs[i] = render_text_png(lay, cw, ch, folder / f"{tag}_text{i}.png")
    return pngs


def comp_inputs(comp, pngs, duration):
    """Extra ffmpeg inputs for the text PNGs and the mapping layer index -> input number."""
    args, mapping = [], {}
    for n, (i, path) in enumerate(sorted(pngs.items()), start=1):
        args += ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(path)]
        mapping[i] = n
    return args, mapping


def render_preview(info, comp, t, out_path, tag="preview"):
    """One finished frame of a composition (settings previews)."""
    pngs = text_inputs(comp, TEXT_DIR, tag)
    extra, mapping = comp_inputs(comp, pngs, 1.0)
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-y",
           "-ss", f"{max(0.0, t):.3f}", "-i", info.path, *extra,
           "-filter_complex", build_filter(info, comp, mapping), "-map", "[v]", "-frames:v", "1", str(out_path)]
    run(cmd, timeout=60)
    return out_path


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


def _encode(info, clip, comp, pngs, out_path, encoder, progress, cancel, gpu_decode):
    duration = clip.end - clip.start
    extra, mapping = comp_inputs(comp, pngs, duration)
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-y",
           "-progress", "pipe:1", "-nostats"]
    if gpu_decode:
        cmd += gpu_decode_args(info)
    cmd += ["-ss", f"{clip.start:.3f}", "-i", info.path, *extra, "-t", f"{duration:.3f}",
            "-filter_complex", build_filter(info, comp, mapping, info.fps_str), "-map", "[v]"]
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
    gpu_decode = True
    written = []
    for i, (clip, path) in enumerate(zip(clips, paths)):
        comp = comp_for_clip(clip, settings, info)
        pngs = text_inputs(comp, TEXT_DIR, f"clip{i}")
        log.info("Clip %d layout: %s", i + 1, comp)
        def clip_progress(f, i=i):
            progress((i + f) / len(clips), f"Exporting clip {i + 1} of {len(clips)}")
        clip_progress(0)
        last_err = ""
        for encoder in list(encoders):
            ok, last_err = _encode(info, clip, comp, pngs, path, encoder, clip_progress, cancel, gpu_decode)
            if not ok and gpu_decode:
                log.warning("Export with GPU decoding failed; decoding on the CPU from now on")
                gpu_decode = False
                ok, last_err = _encode(info, clip, comp, pngs, path, encoder, clip_progress, cancel, gpu_decode)
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
