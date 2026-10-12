"""Finding and running ffmpeg / ffprobe / ffplay."""
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

from .errors import FriendlyError
from .paths import TOOLS_DIR

log = logging.getLogger(__name__)

# Stops a black console window flashing up every time ffmpeg runs on Windows.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
EXE = ".exe" if os.name == "nt" else ""


def find_tool(name):
    for folder in (TOOLS_DIR / "ffmpeg", TOOLS_DIR / "ffmpeg" / "bin"):
        candidate = folder / f"{name}{EXE}"
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def ffmpeg_path():
    path = find_tool("ffmpeg")
    if not path:
        raise FriendlyError(
            "ffmpeg is missing. Please run Install.bat again to repair the app.",
            f"ffmpeg not found in {TOOLS_DIR / 'ffmpeg'} or on PATH",
        )
    return path


def ffprobe_path():
    path = find_tool("ffprobe")
    if not path:
        raise FriendlyError("ffprobe is missing. Please run Install.bat again to repair the app.")
    return path


def run(cmd, check=True, timeout=None):
    log.debug("run: %s", subprocess.list2cmdline(cmd))
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=NO_WINDOW, timeout=timeout,
    )
    if check and result.returncode != 0:
        log.error("command failed (%s): %s", result.returncode, result.stderr[-3000:])
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def popen(cmd, **kwargs):
    log.debug("popen: %s", subprocess.list2cmdline(cmd))
    return subprocess.Popen(cmd, creationflags=NO_WINDOW, **kwargs)


@dataclass
class VideoInfo:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    fps_str: str  # exact rate for ffmpeg, e.g. "60" or "30000/1001"
    has_audio: bool
    vcodec: str


def _parse_rate(text):
    try:
        rate = Fraction(text)
        return rate if rate > 0 else None
    except (ValueError, ZeroDivisionError, TypeError):
        return None


def probe(path):
    path = str(path)
    if not Path(path).is_file():
        raise FriendlyError(f"Can't find the video file:\n{path}")
    try:
        result = run([ffprobe_path(), "-v", "error", "-print_format", "json",
                      "-show_streams", "-show_format", path], timeout=60)
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        raise FriendlyError(
            "This file couldn't be opened as a video. It may be damaged or still being recorded.",
            getattr(e, "stderr", "") or str(e),
        ) from e

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        raise FriendlyError("This file doesn't contain any video.")
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))

    rate = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate")) or Fraction(30)
    if rate > 60:
        log.info("Source is %.2f fps; exports are capped at 60 fps for Shorts/Reels", float(rate))
        rate = Fraction(60)
    rate = rate.limit_denominator(1001)
    fps_str = str(rate.numerator) if rate.denominator == 1 else f"{rate.numerator}/{rate.denominator}"

    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise FriendlyError("Couldn't read the length of this video. If it's an OBS recording that "
                            "was interrupted, try remuxing it in OBS (File > Remux Recordings).")

    info = VideoInfo(path=path, duration=duration, width=int(video["width"]), height=int(video["height"]),
                     fps=float(rate), fps_str=fps_str, has_audio=has_audio,
                     vcodec=video.get("codec_name", "?"))
    log.info("Video: %s", info)
    return info


def extract_frame(path, t, out_png, filter_complex=None, width=None):
    """Save one frame at time t (seconds). filter_complex must end in [v] if given."""
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-y",
           "-ss", f"{max(0.0, t):.3f}", "-i", str(path)]
    if filter_complex:
        cmd += ["-filter_complex", filter_complex, "-map", "[v]"]
    elif width:
        cmd += ["-vf", f"scale={width}:-2"]
    cmd += ["-frames:v", "1", str(out_png)]
    run(cmd, timeout=60)
    return out_png


ENCODER_ARGS = {
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr",
                   "-cq", "19", "-b:v", "16M", "-maxrate", "24M", "-bufsize", "32M", "-profile:v", "high"],
    "h264_qsv": ["-c:v", "h264_qsv", "-global_quality", "21", "-profile:v", "high"],
    "h264_amf": ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "19",
                 "-qp_p", "21", "-profile:v", "high"],
    "libx264": ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high"],
}


@lru_cache(maxsize=1)
def working_encoders():
    """Try a tiny test encode with each H.264 encoder; GPU ones first, CPU last."""
    found = []
    for name in ("h264_nvenc", "h264_qsv", "h264_amf", "libx264"):
        cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi",
               "-i", "color=c=black:s=1080x1920:r=30", "-frames:v", "5",
               *ENCODER_ARGS[name], "-pix_fmt", "yuv420p", "-f", "null", "-"]
        try:
            ok = run(cmd, check=False, timeout=30).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            ok = False
        log.info("Encoder %s: %s", name, "available" if ok else "not available")
        if ok:
            found.append(name)
    if not found:
        raise FriendlyError("No working video encoder was found. Please run Install.bat again.")
    return found


def ffmpeg_version():
    try:
        return run([ffmpeg_path(), "-version"], timeout=15).stdout.splitlines()[0]
    except Exception as e:  # only used for the log
        return f"unknown ({e})"
