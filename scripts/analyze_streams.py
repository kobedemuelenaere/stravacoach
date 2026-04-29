#!/usr/bin/env python3
"""
StravaCoach stream analyzer.
Usage:
    python3 analyze_streams.py <streams.json> [--max-hr 190]
    python3 analyze_streams.py --activity-id 123456 [--max-hr 190] [--env-file .env] [--save-input streams.json]
Output: structured analysis JSON to stdout

Input JSON format:
{
  "metadata": { "activity_id", "activity_name", "date", "elapsed_time_s", "distance_m", "total_elevation_gain" },
  "streams": { "time": [...], "distance": [...], "heartrate": [...], "altitude": [...], "velocity_smooth": [...], "grade_smooth": [...] }
}
"""

import json
import os
import statistics
import sys
from pathlib import Path
from typing import Dict, Optional
from urllib import error, parse, request

# ── Constants ────────────────────────────────────────────────────────────────

DEFAULT_MAX_HR = 190
WARMUP_MAX_SECONDS = 300  # at most first 5 minutes can be excluded from adjusted HR

STRAVA_API_BASE = "https://www.strava.com/api/v3"
STRAVA_CONFIG_PATH = Path.home() / ".config" / "strava-mcp" / "config.json"
DEFAULT_ENV_FILE = Path.cwd() / ".env"


# ── Strava input helpers ─────────────────────────────────────────────────────

def parse_args(argv):
    args = {
        "filepath": None,
        "activity_id": None,
        "max_hr": DEFAULT_MAX_HR,
        "env_file": str(DEFAULT_ENV_FILE),
        "save_input": None,
    }

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--max-hr" and i + 1 < len(argv):
            args["max_hr"] = int(argv[i + 1])
            i += 2
            continue
        if arg == "--activity-id" and i + 1 < len(argv):
            args["activity_id"] = int(argv[i + 1])
            i += 2
            continue
        if arg == "--env-file" and i + 1 < len(argv):
            args["env_file"] = argv[i + 1]
            i += 2
            continue
        if arg == "--save-input" and i + 1 < len(argv):
            args["save_input"] = argv[i + 1]
            i += 2
            continue

        if arg == "--help":
            print(
                "Usage:\n"
                "  python3 analyze_streams.py <streams.json> [--max-hr 190]\n"
                "  python3 analyze_streams.py --activity-id 123456 [--max-hr 190] [--env-file .env] [--save-input streams.json]\n"
                "\n"
                "Input priority when --activity-id is used:\n"
                "  1) environment variables\n"
                "  2) ~/.config/strava-mcp/config.json\n"
                "  3) local .env file (or --env-file path)\n"
            )
            sys.exit(0)

        if not arg.startswith("--") and args["filepath"] is None:
            args["filepath"] = arg
            i += 1
            continue

        raise ValueError(f"Unknown argument: {arg}")

    return args


def read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_strava_mcp_config(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}

    try:
        cfg = json.loads(path.read_text())
    except Exception:
        return {}

    return {
        "STRAVA_ACCESS_TOKEN": cfg.get("accessToken"),
        "STRAVA_REFRESH_TOKEN": cfg.get("refreshToken"),
        "STRAVA_CLIENT_ID": str(cfg.get("clientId")) if cfg.get("clientId") is not None else None,
        "STRAVA_CLIENT_SECRET": cfg.get("clientSecret"),
    }


def merge_auth(env_file: Path) -> Dict[str, str]:
    merged: Dict[str, str] = {}

    dot_env = read_env_file(env_file)
    for key, value in dot_env.items():
        if value:
            merged[key] = value

    cfg = read_strava_mcp_config(STRAVA_CONFIG_PATH)
    for key, value in cfg.items():
        if value:
            merged[key] = value

    for key in ("STRAVA_ACCESS_TOKEN", "STRAVA_REFRESH_TOKEN", "STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET"):
        value = os.environ.get(key)
        if value:
            merged[key] = value

    return merged


def write_env_values(env_path: Path, updates: Dict[str, str]) -> None:
    existing = {}
    if env_path.exists():
        existing = read_env_file(env_path)
    existing.update({k: v for k, v in updates.items() if v})
    lines = [f"{k}={v}" for k, v in existing.items()]
    env_path.write_text("\n".join(lines) + "\n")


def write_mcp_tokens(access_token: str, refresh_token: str) -> None:
    if not STRAVA_CONFIG_PATH.exists():
        return
    try:
        cfg = json.loads(STRAVA_CONFIG_PATH.read_text())
    except Exception:
        return
    cfg["accessToken"] = access_token
    cfg["refreshToken"] = refresh_token
    STRAVA_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    STRAVA_CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def http_get(url: str, token: str):
    req = request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(url: str, payload: Dict[str, str]):
    data = parse.urlencode(payload).encode("utf-8")
    req = request.Request(url, data=data, method="POST")
    with request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def refresh_access_token(auth: Dict[str, str], env_file: Path) -> Dict[str, str]:
    refresh_token = auth.get("STRAVA_REFRESH_TOKEN")
    client_id = auth.get("STRAVA_CLIENT_ID")
    client_secret = auth.get("STRAVA_CLIENT_SECRET")

    if not refresh_token or not client_id or not client_secret:
        raise RuntimeError(
            "Cannot refresh token. Missing STRAVA_REFRESH_TOKEN, STRAVA_CLIENT_ID, or STRAVA_CLIENT_SECRET."
        )

    token_data = http_post_json(
        "https://www.strava.com/oauth/token",
        {
            "client_id": str(client_id),
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )

    new_access = token_data.get("access_token")
    new_refresh = token_data.get("refresh_token")
    if not new_access or not new_refresh:
        raise RuntimeError("Strava refresh succeeded but response was missing token fields.")

    auth["STRAVA_ACCESS_TOKEN"] = new_access
    auth["STRAVA_REFRESH_TOKEN"] = new_refresh

    os.environ["STRAVA_ACCESS_TOKEN"] = new_access
    os.environ["STRAVA_REFRESH_TOKEN"] = new_refresh

    write_env_values(env_file, {
        "STRAVA_ACCESS_TOKEN": new_access,
        "STRAVA_REFRESH_TOKEN": new_refresh,
    })
    write_mcp_tokens(new_access, new_refresh)
    return auth


def strava_get_with_refresh(path: str, auth: Dict[str, str], env_file: Path, params: Optional[Dict[str, str]] = None):
    token = auth.get("STRAVA_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("Missing STRAVA_ACCESS_TOKEN. Set env/.env or connect via strava-mcp first.")

    query = f"?{parse.urlencode(params)}" if params else ""
    url = f"{STRAVA_API_BASE}{path}{query}"

    try:
        return http_get(url, token)
    except error.HTTPError as exc:
        if exc.code != 401:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Strava API error {exc.code} on {path}: {body}")
        refresh_access_token(auth, env_file)
        return http_get(url, auth["STRAVA_ACCESS_TOKEN"])


def fetch_activity_input(activity_id: int, env_file: Path) -> Dict:
    auth = merge_auth(env_file)

    details = strava_get_with_refresh(f"/activities/{activity_id}", auth, env_file)
    streams_raw = strava_get_with_refresh(
        f"/activities/{activity_id}/streams/time,distance,heartrate,altitude,velocity_smooth,grade_smooth",
        auth,
        env_file,
        params={"key_by_type": "true"},
    )

    def stream_data(name: str):
        entry = streams_raw.get(name)
        return entry.get("data", []) if isinstance(entry, dict) else []

    return {
        "metadata": {
            "activity_id": details.get("id"),
            "activity_name": details.get("name"),
            "description": details.get("description") or "",
            "private_note": details.get("private_note") or "",
            "date": details.get("start_date_local", "")[:10],
            "elapsed_time_s": details.get("elapsed_time"),
            "distance_m": details.get("distance"),
            "total_elevation_gain": details.get("total_elevation_gain"),
        },
        "streams": {
            "time": stream_data("time"),
            "distance": stream_data("distance"),
            "heartrate": stream_data("heartrate"),
            "altitude": stream_data("altitude"),
            "velocity_smooth": stream_data("velocity_smooth"),
            "grade_smooth": stream_data("grade_smooth"),
        },
    }


def detect_warmup_end_idx(time, heartrate, max_hr):
    """
    Exclude warmup only when HR clearly ramps up after the first 5 minutes.
    Returns (warmup_end_idx, was_excluded).
    """
    if not time or len(time) < 3:
        return 0, False

    warmup_end_idx = next((i for i, t in enumerate(time) if (t - time[0]) >= WARMUP_MAX_SECONDS), len(time) - 1)

    if warmup_end_idx <= 1 or not heartrate or len(heartrate) <= warmup_end_idx + 3:
        return 0, False

    early_hr = [h for h in heartrate[:warmup_end_idx + 1] if h is not None]
    later_end_idx = min(len(heartrate), warmup_end_idx + 1 + (warmup_end_idx + 1))
    later_hr = [h for h in heartrate[warmup_end_idx + 1:later_end_idx] if h is not None]

    if not early_hr or not later_hr:
        return 0, False

    early_avg = statistics.mean(early_hr)
    later_avg = statistics.mean(later_hr)

    # Exclude first 5 minutes only if warmup HR is substantially lower than the next segment.
    should_exclude = (later_avg - early_avg) >= 6 and early_avg < (0.75 * max_hr)
    if should_exclude:
        return warmup_end_idx, True
    return 0, False


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

def duration_str(seconds) -> str:
    """Format seconds as M:SS."""
    s = int(round(seconds))
    m, s = divmod(s, 60)
    return f"{m}:{s:02d}"

def parse_pace(pace_s: str) -> Optional[float]:
    """'M:SS' string → float min/km, or None for 'n/a'."""
    if not pace_s or pace_s == "n/a":
        return None
    parts = pace_s.split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]) + int(parts[1]) / 60
    except ValueError:
        return None

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


def percentile(values, q):
    """Simple percentile helper without external deps."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * (q / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] + (vals[hi] - vals[lo]) * frac


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


def find_effort_blocks(velocity, time, threshold_factor=1.15, min_duration_s=30, min_gap_s=20, velocity_threshold=None):
    """
    Find sustained effort blocks where velocity exceeds threshold_factor * mean for at least min_duration_s.
    Returns list of effort dicts with start/end indices.
    """
    moving = [(i, v) for i, v in enumerate(velocity) if v > 0.3]
    if not moving:
        return []

    mean_vel = statistics.mean(v for _, v in moving)
    threshold = velocity_threshold if velocity_threshold is not None else (mean_vel * threshold_factor)

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


# ── Athlete hint parsing ─────────────────────────────────────────────────────

_HINT_MAP = {
    "lsd": "long_run",
    "long run": "long_run",
    "long": "long_run",
    "easy": "easy_run",
    "recovery": "recovery_run",
    "interval": "intervals",
    "intervals": "intervals",
    "tempo": "tempo",
    "threshold": "tempo",
    "fartlek": "fartlek",
    "hill": "hill_repeats",
    "hills": "hill_repeats",
    "hillrun": "hill_repeats",
    "hill repeats": "hill_repeats",
    "trail": "trail_mountain",
}

def parse_athlete_hint(description: str) -> Optional[str]:
    """Extract a session type hint from the Strava activity description."""
    if not description:
        return None
    desc_lower = description.lower().strip()
    for keyword, session_type in _HINT_MAP.items():
        if keyword in desc_lower:
            return session_type
    return None


def detect_session_type(streams, warmup_end_idx, max_hr, total_time_s, total_dist_m, athlete_hint=None):
    velocity = streams.get("velocity_smooth", [])
    altitude = streams.get("altitude", [])
    heartrate = streams.get("heartrate", [])
    time = streams.get("time", [])
    distance = streams.get("distance", [])

    # ── Athlete hint override ────────────────────────────────────────────────
    if athlete_hint:
        return athlete_hint, []

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

    # ── Long run / trail guard ───────────────────────────────────────────────
    # Runs over 55 minutes with significant elevation or walking segments are
    # trail/long runs where pace variance comes from terrain, not structured work.
    walking_points = sum(1 for v in velocity if 0 < v < 1.5)
    moving_points = sum(1 for v in velocity if v > 0.3)
    walking_pct = (walking_points / moving_points * 100) if moving_points else 0
    is_long_duration = total_time_s > 3300  # > 55 min
    is_hilly = gain_per_km > 8
    has_walking = walking_pct > 5

    if is_long_duration and (is_hilly or has_walking):
        working_hr = heartrate[warmup_end_idx:] if heartrate else []
        avg_hr = statistics.mean(working_hr) if working_hr else None
        if gain_per_km > 20:
            return "trail_mountain", []
        # On hilly/trail terrain, HR runs higher from elevation — use a higher
        # threshold before calling it tempo (flat runs use 0.82).
        tempo_hr_threshold = 0.87 if is_hilly else 0.82
        if avg_hr and avg_hr / max_hr > tempo_hr_threshold:
            return "tempo", []
        return "long_run", []

    if valid_paces and len(valid_paces) > 10:
        mean_pace = statistics.mean(valid_paces)
        stdev_pace = statistics.stdev(valid_paces)
        cv = stdev_pace / mean_pace if mean_pace > 0 else 0

        working_moving_vel = [v for v in working_vel if v > 0.3]
        adaptive_threshold = None
        if working_moving_vel:
            mean_working_vel = statistics.mean(working_moving_vel)
            # Use a mild threshold to capture sustained work blocks (not only peak surges).
            adaptive_threshold = mean_working_vel * 1.03

        pattern_efforts = find_effort_blocks(
            velocity,
            time,
            min_duration_s=60,
            min_gap_s=20,
            velocity_threshold=adaptive_threshold,
        ) if time and velocity and adaptive_threshold else []

        if len(pattern_efforts) >= 4:
            durations = [time[e["end_idx"]] - time[e["start_idx"]] for e in pattern_efforts]
            recoveries = [
                time[pattern_efforts[i + 1]["start_idx"]] - time[pattern_efforts[i]["end_idx"]]
                for i in range(len(pattern_efforts) - 1)
            ]
            avg_dur = statistics.mean(durations) if durations else 0
            avg_rec = statistics.mean(recoveries) if recoveries else 0
            dur_cv = (statistics.stdev(durations) / avg_dur) if len(durations) >= 3 and avg_dur > 0 else 1.0
            rec_cv = (statistics.stdev(recoveries) / avg_rec) if len(recoveries) >= 3 and avg_rec > 0 else 1.0

            if 55 <= avg_dur <= 260 and 35 <= avg_rec <= 220 and dur_cv < 0.65 and rec_cv < 0.75:
                return "intervals", pattern_efforts
            return "fartlek", pattern_efforts

        if cv > 0.16 and time and velocity:
            efforts = find_effort_blocks(velocity, time)
            if len(efforts) >= 2:
                # Check if fast blocks are predominantly downhill → hill session, not intervals
                grade = streams.get("grade_smooth", [])
                if grade:
                    effort_grades = []
                    for e in efforts:
                        seg = grade[e["start_idx"]:e["end_idx"] + 1]
                        if seg:
                            effort_grades.append(statistics.mean(seg))
                    avg_effort_grade = statistics.mean(effort_grades) if effort_grades else 0
                    if avg_effort_grade < -5 and gain_per_km > 15:
                        # Fast segments are descents — this is a hill session misclassified
                        if altitude:
                            repeats = find_hill_repeats(altitude, distance, min_gain=10)
                            if len(repeats) >= 2:
                                return "hill_repeats", repeats
                        return "trail_mountain", []

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
        "duration_str": duration_str(duration_s),
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

    # Warmup cutoff index (conditional, max first 5 minutes)
    warmup_end_idx, warmup_excluded = detect_warmup_end_idx(time, heartrate, max_hr)

    # ── Session type detection ──────────────────────────────────────────────
    athlete_hint = (
        parse_athlete_hint(meta.get("description", ""))
        or parse_athlete_hint(meta.get("private_note", ""))
    )
    session_type, structure_data = detect_session_type(
        streams, warmup_end_idx, max_hr, total_time_s, total_dist_m,
        athlete_hint=athlete_hint,
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
        "athlete_hint": athlete_hint,
        "athlete_description": meta.get("description", ""),
        "athlete_private_note": meta.get("private_note", ""),
        "overall": {
            "distance_km": round(total_dist_m / 1000, 2),
            "duration_s": round(total_time_s),
            "avg_pace": pace_str(avg_pace_overall),
            "avg_hr_adjusted": avg_hr_adjusted,
            "avg_hr_note": (
                f"Excludes first {WARMUP_MAX_SECONDS // 60} min warmup (HR ramp detected)"
                if warmup_excluded
                else "No warmup exclusion applied"
            ),
            "max_hr": round(max_hr_overall) if max_hr_overall else None,
            "elevation_gain_m": round(total_elevation_gain, 1),
            "gain_per_km": round(total_elevation_gain / total_dist_m * 1000, 1) if total_dist_m > 0 else 0,
            "hr_efficiency_index": hr_efficiency,
        },
        "hr_zones": zone_summary,
        "hr_drift_bpm": hr_drift,
        "pacing_split": pacing_split,
        "warmup_end_seconds": (time[warmup_end_idx] - time[0]) if warmup_excluded else 0,
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

        hill_summary = "n/a"
        if ascents:
            avg_climb_s = statistics.mean(a["duration_s"] for a in ascents)
            avg_gain = statistics.mean(a["elevation_gain_m"] for a in ascents)
            climb_paces = [parse_pace(a["avg_pace"]) for a in ascents if parse_pace(a["avg_pace"])]
            avg_climb_pace = pace_str(statistics.mean(climb_paces)) if climb_paces else "n/a"
            avg_climb_hr = round(statistics.mean(a["avg_hr"] for a in ascents if a["avg_hr"])) if any(a["avg_hr"] for a in ascents) else None
            parts = [f"{len(ascents)} × ~{duration_str(avg_climb_s)} climb ({round(avg_gain)}m) @ ~{avg_climb_pace}/km"]
            if avg_climb_hr:
                parts[0] += f" HR ~{avg_climb_hr}"
            if descents:
                avg_desc_s = statistics.mean(d["duration_s"] for d in descents)
                desc_paces = [parse_pace(d["avg_pace"]) for d in descents if parse_pace(d["avg_pace"])]
                avg_desc_pace = pace_str(statistics.mean(desc_paces)) if desc_paces else "n/a"
                parts.append(f"~{duration_str(avg_desc_s)} descent @ ~{avg_desc_pace}/km")
            hill_summary = " | ".join(parts)

        result["hill_repeats"] = {
            "total_reps": len(ascents),
            "summary": hill_summary,
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

        interval_summary = "n/a"
        if effort_details:
            avg_eff_s = statistics.mean(e["duration_s"] for e in effort_details)
            eff_paces = [parse_pace(e["avg_pace"]) for e in effort_details if parse_pace(e["avg_pace"])]
            avg_eff_pace = pace_str(statistics.mean(eff_paces)) if eff_paces else "n/a"
            parts = [f"{len(effort_details)} × ~{duration_str(avg_eff_s)} @ ~{avg_eff_pace}/km"]
            if avg_effort_hr:
                parts[0] += f" HR ~{avg_effort_hr}"
            if recovery_details:
                avg_rec_s = statistics.mean(r["duration_s"] for r in recovery_details)
                rec_paces = [parse_pace(r["avg_pace"]) for r in recovery_details if parse_pace(r["avg_pace"])]
                avg_rec_pace = pace_str(statistics.mean(rec_paces)) if rec_paces else "n/a"
                parts.append(f"~{duration_str(avg_rec_s)} rest @ ~{avg_rec_pace}/km")
            interval_summary = " | ".join(parts)

        result["structured_work"] = {
            "type": "intervals" if session_type == "intervals" else "fartlek",
            "total_efforts": len(effort_details),
            "summary": interval_summary,
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

    # ── Notable efforts for unstructured sessions ───────────────────────────
    # For easy/moderate/recovery/long/tempo runs, surface any fast bursts (strides, pickups)
    if session_type in ("easy_run", "moderate_run", "recovery_run", "long_run", "tempo"):
        effort_blocks_all = find_effort_blocks(velocity, time, threshold_factor=1.15, min_duration_s=30)
        if effort_blocks_all:
            notable = []
            for i, e in enumerate(effort_blocks_all, 1):
                seg = analyze_segment(streams, e["start_idx"], e["end_idx"])
                seg["number"] = i
                notable.append(seg)
            if notable:
                ne_paces = [parse_pace(n["avg_pace"]) for n in notable if parse_pace(n["avg_pace"])]
                ne_summary = (
                    f"{len(notable)} fast segment{'s' if len(notable) > 1 else ''} "
                    f"(≥30s at ≥15% above avg) | avg pace ~{pace_str(statistics.mean(ne_paces))}/km"
                    if ne_paces else f"{len(notable)} fast segment(s)"
                )
                result["notable_efforts"] = {
                    "count": len(notable),
                    "summary": ne_summary,
                    "efforts": notable,
                }

    return result


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        parsed = parse_args(sys.argv[1:])
        max_hr = parsed["max_hr"]
        filepath = parsed["filepath"]
        activity_id = parsed["activity_id"]
        env_file = Path(parsed["env_file"]).expanduser()
        save_input = parsed["save_input"]

        if activity_id:
            data = fetch_activity_input(activity_id, env_file)
            if save_input:
                Path(save_input).write_text(json.dumps(data, indent=2))
        elif filepath:
            with open(filepath) as f:
                data = json.load(f)
        else:
            data = json.load(sys.stdin)

        result = analyze(data, max_hr=max_hr)
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)
