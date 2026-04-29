#!/usr/bin/env python3
"""
StravaCoach stream analyzer.
Usage: python3 analyze_streams.py <streams.json> [--max-hr 190]
Output: structured analysis JSON to stdout

Input JSON format:
{
  "metadata": { "activity_id", "activity_name", "date", "elapsed_time_s", "distance_m", "total_elevation_gain" },
  "streams": { "time": [...], "distance": [...], "heartrate": [...], "altitude": [...], "velocity_smooth": [...], "grade_smooth": [...] }
}
"""

import json
import sys
import statistics
from typing import Optional

# ── Constants ────────────────────────────────────────────────────────────────

DEFAULT_MAX_HR = 190
WARMUP_SECONDS = 600  # first 10 minutes excluded from adjusted HR


# ── Helpers ──────────────────────────────────────────────────────────────────

def pace_str(pace_min_km: float) -> str:
    if pace_min_km is None or pace_min_km > 30:
        return "n/a"
    m = int(pace_min_km)
    s = int(round((pace_min_km - m) * 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}"

def vel_to_pace(v_ms: float) -> Optional[float]:
    """m/s → min/km. Returns None if not moving."""
    if v_ms < 0.3:
        return None
    return (1000.0 / v_ms) / 60.0

def hr_zone(hr: float, max_hr: int) -> int:
    pct = hr / max_hr
    if pct < 0.65: return 1
    if pct < 0.75: return 2
    if pct < 0.85: return 3
    if pct < 0.92: return 4
    return 5

def safe_mean(values):
    vals = [v for v in values if v is not None]
    return statistics.mean(vals) if vals else None

def safe_max(values):
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


# ── Session type detection ────────────────────────────────────────────────────

def find_hill_repeats(altitude, distance, min_gain=15, min_reps=2):
    """
    Detect hill repeat structure by finding repeated ascent-descent cycles.
    Returns list of (ascent_start_idx, peak_idx, descent_end_idx) tuples.
    """
    if not altitude or len(altitude) < 20:
        return []

    # Smooth altitude with a simple rolling mean (window=5)
    smoothed = []
    w = 5
    for i in range(len(altitude)):
        lo = max(0, i - w)
        hi = min(len(altitude), i + w + 1)
        smoothed.append(statistics.mean(altitude[lo:hi]))

    # Find local peaks and valleys using a larger window
    peaks = []
    valleys = []
    window = 20
    for i in range(window, len(smoothed) - window):
        segment = smoothed[i - window:i + window + 1]
        if smoothed[i] == max(segment) and smoothed[i] - min(segment) > min_gain:
            peaks.append(i)
        if smoothed[i] == min(segment):
            valleys.append(i)

    # Merge nearby peaks (within 30 indices)
    merged_peaks = []
    for p in peaks:
        if not merged_peaks or p - merged_peaks[-1] > 30:
            merged_peaks.append(p)
        else:
            # keep the higher one
            if smoothed[p] > smoothed[merged_peaks[-1]]:
                merged_peaks[-1] = p

    if len(merged_peaks) < min_reps:
        return []

    # For each peak, find the valley before and after
    repeats = []
    for peak_idx in merged_peaks:
        # valley before this peak
        prev_valleys = [v for v in valleys if v < peak_idx]
        next_valleys = [v for v in valleys if v > peak_idx]
        if not prev_valleys or not next_valleys:
            continue
        start_idx = prev_valleys[-1]
        end_idx = next_valleys[0]

        gain = smoothed[peak_idx] - smoothed[start_idx]
        if gain >= min_gain:
            repeats.append({
                "start_idx": start_idx,
                "peak_idx": peak_idx,
                "end_idx": end_idx,
                "elevation_gain_m": round(gain, 1),
            })

    return repeats


def find_effort_blocks(velocity, time, threshold_factor=1.15, min_duration_s=45, min_gap_s=20):
    """
    Find sustained effort blocks where velocity exceeds threshold_factor * mean for at least min_duration_s.
    Returns list of effort dicts with start/end indices.
    """
    moving = [(i, v) for i, v in enumerate(velocity) if v > 0.3]
    if not moving:
        return []

    mean_vel = statistics.mean(v for _, v in moving)
    threshold = mean_vel * threshold_factor

    in_effort = False
    effort_start = None
    efforts = []

    for i in range(len(velocity)):
        is_fast = velocity[i] >= threshold

        if not in_effort and is_fast:
            in_effort = True
            effort_start = i
        elif in_effort and not is_fast:
            # Check minimum duration
            t_start = time[effort_start]
            t_end = time[i]
            if (t_end - t_start) >= min_duration_s:
                efforts.append({"start_idx": effort_start, "end_idx": i})
            in_effort = False

    # Handle effort still running at end
    if in_effort and effort_start is not None:
        t_start = time[effort_start]
        t_end = time[-1]
        if (t_end - t_start) >= min_duration_s:
            efforts.append({"start_idx": effort_start, "end_idx": len(velocity) - 1})

    # Merge efforts separated by very short gaps
    merged = []
    for e in efforts:
        if merged and (time[e["start_idx"]] - time[merged[-1]["end_idx"]]) < min_gap_s:
            merged[-1]["end_idx"] = e["end_idx"]
        else:
            merged.append(dict(e))

    return merged


def detect_session_type(streams, warmup_end_idx, max_hr, total_time_s, total_dist_m):
    velocity = streams.get("velocity_smooth", [])
    altitude = streams.get("altitude", [])
    heartrate = streams.get("heartrate", [])
    time = streams.get("time", [])
    distance = streams.get("distance", [])

    # ── Hill repeat check ────────────────────────────────────────────────────
    total_gain = 0
    if altitude and len(altitude) > 1:
        for i in range(1, len(altitude)):
            diff = altitude[i] - altitude[i - 1]
            if diff > 0:
                total_gain += diff

    gain_per_km = (total_gain / total_dist_m * 1000) if total_dist_m > 0 else 0

    if gain_per_km > 20 and altitude:
        repeats = find_hill_repeats(altitude, distance)
        if len(repeats) >= 2:
            return "hill_repeats", repeats
        if gain_per_km > 30:
            return "trail_mountain", []

    # ── Pace variance check ──────────────────────────────────────────────────
    working_vel = velocity[warmup_end_idx:]
    valid_paces = [vel_to_pace(v) for v in working_vel if v > 0.3]

    if valid_paces and len(valid_paces) > 10:
        mean_pace = statistics.mean(valid_paces)
        stdev_pace = statistics.stdev(valid_paces)
        cv = stdev_pace / mean_pace if mean_pace > 0 else 0

        if cv > 0.16 and time and velocity:
            efforts = find_effort_blocks(velocity, time)
            if len(efforts) >= 2:
                durations = []
                for e in efforts:
                    t_start = time[e["start_idx"]]
                    t_end = time[e["end_idx"]]
                    durations.append(t_end - t_start)
                if len(durations) >= 3:
                    dur_cv = statistics.stdev(durations) / statistics.mean(durations)
                    if dur_cv < 0.45:
                        return "intervals", efforts
                return "fartlek", efforts

    # ── Steady-state classification ──────────────────────────────────────────
    working_hr = heartrate[warmup_end_idx:] if heartrate else []
    avg_hr = statistics.mean(working_hr) if working_hr else None

    if total_time_s > 4200:  # 70 min
        return "long_run", []

    if avg_hr:
        pct = avg_hr / max_hr
        if pct > 0.82:
            return "tempo", []
        elif pct < 0.70:
            return "recovery_run", []
        elif pct < 0.75:
            return "easy_run", []
        else:
            return "moderate_run", []

    return "easy_run", []


# ── Zone distribution ─────────────────────────────────────────────────────────

def zone_distribution(hr_list, time_list, max_hr):
    """Returns seconds spent in each HR zone (1-5)."""
    zones = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for i in range(1, len(hr_list)):
        dt = time_list[i] - time_list[i - 1]
        if dt <= 0 or dt > 60:
            continue
        z = hr_zone(hr_list[i], max_hr)
        zones[z] += dt
    return zones


# ── Per-segment analysis ──────────────────────────────────────────────────────

def analyze_segment(streams, start_idx, end_idx):
    """Extract key metrics for a segment defined by stream indices."""
    velocity = streams.get("velocity_smooth", [])
    heartrate = streams.get("heartrate", [])
    altitude = streams.get("altitude", [])
    time = streams.get("time", [])
    distance = streams.get("distance", [])
    grade = streams.get("grade_smooth", [])

    seg_vel = velocity[start_idx:end_idx + 1]
    seg_hr = heartrate[start_idx:end_idx + 1] if heartrate else []
    seg_alt = altitude[start_idx:end_idx + 1] if altitude else []
    seg_time = time[start_idx:end_idx + 1]
    seg_dist = distance[start_idx:end_idx + 1]
    seg_grade = grade[start_idx:end_idx + 1] if grade else []

    duration_s = (seg_time[-1] - seg_time[0]) if len(seg_time) >= 2 else 0
    dist_m = (seg_dist[-1] - seg_dist[0]) if len(seg_dist) >= 2 else 0

    moving_vel = [v for v in seg_vel if v > 0.3]
    avg_pace = vel_to_pace(statistics.mean(moving_vel)) if moving_vel else None
    max_pace = vel_to_pace(max(moving_vel)) if moving_vel else None

    avg_hr = statistics.mean(seg_hr) if seg_hr else None
    max_hr_seg = max(seg_hr) if seg_hr else None

    elev_gain = sum(max(0, seg_alt[i] - seg_alt[i - 1]) for i in range(1, len(seg_alt))) if seg_alt else 0
    avg_grade = statistics.mean(seg_grade) if seg_grade else None

    return {
        "duration_s": round(duration_s),
        "distance_m": round(dist_m),
        "avg_pace": pace_str(avg_pace),
        "best_pace": pace_str(max_pace),
        "avg_hr": round(avg_hr) if avg_hr else None,
        "max_hr": round(max_hr_seg) if max_hr_seg else None,
        "elevation_gain_m": round(elev_gain, 1),
        "avg_grade_pct": round(avg_grade, 1) if avg_grade is not None else None,
    }


# ── Grade-adjusted pace ───────────────────────────────────────────────────────

def grade_adjusted_pace(pace_min_km: float, grade_pct: float) -> float:
    """
    Simplified GAP calculation.
    Each 1% of grade = ~4% pace adjustment (based on common running physiology estimates).
    """
    if pace_min_km is None:
        return None
    factor = 1 + (grade_pct * 0.04)
    return pace_min_km / factor


# ── Main analysis ─────────────────────────────────────────────────────────────

def analyze(data, max_hr=DEFAULT_MAX_HR):
    streams = data.get("streams", {})
    meta = data.get("metadata", {})

    time = streams.get("time", [])
    distance = streams.get("distance", [])
    velocity = streams.get("velocity_smooth", [])
    heartrate = streams.get("heartrate", [])
    altitude = streams.get("altitude", [])
    grade = streams.get("grade_smooth", [])

    if not time or not velocity:
        return {"error": "Insufficient stream data"}

    total_time_s = time[-1] - time[0]
    total_dist_m = (distance[-1] - distance[0]) if distance else 0

    # Warmup cutoff index
    warmup_end_idx = next((i for i, t in enumerate(time) if (t - time[0]) >= WARMUP_SECONDS), len(time) - 1)

    # ── Session type detection ──────────────────────────────────────────────
    session_type, structure_data = detect_session_type(
        streams, warmup_end_idx, max_hr, total_time_s, total_dist_m
    )

    # ── Overall stats ───────────────────────────────────────────────────────
    moving_vel = [v for v in velocity if v > 0.3]
    avg_pace_overall = vel_to_pace(statistics.mean(moving_vel)) if moving_vel else None

    # Adjusted HR (post-warmup)
    hr_post_warmup = heartrate[warmup_end_idx:] if heartrate else []
    avg_hr_adjusted = round(statistics.mean(hr_post_warmup)) if hr_post_warmup else None
    max_hr_overall = max(heartrate) if heartrate else None

    # HR zones (post-warmup)
    time_post_warmup = time[warmup_end_idx:]
    zones = zone_distribution(hr_post_warmup, time_post_warmup, max_hr) if hr_post_warmup and time_post_warmup else {}
    total_zone_time = sum(zones.values()) or 1

    zone_summary = {}
    for z, secs in zones.items():
        pct = round(secs / total_zone_time * 100, 1)
        zone_summary[f"z{z}"] = {"seconds": round(secs), "percent": pct}

    # HR drift: compare first vs last third (post-warmup)
    hr_drift = None
    if hr_post_warmup and len(hr_post_warmup) > 9:
        third = len(hr_post_warmup) // 3
        first_third_hr = statistics.mean(hr_post_warmup[:third])
        last_third_hr = statistics.mean(hr_post_warmup[-third:])
        hr_drift = round(last_third_hr - first_third_hr, 1)

    # Pacing: first half vs second half
    pacing_split = None
    if distance and len(distance) > 4:
        mid_dist = (distance[-1] + distance[0]) / 2
        mid_idx = next((i for i, d in enumerate(distance) if d >= mid_dist), len(distance) // 2)
        first_half_vel = [v for v in velocity[:mid_idx] if v > 0.3]
        second_half_vel = [v for v in velocity[mid_idx:] if v > 0.3]
        if first_half_vel and second_half_vel:
            p1 = vel_to_pace(statistics.mean(first_half_vel))
            p2 = vel_to_pace(statistics.mean(second_half_vel))
            if p1:
                diff_pct = round((p2 - p1) / p1 * 100, 1) if p2 else None
                pacing_split = {
                    "first_half_pace": pace_str(p1),
                    "second_half_pace": pace_str(p2),
                    "diff_pct": diff_pct,
                    "type": "negative split" if diff_pct and diff_pct < -3 else ("positive split" if diff_pct and diff_pct > 3 else "even"),
                }

    # Total elevation gain
    total_elevation_gain = sum(max(0, altitude[i] - altitude[i - 1]) for i in range(1, len(altitude))) if altitude else 0

    # HR efficiency index (avg pace / avg HR — using adjusted values)
    hr_efficiency = round(avg_pace_overall / avg_hr_adjusted, 4) if avg_pace_overall and avg_hr_adjusted else None

    result = {
        "session_type": session_type,
        "overall": {
            "distance_km": round(total_dist_m / 1000, 2),
            "duration_s": round(total_time_s),
            "avg_pace": pace_str(avg_pace_overall),
            "avg_hr_adjusted": avg_hr_adjusted,
            "avg_hr_note": f"Excludes first {WARMUP_SECONDS // 60} min warmup",
            "max_hr": round(max_hr_overall) if max_hr_overall else None,
            "elevation_gain_m": round(total_elevation_gain, 1),
            "hr_efficiency_index": hr_efficiency,
        },
        "hr_zones": zone_summary,
        "hr_drift_bpm": hr_drift,
        "pacing_split": pacing_split,
        "warmup_end_seconds": time[warmup_end_idx] - time[0],
    }

    # ── Type-specific analysis ──────────────────────────────────────────────

    if session_type == "hill_repeats":
        repeats = structure_data  # list of {start_idx, peak_idx, end_idx, elevation_gain_m}
        ascents = []
        descents = []

        for rep_num, rep in enumerate(repeats, 1):
            ascent = analyze_segment(streams, rep["start_idx"], rep["peak_idx"])
            ascent["rep"] = rep_num
            ascent["elevation_gain_m"] = rep["elevation_gain_m"]
            ascents.append(ascent)

            if rep["end_idx"] > rep["peak_idx"]:
                descent = analyze_segment(streams, rep["peak_idx"], rep["end_idx"])
                descent["rep"] = rep_num
                descents.append(descent)

        # Progression: did ascent pace hold?
        ascent_paces = []
        for a in ascents:
            p = a.get("avg_pace")
            if p and p != "n/a":
                parts = p.split(":")
                ascent_paces.append(int(parts[0]) + int(parts[1]) / 60)

        progression = "n/a"
        if len(ascent_paces) >= 3:
            if ascent_paces[-1] > ascent_paces[0] * 1.05:
                progression = "fading (slower in later reps)"
            elif ascent_paces[-1] < ascent_paces[0] * 0.95:
                progression = "negative split (faster in later reps)"
            else:
                progression = "consistent"

        result["hill_repeats"] = {
            "total_reps": len(ascents),
            "ascents": ascents,
            "descents": descents,
            "progression": progression,
        }

    elif session_type in ("intervals", "fartlek"):
        efforts = structure_data
        effort_details = []
        recovery_details = []

        for i, effort in enumerate(efforts, 1):
            seg = analyze_segment(streams, effort["start_idx"], effort["end_idx"])
            seg["rep"] = i
            effort_details.append(seg)

            # Recovery between this effort and the next
            if i < len(efforts):
                next_effort = efforts[i]  # i is 1-based, efforts is 0-based so efforts[i] = next
                rec = analyze_segment(streams, effort["end_idx"], next_effort["start_idx"])
                rec["after_rep"] = i
                # Did HR recover to Z2?
                rec_hr = rec.get("avg_hr")
                rec["hr_recovered_to_z2"] = "yes" if rec_hr and rec_hr < max_hr * 0.75 else ("partial" if rec_hr and rec_hr < max_hr * 0.82 else "no")
                recovery_details.append(rec)

        # Effort pace progression
        effort_paces = []
        for e in effort_details:
            p = e.get("avg_pace")
            if p and p != "n/a":
                parts = p.split(":")
                effort_paces.append(int(parts[0]) + int(parts[1]) / 60)

        progression = "n/a"
        if len(effort_paces) >= 3:
            if effort_paces[-1] > effort_paces[0] * 1.05:
                progression = "fading (pace dropped in later efforts)"
            elif effort_paces[-1] < effort_paces[0] * 0.95:
                progression = "building (faster in later efforts)"
            else:
                progression = "consistent"

        avg_effort_hr = None
        all_effort_hrs = [e.get("avg_hr") for e in effort_details if e.get("avg_hr")]
        if all_effort_hrs:
            avg_effort_hr = round(statistics.mean(all_effort_hrs))

        avg_recovery_hr = None
        all_rec_hrs = [r.get("avg_hr") for r in recovery_details if r.get("avg_hr")]
        if all_rec_hrs:
            avg_recovery_hr = round(statistics.mean(all_rec_hrs))

        result["structured_work"] = {
            "type": "intervals" if session_type == "intervals" else "fartlek",
            "total_efforts": len(effort_details),
            "efforts": effort_details,
            "recoveries": recovery_details,
            "avg_effort_hr": avg_effort_hr,
            "avg_recovery_hr": avg_recovery_hr,
            "progression": progression,
        }

    elif session_type == "tempo":
        # Analyze the sustained tempo section (post warmup, pre cooldown)
        # Simple: use post-warmup data
        working_start = warmup_end_idx
        # Estimate cooldown start: last 10% of time where HR drops
        cooldown_start = int(len(time) * 0.9)
        tempo_seg = analyze_segment(streams, working_start, cooldown_start)

        # Pace drift within tempo: first vs last 20%
        tempo_len = cooldown_start - working_start
        first_20_pct = analyze_segment(streams, working_start, working_start + tempo_len // 5)
        last_20_pct = analyze_segment(streams, cooldown_start - tempo_len // 5, cooldown_start)

        result["tempo"] = {
            "main_effort": tempo_seg,
            "first_20pct": first_20_pct,
            "last_20pct": last_20_pct,
        }

    elif session_type == "long_run":
        # Break into thirds
        n = len(time)
        t1 = analyze_segment(streams, 0, n // 3)
        t2 = analyze_segment(streams, n // 3, 2 * n // 3)
        t3 = analyze_segment(streams, 2 * n // 3, n - 1)

        result["long_run"] = {
            "first_third": t1,
            "second_third": t2,
            "final_third": t3,
        }

    return result


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    max_hr = DEFAULT_MAX_HR

    args = sys.argv[1:]
    filepath = None
    for i, arg in enumerate(args):
        if arg == "--max-hr" and i + 1 < len(args):
            max_hr = int(args[i + 1])
        elif not arg.startswith("--"):
            filepath = arg

    if filepath:
        with open(filepath) as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)

    result = analyze(data, max_hr=max_hr)
    print(json.dumps(result, indent=2))
