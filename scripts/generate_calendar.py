#!/usr/bin/env python3
"""
StravaCoach calendar generator.
Reads planned session markdown files and generates an ICS calendar file.

Usage:
    python3 scripts/generate_calendar.py [--plan-dir plan/sessions] [--goal goals/goal_current.md] [--output calendar/schedule.ics]

No LLM needed — pure file parsing + ICS generation.
"""

import os
import re
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

# ── Defaults ─────────────────────────────────────────────────────────────────

WORKSPACE = Path(__file__).resolve().parent.parent
DEFAULT_PLAN_DIR = WORKSPACE / "plan" / "sessions"
DEFAULT_GOAL_FILE = WORKSPACE / "goals" / "goal_current.md"
DEFAULT_OUTPUT = WORKSPACE / "calendar" / "schedule.ics"
TIMEZONE = "Europe/Brussels"
PRODID = "-//StravaCoach//Training Plan//EN"

# ── Dutch month mapping ──────────────────────────────────────────────────────

DUTCH_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4,
    "mei": 5, "juni": 6, "juli": 7, "augustus": 8,
    "september": 9, "oktober": 10, "november": 11, "december": 12,
}

# ── Args ─────────────────────────────────────────────────────────────────────

def parse_args(argv):
    args = {
        "plan_dir": str(DEFAULT_PLAN_DIR),
        "goal_file": str(DEFAULT_GOAL_FILE),
        "output": str(DEFAULT_OUTPUT),
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--plan-dir" and i + 1 < len(argv):
            args["plan_dir"] = argv[i + 1]
            i += 2
        elif arg == "--goal" and i + 1 < len(argv):
            args["goal_file"] = argv[i + 1]
            i += 2
        elif arg == "--output" and i + 1 < len(argv):
            args["output"] = argv[i + 1]
            i += 2
        elif arg == "--help":
            print(
                "Usage: python3 generate_calendar.py [options]\n"
                "  --plan-dir DIR    Session plan directory (default: plan/sessions)\n"
                "  --goal FILE       Goal file (default: goals/goal_current.md)\n"
                "  --output FILE     Output ICS file (default: calendar/schedule.ics)\n"
            )
            sys.exit(0)
        else:
            print(f"Unknown argument: {arg}", file=sys.stderr)
            sys.exit(1)
        continue
    return args


# ── Markdown parsing ─────────────────────────────────────────────────────────

def extract_field(text, field_name):
    """Extract a **Field:** value from markdown text."""
    pattern = rf"\*\*{re.escape(field_name)}:\*\*\s*(.+)"
    match = re.search(pattern, text)
    if match:
        return match.group(1).strip().rstrip("  ")
    return None


def parse_date_from_filename(filename):
    """Extract YYYY-MM-DD from session_YYYY-MM-DD_type.md filename."""
    match = re.search(r"session_(\d{4}-\d{2}-\d{2})_", filename)
    if match:
        return match.group(1)
    return None


def parse_time_from_datum(datum_str):
    """
    Extract start and end time from Datum field.
    e.g. 'do 30 april 2026, 18:00–19:30' → ('18:00', '19:30')
    """
    if not datum_str:
        return None, None
    # Match time range: HH:MM–HH:MM or HH:MM-HH:MM
    time_match = re.search(r"(\d{1,2}:\d{2})\s*[–\-]\s*(\d{1,2}:\d{2})", datum_str)
    if time_match:
        return time_match.group(1), time_match.group(2)
    return None, None


def parse_session_file(filepath):
    """Parse a session markdown file into structured data."""
    text = Path(filepath).read_text(encoding="utf-8")

    # Title from first heading
    title_match = re.search(r"^#\s+(?:Sessie:\s*)?(.+)", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else Path(filepath).stem

    # Date from filename (source of truth)
    date_str = parse_date_from_filename(Path(filepath).name)
    if not date_str:
        return None

    # Time from Datum field
    datum = extract_field(text, "Datum")
    start_time, end_time = parse_time_from_datum(datum)

    # Other fields
    session_type = extract_field(text, "Type") or ""
    status = extract_field(text, "Status") or ""
    completed_raw = extract_field(text, "Voltooid") or "nee"
    completed = completed_raw.lower().startswith("ja")
    distance = extract_field(text, "Afstand") or ""
    duration = extract_field(text, "Duur") or ""
    block = extract_field(text, "Blok") or ""
    hoogtemeters = extract_field(text, "Hoogtemeters") or ""

    # Build a concise prescription for the calendar summary
    prescription_parts = []
    if distance:
        prescription_parts.append(distance)
    elif hoogtemeters:
        prescription_parts.append(hoogtemeters)
    if duration and not distance:
        prescription_parts.append(duration)
    prescription = " — ".join(prescription_parts) if prescription_parts else ""

    # Purpose section
    purpose = ""
    purpose_match = re.search(r"^## Doel\s*\n(.+?)(?=\n## |\Z)", text, re.MULTILINE | re.DOTALL)
    if purpose_match:
        purpose = purpose_match.group(1).strip()

    # Full markdown content as description
    description = text

    return {
        "title": title,
        "date": date_str,
        "start_time": start_time,
        "end_time": end_time,
        "type": session_type,
        "status": status,
        "completed": completed,
        "prescription": prescription,
        "description": description,
        "filepath": str(filepath),
    }


def parse_goal_race(goal_file):
    """Extract race/event date and name from goal file."""
    if not Path(goal_file).exists():
        return None

    text = Path(goal_file).read_text(encoding="utf-8")

    # Look for **Date:** field
    date_str = extract_field(text, "Date")
    primary = extract_field(text, "Primary")

    if not date_str:
        return None

    # Parse English date formats: "July 20, 2026" or "2026-07-20"
    race_date = None
    # Try ISO format
    iso_match = re.match(r"(\d{4}-\d{2}-\d{2})", date_str)
    if iso_match:
        race_date = iso_match.group(1)
    else:
        # Try "Month DD, YYYY"
        eng_months = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }
        month_match = re.match(r"(\w+)\s+(\d{1,2}),?\s+(\d{4})", date_str)
        if month_match:
            month_name = month_match.group(1).lower()
            day = int(month_match.group(2))
            year = int(month_match.group(3))
            month_num = eng_months.get(month_name)
            if month_num:
                race_date = f"{year}-{month_num:02d}-{day:02d}"

    if not race_date:
        return None

    return {
        "title": primary or "Goal Event",
        "date": race_date,
        "description": f"Doeldatum: {primary}" if primary else "Goal event",
    }


# ── ICS generation ───────────────────────────────────────────────────────────

def ics_escape(text):
    """Escape text for ICS DESCRIPTION/SUMMARY fields."""
    if not text:
        return ""
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;")
    text = text.replace(",", "\\,")
    text = text.replace("\n", "\\n")
    return text


def fold_line(line):
    """Fold ICS lines at 75 octets per RFC 5545."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line

    result = []
    while len(encoded) > 75:
        # Find a safe split point (don't break multi-byte chars)
        cut = 75 if not result else 74  # subsequent lines have leading space
        while cut > 0 and (encoded[cut] & 0xC0) == 0x80:
            cut -= 1
        if cut == 0:
            cut = 75 if not result else 74
        chunk = encoded[:cut]
        encoded = encoded[cut:]
        result.append(chunk.decode("utf-8", errors="replace"))

    if encoded:
        result.append(encoded.decode("utf-8", errors="replace"))

    return "\r\n ".join(result)


def make_uid(date_str, session_type):
    """Generate a deterministic UID for a session event."""
    slug = re.sub(r"[^a-z0-9]+", "-", session_type.lower()).strip("-") if session_type else "session"
    return f"stravacoach-{date_str}-{slug}@stravacoach"


def format_ics_date(date_str):
    """Convert YYYY-MM-DD to YYYYMMDD."""
    return date_str.replace("-", "")


def format_ics_datetime(date_str, time_str):
    """Convert date + time to YYYYMMDDTHHMMSS."""
    h, m = time_str.split(":")
    return f"{date_str.replace('-', '')}T{int(h):02d}{int(m):02d}00"


def session_to_vevent(session):
    """Convert a parsed session to a VEVENT string."""
    date_str = session["date"]
    start_time = session.get("start_time")
    end_time = session.get("end_time")

    # Summary
    prefix = "✓ " if session["completed"] else ""
    summary = f"{prefix}{session['title']}"
    if session.get("prescription"):
        # Title already includes prescription in most cases, skip if redundant
        pass

    # Description
    description = ics_escape(session.get("description", ""))
    if session["completed"]:
        description = "✓ VOLTOOID\\n\\n" + description

    # UID
    uid = make_uid(date_str, session.get("type", ""))

    # Timestamp
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now}",
    ]

    if start_time and end_time:
        lines.append(f"DTSTART;TZID={TIMEZONE}:{format_ics_datetime(date_str, start_time)}")
        lines.append(f"DTEND;TZID={TIMEZONE}:{format_ics_datetime(date_str, end_time)}")
    else:
        # All-day event
        ics_date = format_ics_date(date_str)
        next_day = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y%m%d")
        lines.append(f"DTSTART;VALUE=DATE:{ics_date}")
        lines.append(f"DTEND;VALUE=DATE:{next_day}")

    lines.append(f"SUMMARY:{ics_escape(summary)}")

    if description:
        lines.append(f"DESCRIPTION:{description}")

    if session.get("status"):
        lines.append(f"STATUS:{'COMPLETED' if session['completed'] else 'CONFIRMED'}")

    lines.append("END:VEVENT")

    return "\r\n".join(fold_line(line) for line in lines)


def race_to_vevent(race):
    """Convert the goal race to a VEVENT string."""
    date_str = race["date"]
    ics_date = format_ics_date(date_str)
    next_day = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y%m%d")
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VEVENT",
        f"UID:stravacoach-{date_str}-goal@stravacoach",
        f"DTSTAMP:{now}",
        f"DTSTART;VALUE=DATE:{ics_date}",
        f"DTEND;VALUE=DATE:{next_day}",
        f"SUMMARY:🎯 {ics_escape(race['title'])}",
        f"DESCRIPTION:{ics_escape(race.get('description', ''))}",
        "END:VEVENT",
    ]

    return "\r\n".join(fold_line(line) for line in lines)


def generate_vtimezone():
    """Generate a VTIMEZONE component for Europe/Brussels (CET/CEST)."""
    return "\r\n".join([
        "BEGIN:VTIMEZONE",
        "TZID:Europe/Brussels",
        "BEGIN:DAYLIGHT",
        "DTSTART:19700329T020000",
        "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3",
        "TZOFFSETFROM:+0100",
        "TZOFFSETTO:+0200",
        "TZNAME:CEST",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "DTSTART:19701025T030000",
        "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10",
        "TZOFFSETFROM:+0200",
        "TZOFFSETTO:+0100",
        "TZNAME:CET",
        "END:STANDARD",
        "END:VTIMEZONE",
    ])


def generate_ics(sessions, race=None):
    """Generate the full ICS calendar string."""
    parts = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:StravaCoach Training Plan",
        f"X-WR-TIMEZONE:{TIMEZONE}",
    ]

    # Add timezone definition
    parts.append(generate_vtimezone())

    # Sort sessions by date
    sorted_sessions = sorted(sessions, key=lambda s: s["date"])

    for session in sorted_sessions:
        parts.append(session_to_vevent(session))

    if race:
        parts.append(race_to_vevent(race))

    parts.append("END:VCALENDAR")
    return "\r\n".join(parts) + "\r\n"


# ── Main ─────────────────────────────────────────────────────────────────────

def collect_sessions(plan_dir):
    """Walk all block directories and parse session files."""
    sessions = []
    plan_path = Path(plan_dir)

    if not plan_path.exists():
        print(f"Plan directory not found: {plan_dir}", file=sys.stderr)
        return sessions

    for block_dir in sorted(plan_path.iterdir()):
        if not block_dir.is_dir():
            continue
        for session_file in sorted(block_dir.glob("session_*.md")):
            session = parse_session_file(session_file)
            if session:
                sessions.append(session)

    return sessions


def main():
    args = parse_args(sys.argv[1:])
    plan_dir = args["plan_dir"]
    goal_file = args["goal_file"]
    output_file = args["output"]

    # Collect sessions
    sessions = collect_sessions(plan_dir)

    # Parse goal race
    race = parse_goal_race(goal_file)

    # Generate ICS
    ics_content = generate_ics(sessions, race)

    # Write output
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(ics_content, encoding="utf-8")

    # Report
    completed_count = sum(1 for s in sessions if s["completed"])
    upcoming_count = len(sessions) - completed_count
    print(f"Calendar generated: {output_file}")
    print(f"  Sessions: {len(sessions)} ({completed_count} voltooid, {upcoming_count} gepland)")
    if race:
        print(f"  Goal event: {race['title']} ({race['date']})")


if __name__ == "__main__":
    main()
