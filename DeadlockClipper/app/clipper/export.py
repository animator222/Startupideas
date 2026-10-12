"""Rendering 1080x1920 vertical clips."""
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path

from .errors import Cancelled, FriendlyError
from .ffmpeg import ENCODER_ARGS, ffmpeg_path, popen, working_encoders

log = logging.getLogger(__name__)

OUTPUT_SIZES = {"vertical": (1080, 1920), "horizontal": (1920, 1080)}


def vertical_filter(fmt, fps_str=None, crop_x=0.5, orientation="vertical"):
    """ffmpeg filter graph from input 0 to [v], for any input shape.

    orientation: 'vertical' (1080x1920) or 'horizontal' (1920x1080) output.
    fmt 'crop': crop to fill the output; crop_x picks the position along the axis that gets
    cut (0 = left/top, 1 = right/bottom). fmt 'blur': whole frame, blurred copy fills the rest.
    If the input already has the output's shape, both simply scale it.
    """
    ow, oh = OUTPUT_SIZES.get(orientation, OUTPUT_SIZES["vertical"])
    aspect = ow / oh
    tail = (f",fps={fps_str}" if fps_str else "") + ",format=yuv420p[v]"
    if fmt == "crop":
        p = min(1.0, max(0.0, crop_x))
        return (f"[0:v:0]crop=w='trunc(min(iw,ih*{aspect:.6f})/2)*2':h='trunc(min(ih,iw/{aspect:.6f})/2)*2'"
                f":x='(iw-ow)*{p:.4f}':y='(ih-oh)*{p:.4f}',"
                f"scale={ow}:{oh}:flags=lanczos,setsar=1" + tail)
    bw, bh = ow // 4, oh // 4  # blur at low resolution: much faster, looks the same
    return (f"[0:v:0]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"boxblur=10:2,eq=brightness=-0.06,scale={ow}:{oh},setsar=1[bg];"
            f"[fgsrc]scale={ow}:{oh}:force_original_aspect_ratio=decrease:flags=lanczos,setsar=1[fg];"
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


def _encode(info, clip, fmt, orientation, out_path, encoder, progress, cancel):
    fmt = clip.fmt or fmt
    duration = clip.end - clip.start
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-y",
           "-progress", "pipe:1", "-nostats",
           "-ss", f"{clip.start:.3f}", "-i", info.path, "-t", f"{duration:.3f}",
           "-filter_complex", vertical_filter(fmt, info.fps_str, clip.crop_x, orientation), "-map", "[v]"]
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
            ok, last_err = _encode(info, clip, settings.vertical_format, settings.output_orientation, path,
                                  encoder, clip_progress, cancel)
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
