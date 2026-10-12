"""On-screen detection with OpenCV.

Only two small images per sampled frame are looked at:
  * a crop around the centre-top of the screen, where Deadlock shows the red
    "KILL" / "KILL ASSIST" badge above the crosshair (only for YOUR kills/assists)
  * a tiny thumbnail of the whole screen, used to spot the death screen
ffmpeg decodes, samples (4 frames per second) and crops; Python only sees small images.
"""
import logging
import os
import subprocess
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from .errors import Cancelled, FriendlyError
from .ffmpeg import ffmpeg_path, gpu_decode_args, popen

log = logging.getLogger(__name__)

SAMPLE_FPS = 4
# Default badge search areas as fractions of the frame (x, y, width, height). The user can
# change them on the "Kill badge area" screen.
# Horizontal 1920x1080 game capture: the badge sits just above the crosshair.
DEFAULT_REGION_LANDSCAPE = (0.35, 0.12, 0.30, 0.33)
# Vertical 1080x1920 layouts (e.g. OBS vertical canvas): measured from the user's Short, where
# the gameplay is in the middle band and the badge sits above-right of centre.
DEFAULT_REGION_PORTRAIT = (0.28, 0.25, 0.67, 0.25)
BADGE_DIAMETER_1080P = 115  # rough badge size in pixels when the frame's short side is 1080
CROP_W = 384
# The buy menu covers the badge area with a pale beige/grey panel full of item cards, and some
# item icons are red with white symbols - just like the kill badge. Frames where at least this
# much of the badge area is pale panel are treated as "menu open" (measured on the user's
# footage: open shop 0.18-0.42, real kills at most 0.16).
MENU_PANEL = 0.18
THUMB_W, THUMB_H = 160, 90


def _even(x):
    return max(2, int(round(x / 2)) * 2)


def default_region(info):
    return DEFAULT_REGION_PORTRAIT if info.height > info.width else DEFAULT_REGION_LANDSCAPE


def _geometry(info, region):
    rx, ry, rw, rh = region
    cw, ch = _even(info.width * rw), _even(info.height * rh)
    cx, cy = _even(info.width * rx), _even(info.height * ry)
    crop_h = _even(ch * CROP_W / cw)
    scale = CROP_W / cw
    return cw, ch, cx, cy, crop_h, scale


def _filter(info, region):
    cw, ch, cx, cy, crop_h, _ = _geometry(info, region)
    return (f"[0:v:0]fps={SAMPLE_FPS},split=2[a][b];"
            f"[a]crop={cw}:{ch}:{cx}:{cy},scale={CROP_W}:{crop_h}[c];"
            f"[b]scale={THUMB_W}:{THUMB_H},pad={CROP_W}:{THUMB_H}[d];"
            f"[c][d]vstack=2,format=bgr24[out]")


def panel_fraction(crop):
    """How much of the image is pale, washed-out UI panel (the buy menu)."""
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    s, v = hsv[..., 1], hsv[..., 2]
    return float(((s < 70) & (v > 120) & (v < 235)).mean())


def _redness(bgr):
    b, g, r = (bgr[..., i].astype(np.float32) for i in range(3))
    return np.clip(r - np.maximum(g, b), 0, 255) / 255.0


_TEMPLATES = [cv2.imread(str(Path(__file__).with_name("badge_templates") / f"{n}.png"), cv2.IMREAD_GRAYSCALE)
              .astype(np.float32) / 255.0 for n in ("assist", "kill")]
TEMPLATE_DIAM = 64  # badge diameter in the template images
TEMPLATE_SCALES = (0.6, 0.75, 0.9, 1.05, 1.2)  # badge size relative to the expected size
# Measured on 120 hand-labelled frames of the user's footage (49 real badges, 71 look-alikes:
# red-tinted screens, ENEMY SHRINE bars, hero icons, explosions): 0 false hits.
MATCH_MIN, RING_MIN, TEXT_RANGE = 0.65, 0.6, (0.05, 0.45)


def _template_badge(crop, d0):
    """The current (2026) badge: a red splash ring with KILL/ASSIST and the souls amount inside.
    Template matching on 'redness' finds the shape; the red ring and the text inside confirm it."""
    # Search at half resolution (4x faster); the badge is still ~30+ px there.
    small = cv2.resize(crop, (crop.shape[1] // 2, crop.shape[0] // 2), interpolation=cv2.INTER_AREA)
    red_map = _redness(small)
    best = (-1.0, 0, 0, 0.0)
    for k in TEMPLATE_SCALES:
        diam = d0 * k / 2
        sized = [cv2.resize(t, None, fx=diam / TEMPLATE_DIAM, fy=diam / TEMPLATE_DIAM, interpolation=cv2.INTER_AREA)
                 for t in _TEMPLATES]
        th, tw = sized[0].shape
        if th < 8 or th >= red_map.shape[0] or tw >= red_map.shape[1]:
            continue
        # Flat areas give meaningless scores: ignore places with almost no variation.
        mean = cv2.blur(red_map, (tw, th))
        std = np.sqrt(np.maximum(cv2.blur(red_map * red_map, (tw, th)) - mean * mean, 0))
        for tt in sized:
            th2, tw2 = tt.shape
            res = cv2.matchTemplate(red_map, tt, cv2.TM_CCOEFF_NORMED)
            sd = std[th2 // 2:th2 // 2 + res.shape[0], tw2 // 2:tw2 // 2 + res.shape[1]]
            res = res[:sd.shape[0], :sd.shape[1]]
            res = np.where(sd > 0.06, np.nan_to_num(res, nan=-1.0), -1.0)
            _, mx, _, loc = cv2.minMaxLoc(res)
            if mx > best[0]:
                best = (mx, (loc[0] + tw2 / 2) * 2, (loc[1] + th2 / 2) * 2, diam * 2)
    match, x, y, diam = best
    if match < 0.3:
        return (max(0.0, match) * 0.5, -1.0, -1.0)

    # Confirm: a red ring around the centre, and text (bright, not red) inside it.
    x0, x1 = int(max(0, x - diam)), int(min(crop.shape[1], x + diam))
    y0, y1 = int(max(0, y - diam)), int(min(crop.shape[0], y + diam))
    hsv = cv2.cvtColor(crop[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    red = ((H <= 10) | (H >= 170)) & (S >= 100) & (V >= 60)
    text = (V >= 150) & ~(((H <= 10) | (H >= 170)) & (S >= 90))
    yy, xx = np.mgrid[y0:y1, x0:x1]
    r = np.hypot(xx - x, yy - y)
    ring_red, inner_text = 0.0, 0.0
    for k in TEMPLATE_SCALES:  # the exact size is unknown: use the one with the clearest ring
        dd = d0 * k
        ring = (r > 0.32 * dd) & (r <= 0.5 * dd)
        inner = r <= 0.32 * dd
        if ring.any() and inner.any() and red[ring].mean() > ring_red:
            ring_red, inner_text = float(red[ring].mean()), float(text[inner].mean())
    ok = match >= MATCH_MIN and ring_red >= RING_MIN and TEXT_RANGE[0] <= inner_text <= TEXT_RANGE[1]
    score = 0.5 + 0.5 * min(1.0, (match - MATCH_MIN) / 0.3) if ok else min(0.45, match * 0.5)
    return (score, x, y)


def detect_badge(crop, scale, short_side):
    """Score (0..1, >= 0.5 counts) how much the crop shows the red kill/assist badge, plus its
    centre. Two checks: the current badge (template match), and the older plain red circle with
    white KILL / KILL ASSIST text (for older recordings)."""
    d0 = BADGE_DIAMETER_1080P * (short_side / 1080.0) * scale
    best = _template_badge(crop, d0)
    best = (best[0], best[1] / scale, best[2] / scale) if best[1] >= 0 else best

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    red = (((h <= 10) | (h >= 170)) & (s >= 110) & (v >= 80)).astype(np.uint8) * 255
    red = cv2.morphologyEx(red, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_area = np.pi / 4 * (0.45 * d0) ** 2
    max_area = np.pi / 4 * (1.8 * d0) ** 2
    light = (v >= 170) & (s <= 90)  # the white text inside the older badge
    for c in contours:
        area = cv2.contourArea(c)
        if not (min_area <= area <= max_area):
            continue
        (x, y), r = cv2.minEnclosingCircle(c)
        circularity = area / (np.pi * r * r + 1e-6)
        bx, by, bw, bh = cv2.boundingRect(c)
        if circularity < 0.55 or not (0.65 <= bw / max(1, bh) <= 1.5):
            continue
        mask = np.zeros(red.shape, np.uint8)
        cv2.drawContours(mask, [c], -1, 255, -1)
        inside = mask > 0
        text_frac = float(light[inside].mean()) if inside.any() else 0.0
        if text_frac < 0.01 or text_frac > 0.6:
            continue
        score = min(1.0, circularity) * min(1.0, text_frac / 0.04)
        if score > best[0]:
            best = (score, x / scale, y / scale)
    return best


CHUNK_SECONDS = 300  # the video is split into 5-minute pieces that are scanned in parallel
SAVE_SCORE = 0.25
FIRST_FRAME_TIMEOUT = 60  # seconds a decoder may take to produce its first frame  # frames at least this badge-like are saved for the detections picture


def _workers():
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def _scan_chunk(info, region, start, end, use_hwaccel, cancel, on_frame):
    """Decode one piece of the video at SAMPLE_FPS. Returns (rows, crops, error_text)."""
    cw, ch, cx, cy, crop_h, scale = _geometry(info, region)
    frame_h = crop_h + THUMB_H
    frame_bytes = CROP_W * frame_h * 3
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error"]
    if use_hwaccel:
        cmd += gpu_decode_args(info)
    # skip_frame noref: don't decode frames nothing else depends on - we only need 4 per second.
    cmd += ["-skip_frame", "noref", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", info.path,
            "-an", "-filter_complex", _filter(info, region), "-map", "[out]", "-f", "rawvideo", "-"]
    proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=frame_bytes * 8)
    rows, crops = [], []
    # Safety net: if a decoder stalls without producing anything, stop it so the caller can
    # retry without hardware decoding.
    watchdog = threading.Timer(FIRST_FRAME_TIMEOUT, lambda: not rows and proc.poll() is None and proc.kill())
    watchdog.daemon = True
    watchdog.start()
    try:
        while True:
            if cancel.is_set():
                raise Cancelled()
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            img = np.frombuffer(buf, np.uint8).reshape(frame_h, CROP_W, 3)
            t = start + len(rows) / SAMPLE_FPS
            score, bx, by = detect_badge(img[:crop_h], scale, min(info.width, info.height))
            panel = panel_fraction(img[:crop_h])
            hsv = cv2.cvtColor(img[crop_h:, :THUMB_W], cv2.COLOR_BGR2HSV)
            rows.append((t, score, cx + bx if bx >= 0 else -1, cy + by if by >= 0 else -1,
                         float(hsv[..., 1].mean()), float(hsv[..., 2].mean()), panel))
            if score >= SAVE_SCORE and panel < MENU_PANEL:
                small = img[:crop_h:2, ::2].copy()
                if crops and t - crops[-1][0] <= 2.5:
                    if score > crops[-1][1]:  # keep the clearest frame of each pop-up
                        crops[-1] = (crops[-1][0], score, small)
                elif len(crops) < 80:
                    crops.append((t, score, small))
            on_frame()
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        proc.wait()
    return rows, crops, err


def detections_sheet(crops, path, limit=120):
    """One picture showing everything that looked like a kill badge, for tuning."""
    if not crops:
        return None
    crops = sorted(crops, key=lambda c: c[0])
    if len(crops) > limit:  # keep the most badge-like ones
        crops = sorted(sorted(crops, key=lambda c: -c[1])[:limit], key=lambda c: c[0])
    th, tw = crops[0][2].shape[:2]
    cols = 6
    rows = (len(crops) + cols - 1) // cols
    sheet = np.full((rows * (th + 22), cols * tw, 3), 24, np.uint8)
    for i, (t, score, img) in enumerate(crops):
        r, c = divmod(i, cols)
        y, x = r * (th + 22), c * tw
        sheet[y:y + th, x:x + tw] = img[:th, :tw]
        m, s = divmod(int(t), 60)
        h, m = divmod(m, 60)
        stamp = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        color = (80, 220, 80) if score >= 0.5 else (60, 160, 240)
        cv2.putText(sheet, f"{stamp}  {score:.2f}", (x + 4, y + th + 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
    cv2.imwrite(str(path), sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return path


def scan_segments(info, segments, progress, cancel, region):
    """Sample frames inside each (start, end) segment. Returns arrays sorted by time."""
    chunks = []
    for s0, s1 in segments:
        t = s0
        while t < s1 - 0.01:
            chunks.append((t, min(s1, t + CHUNK_SECONDS)))
            t += CHUNK_SECONDS
    total_frames = max(1, int(sum(e - s for s, e in chunks) * SAMPLE_FPS))
    lock = threading.Lock()
    state = {"frames": 0, "hwaccel": True}

    def on_frame():
        with lock:
            state["frames"] += 1
            n = state["frames"]
        if n % 40 == 0:
            progress(min(1.0, n / total_frames), "Watching for your kills")

    def work(chunk):
        start, end = chunk
        rows, crops, err = _scan_chunk(info, region, start, end, state["hwaccel"], cancel, on_frame)
        if not rows and state["hwaccel"]:
            log.warning("No frames with hardware decoding at %.0fs, retrying without it: %s", start, err[-500:])
            state["hwaccel"] = False
            rows, crops, err = _scan_chunk(info, region, start, end, False, cancel, on_frame)
        if not rows and end - start > 1:
            log.error("No frames decoded for %.1f-%.1f: %s", start, end, err[-1500:])
            raise FriendlyError("Couldn't read the video frames. The file may be damaged.", err[-1500:])
        return rows, crops

    workers = _workers()
    log.info("Scanning %d pieces with %d parallel decoders", len(chunks), workers)
    rows, crops = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, c) for c in chunks]
        try:
            for f in futures:
                r, c = f.result()
                rows += r
                crops += c
        except BaseException:
            cancel.set()  # stop the other decoders too
            raise

    rows.sort(key=lambda r: r[0])
    arr = np.asarray(rows, dtype=np.float32).reshape(-1, 7)
    log.info("Vision scan: %d frames sampled, %d with a badge-like shape",
             len(arr), int((arr[:, 1] >= 0.5).sum()))
    return {"t": arr[:, 0], "badge": arr[:, 1], "bx": arr[:, 2], "by": arr[:, 3],
            "sat": arr[:, 4], "val": arr[:, 5], "panel": arr[:, 6]}, crops
