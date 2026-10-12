"""On-screen detection with OpenCV.

Only two small images per sampled frame are looked at:
  * a crop around the centre-top of the screen, where Deadlock shows the red
    "KILL" / "KILL ASSIST" badge above the crosshair (only for YOUR kills/assists)
  * a tiny thumbnail of the whole screen, used to spot the death screen
ffmpeg decodes, samples (4 frames per second) and crops; Python only sees small images.
"""
import logging
import subprocess

import cv2
import numpy as np

from .errors import Cancelled, FriendlyError
from .ffmpeg import ffmpeg_path, popen

log = logging.getLogger(__name__)

SAMPLE_FPS = 4
# Badge search area as fractions of the screen (x, y, width, height). The badge sits just
# above the crosshair; this box is deliberately generous until we calibrate on real footage.
BADGE_REGION = (0.35, 0.12, 0.30, 0.33)
BADGE_DIAMETER_1080P = 115  # rough badge size in pixels on a 1080p screen
CROP_W = 384
THUMB_W, THUMB_H = 160, 90


def _even(x):
    return max(2, int(round(x / 2)) * 2)


def _geometry(info):
    rx, ry, rw, rh = BADGE_REGION
    cw, ch = _even(info.width * rw), _even(info.height * rh)
    cx, cy = _even(info.width * rx), _even(info.height * ry)
    crop_h = _even(ch * CROP_W / cw)
    scale = CROP_W / cw
    return cw, ch, cx, cy, crop_h, scale


def _filter(info):
    cw, ch, cx, cy, crop_h, _ = _geometry(info)
    return (f"[0:v:0]fps={SAMPLE_FPS},split=2[a][b];"
            f"[a]crop={cw}:{ch}:{cx}:{cy},scale={CROP_W}:{crop_h}[c];"
            f"[b]scale={THUMB_W}:{THUMB_H},pad={CROP_W}:{THUMB_H}[d];"
            f"[c][d]vstack=2,format=bgr24[out]")


def detect_badge(crop, scale, height):
    """Score (0..1) how much the crop looks like the red kill badge, plus its centre."""
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    red = (((h <= 10) | (h >= 170)) & (s >= 110) & (v >= 80)).astype(np.uint8) * 255
    red = cv2.morphologyEx(red, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    d0 = BADGE_DIAMETER_1080P * (height / 1080.0) * scale
    min_area = np.pi / 4 * (0.45 * d0) ** 2
    max_area = np.pi / 4 * (1.8 * d0) ** 2
    light = (v >= 170) & (s <= 90)  # the white-ish text inside the badge

    best = (0.0, -1.0, -1.0)
    for c in contours:
        area = cv2.contourArea(c)
        if not (min_area <= area <= max_area):
            continue
        (x, y), r = cv2.minEnclosingCircle(c)
        circularity = area / (np.pi * r * r + 1e-6)
        bx, by, bw, bh = cv2.boundingRect(c)
        aspect = bw / max(1, bh)
        if circularity < 0.55 or not (0.65 <= aspect <= 1.5):
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


def scan_segments(info, segments, progress, cancel):
    """Sample frames inside each (start, end) segment. Returns arrays sorted by time."""
    cw, ch, cx, cy, crop_h, scale = _geometry(info)
    frame_h = crop_h + THUMB_H
    frame_bytes = CROP_W * frame_h * 3
    total = sum(e - s for s, e in segments) or 1.0
    done = 0.0
    times, scores, xs, ys, sats, vals = [], [], [], [], [], []
    use_hwaccel = True

    for seg_start, seg_end in segments:
        for attempt in (0, 1):
            cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error"]
            if use_hwaccel:
                cmd += ["-hwaccel", "auto"]
            cmd += ["-ss", f"{seg_start:.3f}", "-t", f"{seg_end - seg_start:.3f}", "-i", info.path,
                    "-an", "-filter_complex", _filter(info), "-map", "[out]",
                    "-f", "rawvideo", "-"]
            proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=frame_bytes * 8)
            count = 0
            try:
                while True:
                    if cancel.is_set():
                        raise Cancelled()
                    buf = proc.stdout.read(frame_bytes)
                    if len(buf) < frame_bytes:
                        break
                    img = np.frombuffer(buf, np.uint8).reshape(frame_h, CROP_W, 3)
                    t = seg_start + count / SAMPLE_FPS
                    score, bx, by = detect_badge(img[:crop_h], scale, info.height)
                    thumb = img[crop_h:, :THUMB_W]
                    hsv = cv2.cvtColor(thumb, cv2.COLOR_BGR2HSV)
                    times.append(t)
                    scores.append(score)
                    xs.append(cx + bx if bx >= 0 else -1)
                    ys.append(cy + by if by >= 0 else -1)
                    sats.append(float(hsv[..., 1].mean()))
                    vals.append(float(hsv[..., 2].mean()))
                    count += 1
                    if count % 40 == 0:
                        progress(min(1.0, (done + count / SAMPLE_FPS) / total), "Watching for your kills")
            finally:
                if proc.poll() is None:
                    proc.kill()
                err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
                proc.wait()
            if count == 0 and use_hwaccel and attempt == 0:
                log.warning("No frames with hardware decoding, retrying without it: %s", err[-500:])
                use_hwaccel = False
                continue
            if count == 0 and seg_end - seg_start > 1:
                log.error("No frames decoded for %.1f-%.1f: %s", seg_start, seg_end, err[-1500:])
                raise FriendlyError("Couldn't read the video frames. The file may be damaged.", err[-1500:])
            break
        done += seg_end - seg_start
        progress(min(1.0, done / total), "Watching for your kills")

    order = np.argsort(times, kind="stable")
    arr = lambda a: np.asarray(a, dtype=np.float32)[order]  # noqa: E731
    log.info("Vision scan: %d frames sampled, %d with a badge-like shape",
             len(times), int((arr(scores) >= 0.5).sum()) if times else 0)
    return {"t": arr(times), "badge": arr(scores), "bx": arr(xs), "by": arr(ys),
            "sat": arr(sats), "val": arr(vals)}
