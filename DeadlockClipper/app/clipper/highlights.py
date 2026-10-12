"""Turns the raw scans into events (kills, deaths, fights) and picks the best clips.

Everything here is fast, so changing settings in the review screen re-picks clips
instantly without scanning the video again.
"""
import logging
from dataclasses import dataclass, field

import numpy as np

from .audio import HOP, intensity
from .vision import MENU_PANEL, SAMPLE_FPS

log = logging.getLogger(__name__)

BADGE_THRESHOLD = 0.5
MULTIKILL_GAP = 12.0  # takedowns this close together form one moment
MENU_PAD = 4.0  # ignore badges this many seconds before/after the buy menu is seen open
TEAMFIGHT_MIN_SECONDS = 5.0


@dataclass
class Fight:
    start: float
    end: float
    peak: float  # dB above normal
    mean: float

    @property
    def length(self):
        return self.end - self.start


@dataclass
class Events:
    takedowns: list  # times (s) a kill / kill-assist badge appeared
    deaths: list  # times (s) the death screen started
    fights: list  # Fight objects (sustained loud stretches)
    spikes: list  # times of the sharpest loudness spikes
    threshold: float  # sustained-loudness threshold used for fights (dB above normal)
    sustained: np.ndarray  # for the loudness graph


@dataclass
class Clip:
    start: float
    end: float
    score: float
    reasons: list = field(default_factory=list)
    takedowns: list = field(default_factory=list)
    focus: float = 0.0  # best moment, used for the thumbnail
    fmt: str = ""  # per-clip vertical format chosen in the editor; "" = use the setting
    crop_x: float = 0.5  # center-crop position: 0 = left edge, 0.5 = middle, 1 = right edge

    @property
    def length(self):
        return self.end - self.start


def _runs(mask):
    """(start_index, end_index_exclusive) for each run of True values."""
    if len(mask) == 0:
        return []
    padded = np.concatenate([[False], mask, [False]])
    diff = np.diff(padded.astype(np.int8))
    return list(zip(np.where(diff == 1)[0], np.where(diff == -1)[0]))


def find_takedowns(vision):
    t, score, bx, by = vision["t"], vision["badge"], vision["bx"], vision["by"]
    # Times the buy menu is open. Tooltips can hide part of the menu panel for a moment,
    # so badges a few seconds either side of a clearly open menu are ignored too.
    menu_times = t[vision["panel"] >= MENU_PANEL] if "panel" in vision else t[:0]
    seen = score >= BADGE_THRESHOLD
    # Bridge flickers: the badge can be missed for a frame or two (explosions, overlap).
    for a, b in _runs(~seen):
        if 0 < a and b < len(seen) and b - a <= 2:
            seen[a:b] = True
    found = []
    for a, b in _runs(seen):
        seconds = (b - a) / SAMPLE_FPS
        if b - a < 2:
            continue  # a single frame is usually something else that's red and round
        if t[b - 1] - t[a] > seconds + 1:
            continue  # run spans a gap between scanned parts of the video
        if seconds > 8:
            log.debug("Ignored long red-circle run at %.1fs (%.1fs)", t[a], seconds)
            continue
        hit = score[a:b] >= BADGE_THRESHOLD
        if np.ptp(bx[a:b][hit]) > 60 or np.ptp(by[a:b][hit]) > 60:
            continue  # the badge stays in place; moving red things are not it
        lo, hi = np.searchsorted(menu_times, [t[a] - MENU_PAD, t[b - 1] + MENU_PAD])
        if hi > lo:
            log.debug("Ignored badge at %.1fs: buy menu open", t[a])
            continue
        found.append(float(t[a]))
    log.info("Buy menu seen open in %d sampled frames", len(menu_times))
    return found


def find_deaths(vision):
    """The death screen is much greyer than normal gameplay for several seconds."""
    t, sat = vision["t"], vision["sat"]
    if len(t) < 20:
        return []
    window = 30 * SAMPLE_FPS
    baseline = np.array([np.median(sat[max(0, i - window):i + 1]) for i in range(len(sat))])
    grey = sat < 0.45 * np.maximum(baseline, 1.0)
    deaths = []
    for a, b in _runs(grey):
        if (b - a) / SAMPLE_FPS >= 2.5 and t[b - 1] - t[a] < (b - a) / SAMPLE_FPS + 1:
            deaths.append(float(t[a]))
    return deaths


def sensitivity_percentile(sensitivity):
    # 1 -> only the loudest 3% of the match counts as a fight, 10 -> the loudest 25%.
    return 97.0 - (sensitivity - 1) * (22.0 / 9.0)


def find_events(analysis, sensitivity):
    short, sustained = intensity(analysis["audio_db"])
    threshold = float(np.percentile(sustained, sensitivity_percentile(sensitivity))) if len(sustained) else 0
    threshold = max(threshold, 2.0)  # never call a stretch a fight unless clearly above normal

    fights = []
    for a, b in _runs(sustained > threshold):
        start, end = a * HOP, b * HOP
        if fights and start - fights[-1].end < 2.0:
            prev = fights.pop()
            start = prev.start
            a = int(start / HOP)
        if end - start >= 2.0:
            fights.append(Fight(start, end, float(sustained[a:b].max()), float(sustained[a:b].mean())))

    spike_level = np.percentile(short, 98.5) if len(short) else 0
    spikes = [i * HOP for i in range(1, len(short) - 1)
              if short[i] >= spike_level and short[i] >= short[i - 1] and short[i] >= short[i + 1]]

    vision = analysis["vision"]
    events = Events(find_takedowns(vision), find_deaths(vision), fights, spikes, threshold, sustained)
    log.info("Events: %d takedowns, %d deaths, %d loud fights (threshold %.1f dB), %d spikes",
             len(events.takedowns), len(events.deaths), len(fights), threshold, len(spikes))
    return events


def _cluster(times, gap):
    groups = []
    for t in sorted(times):
        if groups and t - groups[-1][-1] <= gap:
            groups[-1].append(t)
        else:
            groups.append([t])
    return groups


def _overlapping_fight(fights, start, end):
    best = None
    for f in fights:
        if f.end >= start and f.start <= end and (best is None or f.peak > best.peak):
            best = f
    return best


MULTI_NAMES = {2: "Double kill", 3: "Triple kill", 4: "Quad kill"}
MULTI_BONUS = {2: 15, 3: 30, 4: 50}


def _candidates(events, settings):
    s = settings
    cands = []
    max_peak = max([f.peak for f in events.fights], default=1.0) or 1.0
    used_fights = set()

    for group in _cluster(events.takedowns, MULTIKILL_GAP):
        n = len(group)
        first, last = group[0], group[-1]
        reasons = []
        is_multi = n >= 2
        if is_multi and not (s.want_multikills or s.want_kills):
            continue
        if not is_multi and not s.want_kills:
            continue
        score = 10.0 * n + MULTI_BONUS.get(n, 50 + 20 * (n - 4) if n > 4 else 0)
        reasons.append(MULTI_NAMES.get(n, f"{n} takedowns (multikill)") if is_multi else "Your kill / assist")

        start, end = first - s.pre_roll, last + s.post_roll
        fight = _overlapping_fight(events.fights, first - 3, last + 3)
        if fight is not None:
            used_fights.add(id(fight))
            if fight.length >= TEAMFIGHT_MIN_SECONDS and s.want_teamfights:
                score += 20 + 10 * fight.peak / max_peak
                reasons.append(f"Teamfight (loud for {fight.length:.0f}s)")
                start = min(start, max(fight.start - 2, first - 12))
                end = max(end, min(fight.end + 1, last + 8))
            else:
                score += 5 * fight.peak / max_peak

        aligned = sum(1 for t in group if any(abs(t - sp) <= 3 for sp in events.spikes))
        if aligned:
            score *= min(1.6, 1 + 0.15 * aligned)
            reasons.append("Kill lines up with an audio spike")
        cands.append(Clip(start, end, score, reasons, list(group), focus=first))

    if s.want_loud_only:
        for f in events.fights:
            if id(f) in used_fights or f.length < TEAMFIGHT_MIN_SECONDS:
                continue
            score = min(25.0, 6 + 12 * f.peak / max_peak + 0.5 * f.length)
            cands.append(Clip(f.start - 2, f.end + 2, score,
                              [f"Loud fight for {f.length:.0f}s (no kill badge seen)"],
                              [], focus=(f.start + f.end) / 2))
    return cands


def _fit_length(clip, settings, duration):
    s = settings
    if clip.length > s.max_len:
        # Keep the window of max length that contains the most takedowns.
        best_start, best_count = clip.start, -1
        for t in clip.takedowns or [clip.focus]:
            start = max(clip.start, t - s.pre_roll)
            count = sum(1 for x in clip.takedowns if start <= x <= start + s.max_len - s.post_roll)
            if count > best_count:
                best_start, best_count = start, count
        clip.start, clip.end = best_start, best_start + s.max_len
    if clip.length < s.min_len:
        extra = s.min_len - clip.length
        clip.start -= extra * 0.6
        clip.end += extra * 0.4
    if clip.start < 0:
        clip.end, clip.start = clip.end - clip.start, 0.0
    if clip.end > duration:
        clip.start, clip.end = max(0.0, clip.start - (clip.end - duration)), duration
    return clip


def _handle_deaths(clip, deaths, settings):
    inside = [d for d in deaths if clip.start < d < clip.end]
    if not inside:
        return clip
    d = inside[0]
    action_before = [t for t in clip.takedowns if t < d] or ([clip.focus] if clip.focus < d else [])
    if not action_before:
        return None  # you die before the highlight even starts
    new_end = d - 0.5
    clip.start = min(clip.start, new_end - settings.min_len)
    earlier = [x for x in deaths if x < clip.start + 1]
    clip.start = max(clip.start, (earlier[-1] + 1) if earlier else 0.0, 0.0)
    clip.end = new_end
    if clip.length < 8:
        return None
    clip.takedowns = [t for t in clip.takedowns if t < new_end]
    clip.reasons.append("Ends just before your death")
    return clip


def pick_clips(events, settings, duration):
    cands = [_fit_length(c, settings, duration) for c in _candidates(events, settings)]
    if settings.exclude_deaths:
        cands = [c for c in (_handle_deaths(c, events.deaths, settings) for c in cands) if c]
    cands.sort(key=lambda c: c.score, reverse=True)
    chosen = []
    for c in cands:
        if all(c.end + 1 <= o.start or c.start >= o.end + 1 for o in chosen):
            chosen.append(c)
        if settings.num_clips and len(chosen) >= settings.num_clips:
            break
    chosen.sort(key=lambda c: c.start)
    for c in chosen:
        c.score = round(c.score, 1)
    log.info("Picked %d clips from %d candidates", len(chosen), len(cands))
    return chosen


def fmt_time(seconds):
    seconds = max(0.0, seconds)
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
