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
from lib.task_manager_config import load_config, save_config
from lib.task_manager_schedule import (
    estimate_expected_interval,
    generate_plist,
    parse_schedule,
    schedule_to_human,
)

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


def get_launchd_run_count(task_id: str) -> Optional[int]:
    """Get the number of times launchd has fired an agent since bootstrap.

    Uses 'launchctl print' to parse the 'runs = N' field. This detects
    stale calendar triggers where the agent is loaded but never fires.

    Returns:
        Run count (int), or None if agent is not loaded or parse fails.
    """
    label = f"com.pinglet.{task_id}"
    uid = _get_uid()

    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None

        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("runs = "):
                try:
                    return int(line.split("=")[1].strip())
                except (ValueError, IndexError):
                    pass
        return None
    except (subprocess.TimeoutExpired, OSError):
        return None


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
def _get_uid() -> str:
    """Get current user UID."""
    return subprocess.check_output(["id", "-u"]).decode().strip()


def _strip_macl_from_logs(task_id: str) -> None:
    """Strip com.apple.macl xattr from task log files.

    macOS Sequoia adds Managed Access Control Lists to launchd-managed
    stdout/stderr files. Once a MACL evolves into a blocking state, launchd
    can't write to the file and the agent exits 78 (EX_CONFIG) in a crash
    loop. Auto-recovery isn't enough unless the file is reset.

    `xattr -d com.apple.macl` returns exit 0 but does NOT remove the attribute
    (it's kernel-protected). The only reliable reset is to delete the file
    and recreate it empty; launchd opens the fresh file on next bootstrap.

    Covers both regular task logs ({task_id}.log/.err) and monitoring agent
    logs (launchd-{task_id}.log), since heartbeat and healthcheck use the
    launchd- prefix.
    """
    candidates = [
        LOGS_DIR / f"{task_id}.log",
        LOGS_DIR / f"{task_id}.err",
        LOGS_DIR / f"launchd-{task_id}.log",
    ]
    for log_path in candidates:
        if not log_path.exists():
            continue
        try:
            log_path.unlink()
            log_path.touch()
        except OSError:
            pass


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

    # Strip MACL xattrs from log files to prevent launchd exit 78
    _strip_macl_from_logs(task_id)

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
