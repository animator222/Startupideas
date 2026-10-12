"""Runs the scans in order (audio first, then only the needed video parts) and caches results."""
import hashlib
import json
import logging
import os
import time

import numpy as np

from . import APP_VERSION, audio, vision
from .audio import HOP
from .highlights import _runs, fmt_time
import shutil

from .paths import ANALYSIS_REPORT, CACHE_DIR, LOG_DIR

log = logging.getLogger(__name__)

ANALYSIS_VERSION = 3  # v2: scan whole match by default; v3: buy-menu measurement added
SMART_SCAN_PERCENTILE = 60  # video is scanned wherever the audio is above this loudness
SCAN_PAD_BEFORE, SCAN_PAD_AFTER = 10.0, 12.0


def _cache_key(info, quick, region):
    st = os.stat(info.path)
    region = ",".join(f"{v:.3f}" for v in region)
    raw = f"{os.path.abspath(info.path)}|{st.st_size}|{int(st.st_mtime)}|{ANALYSIS_VERSION}|{quick}|{region}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _load_cache(key):
    path = CACHE_DIR / f"{key}.npz"
    if not path.exists():
        return None
    try:
        data = np.load(path)
        return {"audio_db": data["audio_db"],
                "vision": {k: data[f"v_{k}"] for k in ("t", "badge", "bx", "by", "sat", "val", "panel")},
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


def scan_plan(audio_db, duration, quick):
    """Which parts of the video to look at. Normally all of it. Quick scan only looks at the
    louder parts - faster, but it can miss kills in noisy-but-steady phases like laning."""
    if not quick:
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


DETECTIONS_IMAGE = LOG_DIR / "detections.jpg"


def analyze(info, quick, region, progress, cancel):
    """progress(fraction 0..1, message). region = kill badge search area (fractions x, y, w, h)."""
    keys = [_cache_key(info, False, region)] + ([_cache_key(info, True, region)] if quick else [])
    for key in keys:
        cached = _load_cache(key)
        if cached is not None:
            log.info("Using cached analysis %s", key)
            sheet = CACHE_DIR / f"{key}_detections.jpg"
            DETECTIONS_IMAGE.unlink(missing_ok=True)
            if sheet.exists():
                shutil.copyfile(sheet, DETECTIONS_IMAGE)
            progress(1.0, "Using saved analysis")
            return cached

    t0 = time.time()
    audio_db = audio.loudness_curve(info, lambda f, m: progress(0.25 * f, m), cancel)
    log.info("Audio pass took %.1fs", time.time() - t0)

    segments = scan_plan(audio_db, info.duration, quick)
    covered = sum(e - s for s, e in segments)
    log.info("Scanning %d parts of the video (%.0f of %.0f seconds)", len(segments), covered, info.duration)

    t1 = time.time()
    vis, crops = vision.scan_segments(info, segments, lambda f, m: progress(0.25 + 0.75 * f, m), cancel, region)
    log.info("Video pass took %.1fs", time.time() - t1)

    key = _cache_key(info, quick, region)
    result = {"audio_db": audio_db, "vision": vis, "segments": segments}
    _save_cache(key, result)
    DETECTIONS_IMAGE.unlink(missing_ok=True)
    try:
        if vision.detections_sheet(crops, CACHE_DIR / f"{key}_detections.jpg"):
            shutil.copyfile(CACHE_DIR / f"{key}_detections.jpg", DETECTIONS_IMAGE)
    except Exception:
        log.exception("Could not save detections picture")
    return result


def write_report(info, events, clips, settings, segments=None):
    """Human-readable summary for tuning - the user can send this file back to us."""
    lines = [
        f"Video: {info.path}",
        f"Length: {fmt_time(info.duration)}  {info.width}x{info.height} @ {info.fps:.2f} fps, audio={info.has_audio}",
        f"Settings: {json.dumps(settings.__dict__)}",
        f"App version: {APP_VERSION}",
        f"Scanned for kill badges: {fmt_time(sum(e - s for s, e in segments or []))} of {fmt_time(info.duration)}",
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
