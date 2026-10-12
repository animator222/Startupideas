"""Game-audio loudness scan. Fast: reads a small mono audio stream, never decodes video."""
import logging
import subprocess

import numpy as np

from .errors import Cancelled, FriendlyError
from .ffmpeg import ffmpeg_path, popen

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
HOP = 0.1  # one loudness value per 0.1 s
HOP_SAMPLES = int(SAMPLE_RATE * HOP)


def loudness_curve(info, progress, cancel):
    """Return RMS loudness in dB, one value per HOP seconds."""
    if not info.has_audio:
        log.warning("Video has no audio track; audio scoring disabled")
        return np.full(int(info.duration / HOP) + 1, -90.0, dtype=np.float32)

    # The high-pass filter removes low rumble/music so gunfire and abilities stand out more.
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-v", "error", "-i", info.path,
           "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
           "-af", "highpass=f=150", "-f", "s16le", "-"]
    proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    chunk_bytes = HOP_SAMPLES * 2 * 600  # one minute of audio per read
    total_hops = max(1, int(info.duration / HOP))
    values = []
    leftover = b""
    try:
        while True:
            if cancel.is_set():
                raise Cancelled()
            data = proc.stdout.read(chunk_bytes)
            if not data:
                break
            data = leftover + data
            usable = len(data) - len(data) % (HOP_SAMPLES * 2)
            leftover = data[usable:]
            samples = np.frombuffer(data[:usable], dtype=np.int16).astype(np.float32)
            frames = samples.reshape(-1, HOP_SAMPLES)
            rms = np.sqrt(np.mean(frames * frames, axis=1)) / 32768.0
            values.append(20 * np.log10(rms + 1e-6))
            progress(min(1.0, sum(len(v) for v in values) / total_hops),
                     "Listening to the game audio")
    finally:
        if proc.poll() is None:
            proc.kill()
        stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        proc.wait()

    if not values:
        raise FriendlyError("Couldn't read the audio from this video.", stderr[-2000:])
    curve = np.concatenate(values).astype(np.float32)
    log.info("Audio scan done: %d values, median %.1f dB, max %.1f dB",
             len(curve), float(np.median(curve)), float(curve.max()))
    return curve


def moving_average(x, n):
    if n <= 1 or len(x) == 0:
        return x.astype(np.float32)
    kernel = np.ones(n, dtype=np.float32) / n
    return np.convolve(x, kernel, mode="same").astype(np.float32)


def intensity(curve_db):
    """Loudness relative to the 'normal' level around that moment of the match.

    Farming and walking around happen all match long, so they become the baseline and
    fights stand out above it. Returns (short-term, sustained) intensity in dB above baseline.
    """
    n = len(curve_db)
    block = 100  # 10 s blocks
    blocks = max(1, n // block)
    medians = np.array([np.median(curve_db[i * block:(i + 1) * block]) for i in range(blocks)])
    # Rolling median over ~90 s of blocks gives a slowly changing "normal" level.
    k = 4
    smooth = np.array([np.median(medians[max(0, i - k):i + k + 1]) for i in range(blocks)])
    centers = (np.arange(blocks) + 0.5) * block
    baseline = np.interp(np.arange(n), centers, smooth)
    rel = curve_db - baseline
    return moving_average(rel, 5), moving_average(rel, 30)
