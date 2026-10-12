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
SINGLE_FRAME_SCORE = 0.7  # one clear sighting is enough; weaker ones need two frames
POSITION_TOLERANCE = 80  # px (source video): how far sightings of one badge may be apart
MENU_MIN_SECONDS = 1.5  # the shop panel must be seen this long to count as the shop
MULTIKILL_GAP = 12.0  # takedowns this close together count as a multikill
MOMENT_GAP = 20.0  # kills this close together go into the same clip (if it fits the max length)
ACTION_QUIET_BEFORE = 3.0  # walking back from a kill, this much quiet means the fight hadn't started
ACTION_QUIET_AFTER = 2.0  # after the last kill, this much quiet means the fight is over
ACTION_MAX_AFTER = 10.0  # never run on more than this long after the last kill
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
    sightings: list = field(default_factory=list)  # every badge sighting and what happened to it
    clip_notes: list = field(default_factory=list)  # kills that did not become clips, and why


@dataclass
class Clip:
    start: float
    end: float
    score: float
    reasons: list = field(default_factory=list)
    takedowns: list = field(default_factory=list)
    focus: float = 0.0  # best moment, used for the thumbnail
    comp: dict = None  # this clip's own layout from the editor (None = use the settings)

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


def _menu_times(vision):
    """Times the buy menu is open: the pale shop panel seen for at least MENU_MIN_SECONDS.
    (A single pale frame - a bright wall, sky or flash - is not the shop.)"""
    t = vision["t"]
    if "panel" not in vision:
        return t[:0]
    pale = vision["panel"] >= MENU_PANEL
    keep = np.zeros(len(t), bool)
    for a, b in _runs(pale):
        if (b - a) / SAMPLE_FPS >= MENU_MIN_SECONDS:
            keep[a:b] = True
    return t[keep]


def find_takedowns(vision, report=None):
    """Turn per-frame badge scores into takedown times. Every badge sighting and what happened
    to it is added to `report` (a list of strings) when given."""
    t, score, bx, by = vision["t"], vision["badge"], vision["bx"], vision["by"]
    # Tooltips can hide part of the shop for a moment, so badges a few seconds either side of a
    # clearly open shop are ignored too.
    menu_times = _menu_times(vision)
    seen = score >= BADGE_THRESHOLD
    # Bridge flickers: the badge can be missed for a few frames (explosions, effects).
    for a, b in _runs(~seen):
        if 0 < a and b < len(seen) and b - a <= 3:
            seen[a:b] = True
    found = []

    def note(when, text):
        if report is not None:
            report.append(f"{fmt_time(when)}  {text}")

    for a, b in _runs(seen):
        seconds = (b - a) / SAMPLE_FPS
        hit = score[a:b] >= BADGE_THRESHOLD
        hits, best = int(hit.sum()), float(score[a:b].max())
        if hits < 2 and best < SINGLE_FRAME_SCORE:
            note(t[a], f"not counted: seen in only 1 frame (score {best:.2f})")
            continue
        if t[b - 1] - t[a] > seconds + 1:
            note(t[a], "not counted: spans a gap in the scan")
            continue
        if seconds > 8:
            note(t[a], f"not counted: stayed on screen {seconds:.0f}s (a real badge disappears within a few seconds)")
            continue
        # The badge stays in place: most sightings must be close to the middle one.
        xs, ys = bx[a:b][hit], by[a:b][hit]
        mx, my = np.median(xs), np.median(ys)
        near = (np.abs(xs - mx) <= POSITION_TOLERANCE) & (np.abs(ys - my) <= POSITION_TOLERANCE)
        if near.mean() < 0.6:
            note(t[a], "not counted: the red shape moved around (a real badge stays put)")
            continue
        lo, hi = np.searchsorted(menu_times, [t[a] - MENU_PAD, t[b - 1] + MENU_PAD])
        if hi > lo:
            note(t[a], "not counted: buy menu open around this time")
            continue
        note(t[a], f"COUNTED as a kill/assist ({hits} frames, best score {best:.2f})")
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
    sightings = []
    events = Events(find_takedowns(vision, sightings), find_deaths(vision), fights, spikes, threshold, sustained,
                    sightings)
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


def _moments(takedowns, max_span):
    """Group kills into clips: kills up to MOMENT_GAP apart share a clip while it fits."""
    groups = []
    for t in sorted(takedowns):
        if groups and t - groups[-1][-1] <= MOMENT_GAP and t - groups[-1][0] <= max_span:
            groups[-1].append(t)
        else:
            groups.append([t])
    return groups


def _action_bounds(events, first, last, settings, duration):
    """Stretch a clip over the whole fight around its kills, using the game audio.

    Walks back from the first kill while the audio shows action (stopping at a few seconds
    of quiet) and forward from the last kill until things calm down. Stays within max length.
    """
    s = settings
    curve = events.sustained
    # "Action" = at least halfway between this video's normal loudness and the loudness of the
    # fight around the kill. Relative to each fight, so it works for loud and for heavily
    # compressed audio (YouTube downloads) alike.
    i0, i1 = max(0, int((first - 3) / HOP)), min(len(curve), int((last + 3) / HOP) + 1)
    if i1 <= i0:
        return first - s.pre_roll, last + s.post_roll
    normal, peak = float(np.median(curve)), float(curve[i0:i1].max())
    if peak - normal < 0.3:  # the kill isn't audibly louder: nothing to go on
        return first - s.pre_roll, last + s.post_roll
    level = normal + 0.5 * (peak - normal)
    active = lambda t: 0 <= int(t / HOP) < len(curve) and curve[int(t / HOP)] > level  # noqa: E731
    budget = s.max_len - (last - first) - s.post_roll - 1.0
    start = first - s.pre_roll
    t, quiet = first, 0.0
    while first - t < budget and t > 0:
        t -= HOP
        quiet = 0.0 if active(t) else quiet + HOP
        if quiet >= ACTION_QUIET_BEFORE:
            break
        if quiet == 0.0:
            start = min(start, t - 2.0)  # a little lead-in before the action starts
    start = max(start, first - budget, 0.0)

    end = last + s.post_roll
    t, quiet = last, 0.0
    while t - last < ACTION_MAX_AFTER and t < duration:
        t += HOP
        quiet = 0.0 if active(t) else quiet + HOP
        if quiet >= ACTION_QUIET_AFTER:
            break
        if quiet == 0.0:
            end = max(end, t + 1.0)
    end = min(end, start + s.max_len, duration)
    return start, end


def _candidates(events, settings, duration):
    s = settings
    cands = []
    max_peak = max([f.peak for f in events.fights], default=1.0) or 1.0
    used_fights = set()
    max_span = max(0.0, s.max_len - s.pre_roll - s.post_roll)

    for group in _moments(events.takedowns, max_span):
        n = len(group)
        first, last = group[0], group[-1]
        best_multi = max(len(c) for c in _cluster(group, MULTIKILL_GAP))
        is_multi = best_multi >= 2
        if is_multi and not (s.want_multikills or s.want_kills):
            continue
        if not is_multi and not s.want_kills:
            continue
        score = 10.0 * n + MULTI_BONUS.get(best_multi, 50 + 20 * (best_multi - 4) if best_multi > 4 else 0)
        reasons = [MULTI_NAMES.get(best_multi, f"{best_multi} takedowns (multikill)") if is_multi
                   else "Your kill / assist"]
        if n > best_multi:
            reasons.append(f"{n} kills/assists in this clip")

        start, end = _action_bounds(events, first, last, s, duration)
        action = end - start
        fight = _overlapping_fight(events.fights, first - 3, last + 3)
        if fight is not None:
            used_fights.add(id(fight))
            if fight.length >= TEAMFIGHT_MIN_SECONDS and s.want_teamfights:
                score += 20 + 10 * fight.peak / max_peak
                reasons.append(f"Teamfight (loud for {fight.length:.0f}s)")
            else:
                score += 5 * fight.peak / max_peak

        aligned = sum(1 for t in group if any(abs(t - sp) <= 3 for sp in events.spikes))
        if aligned:
            score *= min(1.6, 1 + 0.15 * aligned)
            reasons.append("Kill lines up with an audio spike")
        reasons.append(f"Fight about {action:.0f}s" + (f" (padded to your {s.min_len}s minimum)"
                                                         if action < s.min_len else ""))
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
        clip.start -= extra * 0.65  # more build-up before the action than after it
        clip.end += extra * 0.35
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
    events.clip_notes = []
    note = lambda c, why: events.clip_notes.append(  # noqa: E731
        f"{fmt_time(c.takedowns[0] if c.takedowns else c.focus)}  {', '.join(c.reasons[:1])}: {why}")
    cands = [_fit_length(c, settings, duration) for c in _candidates(events, settings, duration)]
    if settings.exclude_deaths:
        kept = []
        for c in cands:
            handled = _handle_deaths(c, events.deaths, settings)
            if handled:
                kept.append(handled)
            else:
                note(c, "dropped - you die right at the start ('Skip moments where I die' is on)")
        cands = kept
    cands.sort(key=lambda c: c.score, reverse=True)
    chosen = []
    for c in cands:
        if settings.num_clips and len(chosen) >= settings.num_clips:
            note(c, f"not picked - you asked for {settings.num_clips} clips and higher-scoring ones filled them "
                    f"(score {c.score:.0f}; tick 'All highlights' to get every kill)")
            continue
        if all(c.end + 1 <= o.start or c.start >= o.end + 1 for o in chosen):
            chosen.append(c)
        else:
            note(c, "already inside another clip")
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
