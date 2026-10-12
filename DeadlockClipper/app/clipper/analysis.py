"""Runs the scans in order (audio first, then only the needed video parts) and caches results."""
import hashlib
import json
import logging
import os
import time

import numpy as np

from . import audio, vision
from .audio import HOP
from .highlights import _runs, fmt_time
from .paths import ANALYSIS_REPORT, CACHE_DIR

log = logging.getLogger(__name__)

ANALYSIS_VERSION = 1
SMART_SCAN_PERCENTILE = 60  # video is scanned wherever the audio is above this loudness
SCAN_PAD_BEFORE, SCAN_PAD_AFTER = 10.0, 12.0


def _cache_key(info, full_scan):
    st = os.stat(info.path)
    raw = f"{os.path.abspath(info.path)}|{st.st_size}|{int(st.st_mtime)}|{ANALYSIS_VERSION}|{full_scan}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _load_cache(key):
    path = CACHE_DIR / f"{key}.npz"
    if not path.exists():
        return None
    try:
        data = np.load(path)
        return {"audio_db": data["audio_db"],
                "vision": {k: data[f"v_{k}"] for k in ("t", "badge", "bx", "by", "sat", "val")},
                "segments": data["segments"].tolist()}
    except Exception:
        log.exception("Ignoring unreadable cache file %s", path)
        return None


def _save_cache(key, result):
    try:
        np.savez_compressed(CACHE_DIR / f"{key}.npz", audio_db=result["audio_db"],
                            segments=np.asarray(result["segments"], dtype=np.float32).reshape(-1, 2),
                            **{f"v_{k}": v for k, v in result["vision"].items()})
    except Exception:
        log.exception("Could not write cache")


def scan_plan(audio_db, duration, full_scan):
    """Which parts of the video to look at. Kills nearly always come with gunfire,
    so by default only the louder parts (plus padding) are decoded."""
    if full_scan:
        return [(0.0, duration)]
    _, sustained = audio.intensity(audio_db)
    level = np.percentile(sustained, SMART_SCAN_PERCENTILE)
    segments = []
    for a, b in _runs(sustained > level):
        start, end = max(0.0, a * HOP - SCAN_PAD_BEFORE), min(duration, b * HOP + SCAN_PAD_AFTER)
        if segments and start <= segments[-1][1] + 10:
            segments[-1] = (segments[-1][0], max(segments[-1][1], end))
        else:
            segments.append((start, end))
    covered = sum(e - s for s, e in segments)
    if covered > 0.7 * duration or not segments:
        return [(0.0, duration)]
    return segments


def analyze(info, full_scan, progress, cancel):
    """progress(fraction 0..1, message)."""
    for key in (_cache_key(info, True), _cache_key(info, full_scan)):
        cached = _load_cache(key)
        if cached is not None:
            log.info("Using cached analysis %s", key)
            progress(1.0, "Using saved analysis")
            return cached

    t0 = time.time()
    audio_db = audio.loudness_curve(info, lambda f, m: progress(0.25 * f, m), cancel)
    log.info("Audio pass took %.1fs", time.time() - t0)

    segments = scan_plan(audio_db, info.duration, full_scan)
    covered = sum(e - s for s, e in segments)
    log.info("Scanning %d parts of the video (%.0f of %.0f seconds)", len(segments), covered, info.duration)

    t1 = time.time()
    vis = vision.scan_segments(info, segments, lambda f, m: progress(0.25 + 0.75 * f, m), cancel)
    log.info("Video pass took %.1fs", time.time() - t1)

    result = {"audio_db": audio_db, "vision": vis, "segments": segments}
    _save_cache(_cache_key(info, full_scan), result)
    return result


def write_report(info, events, clips, settings):
    """Human-readable summary for tuning - the user can send this file back to us."""
    lines = [
        f"Video: {info.path}",
        f"Length: {fmt_time(info.duration)}  {info.width}x{info.height} @ {info.fps:.2f} fps, audio={info.has_audio}",
        f"Settings: {json.dumps(settings.__dict__)}",
        "",
        f"Kill/assist badges found ({len(events.takedowns)}): "
        + ", ".join(fmt_time(t) for t in events.takedowns),
        f"Deaths found ({len(events.deaths)}): " + ", ".join(fmt_time(t) for t in events.deaths),
        f"Loud fights ({len(events.fights)}, threshold {events.threshold:.1f} dB above normal):",
    ]
    for f in sorted(events.fights, key=lambda f: -f.peak)[:40]:
        lines.append(f"   {fmt_time(f.start)}-{fmt_time(f.end)}  peak {f.peak:.1f} dB  ({f.length:.0f}s)")
    lines += ["", f"Chosen clips ({len(clips)}):"]
    for i, c in enumerate(clips, 1):
        lines.append(f"  {i:02d}. {fmt_time(c.start)}-{fmt_time(c.end)}  score {c.score}  "
                     + "; ".join(c.reasons))
    try:
        ANALYSIS_REPORT.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        log.exception("Could not write analysis report")
