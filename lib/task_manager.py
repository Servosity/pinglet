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
    python_path = str(PROJECT_ROOT / "venv" / "bin" / "python")
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
        <string>{python_path}</string>
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
# =============================================================================

def load_config() -> dict:
    """Load config.yaml."""
    if not CONFIG_PATH.exists():
        return {"tasks": {}}
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {"tasks": {}}


def save_config(config: dict) -> None:
    """Atomically write config dict to config.yaml."""
    temp_file = CONFIG_PATH.with_suffix(".tmp")
    with open(temp_file, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    temp_file.rename(CONFIG_PATH)


# =============================================================================
# CRUD Operations
# =============================================================================

def add_task(task_id: str, task_config: dict) -> dict:
    """Add a new task to config.yaml."""
    valid, err = validate_task_id(task_id)
    if not valid:
        return {"ok": False, "error": err}

    config = load_config()
    existing = config.get("tasks", {})

    valid, errors = validate_task_config(task_config, task_id, existing)
    if not valid:
        return {"ok": False, "error": "; ".join(errors)}

    # Apply defaults
    if "name" not in task_config:
        task_config["name"] = task_id.replace("-", " ").title()
    if "timeout" not in task_config:
        task_config["timeout"] = 300

    config.setdefault("tasks", {})[task_id] = task_config

    # Auto-set healthcheck expected_interval if schedule provided
    if "schedule" in task_config:
        try:
            sched = parse_schedule(task_config["schedule"])
            interval = estimate_expected_interval(sched)
            if interval:
                config.setdefault("healthcheck", {}).setdefault("expected_intervals", {})[task_id] = interval
        except ValueError:
            pass  # Invalid schedule stored as-is, will fail on enable

    save_config(config)
    return {"ok": True, "task_id": task_id, "config": task_config}


def edit_task(task_id: str, updates: dict) -> dict:
    """Edit an existing task. Only specified fields are updated (deep merge)."""
    config = load_config()
    tasks = config.get("tasks", {})

    if task_id not in tasks:
        return {"ok": False, "error": f"Task '{task_id}' not found"}

    before = dict(tasks[task_id])
    _deep_merge(tasks[task_id], updates)

    # Update healthcheck interval if schedule changed
    if "schedule" in updates:
        try:
            sched = parse_schedule(updates["schedule"])
            interval = estimate_expected_interval(sched)
            if interval:
                config.setdefault("healthcheck", {}).setdefault("expected_intervals", {})[task_id] = interval
        except ValueError:
            pass

    save_config(config)

    # Regenerate plist if enabled and schedule changed
    if "schedule" in updates and is_task_enabled(task_id):
        try:
            sched = parse_schedule(updates["schedule"])
            _write_and_install_plist(task_id, sched)
        except Exception as e:
            return {"ok": True, "task_id": task_id, "warning": f"Config updated but plist regeneration failed: {e}"}

    return {"ok": True, "task_id": task_id, "before": before, "after": tasks[task_id]}


def remove_task(task_id: str) -> dict:
    """Remove a task. Auto-disables if scheduled."""
    config = load_config()
    tasks = config.get("tasks", {})

    if task_id not in tasks:
        return {"ok": False, "error": f"Task '{task_id}' not found"}

    cleaned = []

    # Disable if enabled
    if is_task_enabled(task_id):
        result = disable_task(task_id)
        if result["ok"]:
            cleaned.append("disabled LaunchAgent")

    # Remove from config
    del tasks[task_id]
    cleaned.append("removed from config")

    # Remove from healthcheck
    hc = config.get("healthcheck", {}).get("expected_intervals", {})
    if task_id in hc:
        del hc[task_id]
        cleaned.append("removed healthcheck interval")

    save_config(config)

    # Remove state file
    state_file = STATE_DIR / f"{task_id}.json"
    if state_file.exists():
        state_file.unlink()
        cleaned.append("removed state file")

    return {"ok": True, "task_id": task_id, "cleaned": cleaned}


def show_task(task_id: str) -> dict:
    """Show full task details: config + state + enabled + schedule + recent logs."""
    config = load_config()
    tasks = config.get("tasks", {})

    if task_id not in tasks:
        return {"ok": False, "error": f"Task '{task_id}' not found"}

    task_config = tasks[task_id]
    state = load_state(task_id)
    enabled = is_task_enabled(task_id)

    # Get schedule from config or try to read from plist
    schedule_str = task_config.get("schedule")
    if not schedule_str:
        plist_path = LAUNCHAGENTS_DIR / f"com.pinglet.{task_id}.plist"
        if plist_path.exists():
            schedule_str = _read_schedule_from_plist(plist_path)

    # Recent logs (last 20 lines from each file)
    log_result = get_task_logs(task_id, lines=20)
    logs = log_result.get("log_files", {})

    launchd = get_launchd_status(task_id)

    return {
        "ok": True,
        "task_id": task_id,
        "config": task_config,
        "state": {
            "last_run": state.last_run,
            "last_status": state.last_status,
            "last_error": state.last_error,
            "last_duration_seconds": state.last_duration_seconds,
            "consecutive_failures": state.consecutive_failures,
            "total_runs": state.total_runs,
            "total_failures": state.total_failures,
        },
        "enabled": enabled,
        "launchd": launchd,
        "schedule": schedule_str,
        "recent_logs": logs,
    }


def list_tasks_json() -> list:
    """List all tasks with config + state summary."""
    config = load_config()
    tasks = config.get("tasks", {})
    result = []

    for task_id, task_config in tasks.items():
        state = load_state(task_id)
        enabled = is_task_enabled(task_id)
        launchd = get_launchd_status(task_id)
        schedule_str = task_config.get("schedule")

        result.append({
            "task_id": task_id,
            "name": task_config.get("name", task_id),
            "command": task_config.get("command", ""),
            "timeout": task_config.get("timeout", 300),
            "schedule": schedule_str,
            "enabled": enabled,
            "launchd": launchd,
            "last_run": state.last_run,
            "last_status": state.last_status,
            "consecutive_failures": state.consecutive_failures,
            "total_runs": state.total_runs,
        })

    return result


def get_task_logs(task_id: str, lines: int = 50) -> dict:
    """Read recent log lines for a task from all matching log files."""
    log_files = {}

    # Check all possible log file patterns
    patterns = [
        f"{task_id}.log",
        f"{task_id}.err",
        f"launchd-{task_id}.log",
        f"launchd-{task_id}.err",
    ]

    for pattern in patterns:
        log_path = LOGS_DIR / pattern
        if log_path.exists():
            log_files[str(log_path)] = _tail_file(log_path, lines)

    # Also grep pinglet.log for this task's entries
    pinglet_log = LOGS_DIR / "pinglet.log"
    if pinglet_log.exists():
        task_lines = _grep_file(pinglet_log, f"[{task_id}]", lines)
        if task_lines:
            log_files[str(pinglet_log)] = task_lines

    return {"ok": True, "task_id": task_id, "log_files": log_files}


# =============================================================================
# LaunchAgent Management
# =============================================================================

def enable_task(task_id: str, schedule_spec: str = None) -> dict:
    """Enable a task: generate plist + load LaunchAgent."""
    config = load_config()
    tasks = config.get("tasks", {})

    if task_id not in tasks:
        return {"ok": False, "error": f"Task '{task_id}' not found in config"}

    # Get schedule
    if schedule_spec:
        try:
            schedule_dict = parse_schedule(schedule_spec)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        # Store in config
        tasks[task_id]["schedule"] = schedule_spec
        save_config(config)
    else:
        stored_schedule = tasks[task_id].get("schedule")
        if not stored_schedule:
            return {"ok": False, "error": f"No schedule set for '{task_id}'. Use --schedule first."}
        try:
            schedule_dict = parse_schedule(stored_schedule)
        except ValueError as e:
            return {"ok": False, "error": f"Invalid stored schedule: {e}"}

    # Disable first if already enabled (reload)
    if is_task_enabled(task_id):
        disable_task(task_id)

    try:
        plist_path = _write_and_install_plist(task_id, schedule_dict)
    except Exception as e:
        return {"ok": False, "error": f"Failed to install LaunchAgent: {e}"}

    # Verify launchd accepted the config
    launchd_status = get_launchd_status(task_id)
    if launchd_status.get("disabled"):
        # Launchd rejected — clean up and report error
        label = f"com.pinglet.{task_id}"
        uid = _get_uid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
        return {
            "ok": False,
            "error": f"LaunchAgent rejected by launchd (exit {launchd_status.get('exit_code')}). "
                     "Check plist for invalid config (e.g. KeepAlive + StartCalendarInterval).",
            "task_id": task_id,
            "launchd_status": launchd_status,
        }

    return {
        "ok": True, "task_id": task_id, "plist": str(plist_path),
        "schedule": schedule_to_human(schedule_dict),
        "launchd_status": launchd_status,
    }


def disable_task(task_id: str) -> dict:
    """Disable a task: unload LaunchAgent + remove plist from ~/Library/LaunchAgents."""
    label = f"com.pinglet.{task_id}"
    plist_path = USER_LAUNCHAGENTS_DIR / f"{label}.plist"

    if not plist_path.exists():
        return {"ok": True, "task_id": task_id, "note": "Already disabled (no plist found)"}

    # Bootout
    uid = _get_uid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{label}"],
        capture_output=True,
    )

    # Remove user plist
    plist_path.unlink(missing_ok=True)

    return {"ok": True, "task_id": task_id, "note": "LaunchAgent disabled"}


def is_task_enabled(task_id: str) -> bool:
    """Check if a task's LaunchAgent is installed."""
    return (USER_LAUNCHAGENTS_DIR / f"com.pinglet.{task_id}.plist").exists()


def get_launchd_status(task_id: str) -> Dict[str, Any]:
    """Get actual launchd status for a task's LaunchAgent.

    Returns dict with:
        installed: bool - plist exists in ~/Library/LaunchAgents
        running: bool - launchd reports the agent as running (PID != -)
        exit_code: int or None - last exit code from launchctl list
        disabled: bool - True if exit_code == 78
        status: str - one of: running, disabled, failed, not_loaded, not_installed
    """
    label = f"com.pinglet.{task_id}"
    plist_exists = (USER_LAUNCHAGENTS_DIR / f"{label}.plist").exists()

    if not plist_exists:
        return {
            "installed": False,
            "running": False,
            "exit_code": None,
            "disabled": False,
            "status": "not_installed",
        }

    # Query launchctl list for this specific label
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            # Agent plist exists but not loaded in launchd
            return {
                "installed": True,
                "running": False,
                "exit_code": None,
                "disabled": False,
                "status": "not_loaded",
            }

        # Parse output: launchctl list <label> outputs key-value pairs
        # We need PID and LastExitStatus
        pid = None
        exit_code = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if '"PID"' in line or line.startswith('"PID"'):
                # Format: "PID" = 12345;
                parts = line.split("=")
                if len(parts) >= 2:
                    val = parts[1].strip().rstrip(";").strip()
                    try:
                        pid = int(val)
                    except ValueError:
                        pass
            elif '"LastExitStatus"' in line or line.startswith('"LastExitStatus"'):
                parts = line.split("=")
                if len(parts) >= 2:
                    val = parts[1].strip().rstrip(";").strip()
                    try:
                        exit_code = int(val)
                    except ValueError:
                        pass

        # Determine status
        # launchd stores exit codes as (exit_code << 8), so 78 -> 19968
        normalized_exit = exit_code
        if exit_code is not None and exit_code > 255:
            normalized_exit = exit_code >> 8

        is_running = pid is not None and pid > 0
        is_disabled = normalized_exit == 78

        if is_running:
            status = "running"
        elif is_disabled:
            status = "disabled"
        elif exit_code is not None and exit_code != 0:
            status = "failed"
        else:
            status = "idle"  # loaded, not currently running, last exit was 0

        return {
            "installed": True,
            "running": is_running,
            "exit_code": normalized_exit,
            "disabled": is_disabled,
            "status": status,
        }

    except (subprocess.TimeoutExpired, OSError):
        # launchctl failed — fall back to plist-existence check
        return {
            "installed": plist_exists,
            "running": False,
            "exit_code": None,
            "disabled": False,
            "status": "unknown",
        }


def get_all_launchd_statuses() -> Dict[str, Dict[str, Any]]:
    """Get launchd status for all pinglet agents by parsing launchctl list once.

    Returns dict mapping task_id -> status dict (same shape as get_launchd_status).
    """
    config = load_config()
    task_ids = list(config.get("tasks", {}).keys())
    # Also include monitoring agents
    all_ids = task_ids + [a for a in MONITORING_AGENTS if a not in task_ids]

    results = {}
    for task_id in all_ids:
        results[task_id] = get_launchd_status(task_id)
    return results


def set_schedule(task_id: str, schedule_spec: str) -> dict:
    """Set schedule for a task in config (does not enable)."""
    config = load_config()
    tasks = config.get("tasks", {})

    if task_id not in tasks:
        return {"ok": False, "error": f"Task '{task_id}' not found"}

    # Validate schedule
    try:
        sched = parse_schedule(schedule_spec)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    tasks[task_id]["schedule"] = schedule_spec

    # Update healthcheck
    interval = estimate_expected_interval(sched)
    if interval:
        config.setdefault("healthcheck", {}).setdefault("expected_intervals", {})[task_id] = interval

    save_config(config)

    return {
        "ok": True,
        "task_id": task_id,
        "schedule": schedule_spec,
        "schedule_parsed": schedule_to_human(sched),
        "expected_interval_hours": interval,
        "note": "Schedule saved. Use --task-enable to activate.",
    }


# =============================================================================
# Helpers
# =============================================================================

def _deep_merge(base: dict, updates: dict) -> None:
    """Deep merge updates into base dict (mutates base)."""
    for key, value in updates.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _get_uid() -> str:
    """Get current user UID."""
    return subprocess.check_output(["id", "-u"]).decode().strip()


def _write_and_install_plist(task_id: str, schedule_dict: dict) -> Path:
    """Generate plist, write to launchagents/, copy to ~/Library/LaunchAgents/, bootstrap."""
    plist_content = generate_plist(task_id, schedule_dict)
    label = f"com.pinglet.{task_id}"

    # Write to project launchagents/
    LAUNCHAGENTS_DIR.mkdir(exist_ok=True)
    local_plist = LAUNCHAGENTS_DIR / f"{label}.plist"
    local_plist.write_text(plist_content)

    # Copy to ~/Library/LaunchAgents/
    USER_LAUNCHAGENTS_DIR.mkdir(exist_ok=True)
    user_plist = USER_LAUNCHAGENTS_DIR / f"{label}.plist"
    user_plist.write_text(plist_content)

    # Bootstrap
    uid = _get_uid()
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(user_plist)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip()}")

    return user_plist


def _tail_file(path: Path, lines: int) -> str:
    """Read last N lines of a file."""
    try:
        with open(path, "r") as f:
            return "\n".join(deque(f, maxlen=lines)).strip()
    except Exception:
        return ""


def _grep_file(path: Path, pattern: str, max_lines: int) -> str:
    """Return last max_lines matching pattern from a file."""
    try:
        matches = deque(maxlen=max_lines)
        with open(path, "r") as f:
            for line in f:
                if pattern in line:
                    matches.append(line.rstrip())
        return "\n".join(matches)
    except Exception:
        return ""


def get_system_status() -> dict:
    """Get structured system status for agent consumption.

    Returns a comprehensive JSON dict any agent or script can parse.
    """
    import json as _json
    from datetime import datetime as _dt
    from lib.heartbeat import (
        _load_monitoring_down_state,
        _load_heartbeat_alert_state,
        _load_learning_state,
        MONITORING_ALERT_THRESHOLD,
        MONITORING_ALERT_COOLDOWN_HOURS,
    )

    config = load_config()
    tasks_config = config.get("tasks", {})
    now = _dt.now()

    # Monitoring agents status
    monitoring_status = {}
    for agent_id in MONITORING_AGENTS:
        status = get_launchd_status(agent_id)
        monitoring_status[agent_id] = {
            "status": status["status"],
            "healthy": not status["disabled"] and status["status"] not in ("not_installed",),
        }

    # Task statuses
    task_list = []
    healthy_count = 0
    issue_list = []

    for task_id, task_cfg in tasks_config.items():
        state = load_state(task_id)
        launchd = get_launchd_status(task_id)
        enabled = is_task_enabled(task_id)

        # Determine health
        if launchd["disabled"]:
            status_str = "disabled"
        elif state.consecutive_failures > 0:
            status_str = "failing"
        elif not state.last_run:
            status_str = "unknown"
        else:
            # Check staleness
            expected = config.get("healthcheck", {}).get("expected_intervals", {}).get(task_id)
            if expected:
                try:
                    last_run_dt = _dt.fromisoformat(state.last_run)
                    hours_since = (now - last_run_dt).total_seconds() / 3600
                    status_str = "stale" if hours_since > expected else "healthy"
                except ValueError:
                    status_str = "unknown"
            else:
                status_str = "healthy"

        if status_str == "healthy":
            healthy_count += 1
        elif status_str not in ("unknown",):
            issue_list.append({
                "task_id": task_id,
                "severity": "warning" if status_str == "stale" else "error",
                "message": f"{status_str}: {state.last_error[:100] if state.last_error else 'no details'}",
            })

        task_list.append({
            "task_id": task_id,
            "name": task_cfg.get("name", task_id),
            "status": status_str,
            "launchd_status": launchd["status"],
            "enabled": enabled,
            "last_run": state.last_run,
            "consecutive_failures": state.consecutive_failures,
            "schedule": task_cfg.get("schedule"),
            "has_on_failure": "on_failure" in task_cfg,
        })

    # Load alert and learning state
    monitoring_down = _load_monitoring_down_state()
    heartbeat_cooldowns = _load_heartbeat_alert_state()
    learning = _load_learning_state()

    # Learning summary
    learning_summary = {}
    for agent_id, agent_data in learning.get("agents", {}).items():
        summary = {
            "pattern": agent_data.get("pattern", "unknown"),
            "suppressed": agent_data.get("suppressed", False),
            "effective_threshold": agent_data.get("effective_threshold", MONITORING_ALERT_THRESHOLD),
        }
        if agent_data.get("total_reload_cycles", 0) > 0:
            summary["total_reload_cycles"] = agent_data["total_reload_cycles"]
        learning_summary[agent_id] = summary

    # Add learning-detected patterns to issues
    for agent_id, data in learning.get("agents", {}).items():
        if data.get("pattern") == "chronic_cycle":
            issue_list.append({
                "task_id": agent_id,
                "severity": "info",
                "message": "Chronic cycle (auto-recovers, suppressed)",
            })
        elif data.get("pattern") == "reload_cycle":
            reload_count = data.get("total_reload_cycles", 0)
            issue_list.append({
                "task_id": agent_id,
                "severity": "warning",
                "message": f"Reload cycle ({reload_count}x bootstrap without real fix — underlying task/script is broken)",
            })

    total_tasks = len(tasks_config)
    all_monitoring_healthy = all(m["healthy"] for m in monitoring_status.values())
    system_ok = healthy_count == total_tasks and all_monitoring_healthy

    return {
        "ok": system_ok,
        "healthy": healthy_count == total_tasks,
        "timestamp": now.isoformat(),
        "summary": f"{total_tasks} tasks: {healthy_count} healthy, {total_tasks - healthy_count} issues",
        "monitoring": monitoring_status,
        "tasks": task_list,
        "alerts": {
            "monitoring_down": monitoring_down,
            "heartbeat_cooldowns": heartbeat_cooldowns,
        },
        "learning": learning_summary,
        "issues": issue_list,
        "defaults": {
            "retry_max_attempts": 3,
            "retry_delays_seconds": [10, 60, 300],
            "consecutive_failures_before_alert": 3,
            "alert_cooldown_minutes": 30,
            "monitoring_alert_threshold": MONITORING_ALERT_THRESHOLD,
            "monitoring_alert_cooldown_hours": MONITORING_ALERT_COOLDOWN_HOURS,
            "on_failure_timeout": 180,
            "on_failure_max_turns": 5,
            "on_failure_max_budget_usd": 2.00,
            "self_diagnosis_max_budget_usd": 1.00,
        },
    }


def _read_schedule_from_plist(plist_path: Path) -> Optional[str]:
    """Try to read schedule from a plist file and convert to human-readable."""
    try:
        import plistlib
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)
        if "StartInterval" in plist:
            return schedule_to_human({"StartInterval": plist["StartInterval"]})
        if "StartCalendarInterval" in plist:
            return schedule_to_human({"StartCalendarInterval": plist["StartCalendarInterval"]})
    except Exception:
        pass
    return None
