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
from lib.task_manager_config import load_config
from lib.task_manager_launchd import get_launchd_status, is_task_enabled
from lib.task_manager_schedule import schedule_to_human

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
