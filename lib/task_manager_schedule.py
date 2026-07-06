"""
Task management for Pinglet.

CRUD operations on tasks, schedule parsing, plist generation,
LaunchAgent enable/disable, and log reading.
"""
import json
import os
import re
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from lib.state import load_state

# --- Monitoring agent labels ---
MONITORING_AGENTS = ["healthcheck", "heartbeat"]


# --- Paths ---
PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
LAUNCHAGENTS_DIR = PROJECT_ROOT / "launchagents"
USER_LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LOGS_DIR = PROJECT_ROOT / "logs"
STATE_DIR = PROJECT_ROOT / "state"

# --- Schedule Constants ---
WEEKDAY_MAP = {
    "sun": 0, "sunday": 0,
    "mon": 1, "monday": 1,
    "tue": 2, "tuesday": 2,
    "wed": 3, "wednesday": 3,
    "thu": 4, "thursday": 4,
    "fri": 5, "friday": 5,
    "sat": 6, "saturday": 6,
}

WEEKDAY_REVERSE = {0: "sun", 1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat"}


# =============================================================================
# Schedule Parsing
# =============================================================================

def parse_schedule(schedule_spec: str) -> dict:
    """Parse human-friendly schedule string into plist schedule dict.

    Formats:
        "every 1h"          -> {"StartInterval": 3600}
        "every 30m"         -> {"StartInterval": 1800}
        "every 3600s"       -> {"StartInterval": 3600}
        "daily 7:00"        -> {"StartCalendarInterval": {"Hour": 7, "Minute": 0}}
        "daily 7:00,19:00"  -> {"StartCalendarInterval": [{"Hour": 7, ...}, {"Hour": 19, ...}]}
        "hourly :22"        -> {"StartCalendarInterval": {"Minute": 22}}
        "weekly mon 7:33"   -> {"StartCalendarInterval": {"Weekday": 1, "Hour": 7, "Minute": 33}}
    """
    spec = schedule_spec.strip().lower()

    # Pattern: "every <N><unit>"
    every_match = re.match(r"^every\s+(\d+)(s|m|h)$", spec)
    if every_match:
        value = int(every_match.group(1))
        unit = every_match.group(2)
        multiplier = {"s": 1, "m": 60, "h": 3600}[unit]
        return {"StartInterval": value * multiplier}

    # Pattern: "hourly :MM" — run at specific minute each hour
    hourly_match = re.match(r"^hourly\s+:(\d{1,2})$", spec)
    if hourly_match:
        minute = int(hourly_match.group(1))
        if not (0 <= minute <= 59):
            raise ValueError(f"Minute must be 0-59, got {minute}")
        return {"StartCalendarInterval": {"Minute": minute}}

    # Pattern: "daily <time>[,<time>...]"
    daily_match = re.match(r"^daily\s+(.+)$", spec)
    if daily_match:
        times_str = daily_match.group(1)
        times = [_parse_time(t.strip()) for t in times_str.split(",")]
        if len(times) == 1:
            return {"StartCalendarInterval": times[0]}
        return {"StartCalendarInterval": times}

    # Pattern: "weekly <weekday> <time>"
    weekly_match = re.match(r"^weekly\s+(\w+)\s+(\d+:\d+)$", spec)
    if weekly_match:
        weekday_str = weekly_match.group(1)
        time_dict = _parse_time(weekly_match.group(2))
        weekday = WEEKDAY_MAP.get(weekday_str)
        if weekday is None:
            raise ValueError(f"Unknown weekday: {weekday_str}. Use: {', '.join(WEEKDAY_MAP.keys())}")
        time_dict["Weekday"] = weekday
        return {"StartCalendarInterval": time_dict}

    raise ValueError(
        f"Cannot parse schedule: '{schedule_spec}'. "
        f"Use: 'every Nh/Nm/Ns', 'daily HH:MM[,HH:MM]', or 'weekly DAY HH:MM'"
    )


def _parse_time(time_str: str) -> dict:
    """Parse 'HH:MM' into {"Hour": H, "Minute": M}."""
    parts = time_str.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format: '{time_str}'. Use HH:MM")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        raise ValueError(f"Invalid time format: '{time_str}'. Use HH:MM")
    if not (0 <= hour <= 23):
        raise ValueError(f"Hour must be 0-23, got {hour}")
    if not (0 <= minute <= 59):
        raise ValueError(f"Minute must be 0-59, got {minute}")
    return {"Hour": hour, "Minute": minute}


def schedule_to_human(schedule_dict: dict) -> str:
    """Convert plist schedule dict back to human-readable string."""
    if "StartInterval" in schedule_dict:
        seconds = schedule_dict["StartInterval"]
        if seconds % 3600 == 0:
            return f"every {seconds // 3600}h"
        elif seconds % 60 == 0:
            return f"every {seconds // 60}m"
        else:
            return f"every {seconds}s"

    cal = schedule_dict.get("StartCalendarInterval")
    if cal is None:
        return "unknown"

    if isinstance(cal, list):
        times = [f"{d['Hour']}:{d['Minute']:02d}" for d in cal]
        return f"daily {','.join(times)}"

    if "Weekday" in cal:
        day = WEEKDAY_REVERSE.get(cal["Weekday"], str(cal["Weekday"]))
        return f"weekly {day} {cal['Hour']}:{cal['Minute']:02d}"

    # Hourly — only Minute key, no Hour
    if "Minute" in cal and "Hour" not in cal:
        return f"hourly :{cal['Minute']:02d}"

    return f"daily {cal['Hour']}:{cal['Minute']:02d}"


def estimate_expected_interval(schedule_dict: dict) -> Optional[float]:
    """Estimate healthcheck expected_interval in hours from schedule."""
    if "StartInterval" in schedule_dict:
        return round(schedule_dict["StartInterval"] * 1.5 / 3600, 1)

    cal = schedule_dict.get("StartCalendarInterval")
    if cal is None:
        return None

    if isinstance(cal, dict):
        if "Weekday" in cal:
            return 180  # ~7.5 days
        if "Minute" in cal and "Hour" not in cal:
            return 1.5  # hourly + grace
        return 26  # 24h + 2h grace

    # Array of calendar intervals — estimate max gap
    if isinstance(cal, list) and len(cal) >= 2:
        minutes = sorted(d["Hour"] * 60 + d["Minute"] for d in cal)
        gaps = []
        for i in range(1, len(minutes)):
            gaps.append(minutes[i] - minutes[i - 1])
        # Wrap-around gap
        gaps.append(1440 - minutes[-1] + minutes[0])
        max_gap_hours = max(gaps) / 60
        return round(max_gap_hours + 2, 1)

    return 26


# =============================================================================
# Validation
# =============================================================================

def validate_task_id(task_id: str) -> Tuple[bool, str]:
    """Validate task ID: lowercase alphanum + hyphens, 2-50 chars, no leading/trailing hyphen."""
    if len(task_id) < 2:
        return False, "Task ID must be at least 2 characters"
    if len(task_id) > 50:
        return False, "Task ID must be at most 50 characters"
    if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", task_id) and len(task_id) >= 2:
        if task_id[0] == "-" or task_id[-1] == "-":
            return False, "Task ID cannot start or end with a hyphen"
        return False, "Task ID must be lowercase alphanumeric with hyphens only"
    return True, ""


def validate_task_config(task_config: dict, task_id: str, existing_tasks: dict) -> Tuple[bool, List[str]]:
    """Validate a task configuration dict. Returns (valid, [errors])."""
    errors = []

    if task_id in existing_tasks:
        errors.append(f"Task '{task_id}' already exists")

    if not task_config.get("command"):
        errors.append("'command' is required")

    timeout = task_config.get("timeout")
    if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
        errors.append("'timeout' must be a positive integer")

    out_fmt = task_config.get("output", {}).get("format")
    if out_fmt is not None and out_fmt not in ("text", "json"):
        errors.append("'output.format' must be 'text' or 'json'")

    return len(errors) == 0, errors


# =============================================================================
# Plist Generation
# =============================================================================

def generate_plist(task_id: str, schedule_dict: dict) -> str:
    """Generate plist XML for a task's LaunchAgent.

    KeepAlive is only added for StartInterval schedules (periodic tasks).
    StartCalendarInterval + KeepAlive causes launchd config error (exit 78).
    """
    project_root = str(PROJECT_ROOT)
    schedule_xml = _render_schedule_xml(schedule_dict)

    # Only add KeepAlive for interval-based schedules, not calendar-based
    keepalive_xml = ""
    if "StartInterval" in schedule_dict:
        keepalive_xml = """    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.pinglet.{task_id}</string>
    <key>ProgramArguments</key>
    <array>
        <string>uv</string>
        <string>run</string>
        <string>python</string>
        <string>pinglet.py</string>
        <string>--task</string>
        <string>{task_id}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{project_root}</string>
{schedule_xml}
    <key>StandardOutPath</key>
    <string>{project_root}/logs/{task_id}.log</string>
    <key>StandardErrorPath</key>
    <string>{project_root}/logs/{task_id}.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
{keepalive_xml}</dict>
</plist>
"""


def _render_schedule_xml(schedule_dict: dict) -> str:
    """Render the schedule portion of a plist as XML."""
    if "StartInterval" in schedule_dict:
        return f"    <key>StartInterval</key>\n    <integer>{schedule_dict['StartInterval']}</integer>"

    cal = schedule_dict.get("StartCalendarInterval")
    if cal is None:
        return ""

    if isinstance(cal, dict):
        inner = _render_calendar_interval_xml(cal, indent=2)
        return f"    <key>StartCalendarInterval</key>\n{inner}"

    if isinstance(cal, list):
        items = "\n".join(_render_calendar_interval_xml(d, indent=3) for d in cal)
        return f"    <key>StartCalendarInterval</key>\n    <array>\n{items}\n    </array>"

    return ""


def _render_calendar_interval_xml(interval: dict, indent: int = 2) -> str:
    """Render a single calendar interval dict as plist XML."""
    prefix = "    " * indent
    lines = [f"{prefix}<dict>"]
    for key in ("Weekday", "Hour", "Minute"):
        if key in interval:
            lines.append(f"{prefix}    <key>{key}</key>")
            lines.append(f"{prefix}    <integer>{interval[key]}</integer>")
    lines.append(f"{prefix}</dict>")
    return "\n".join(lines)


# =============================================================================
# Config I/O
