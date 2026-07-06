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
from lib.task_manager_schedule import (
    estimate_expected_interval,
    parse_schedule,
    validate_task_config,
    validate_task_id,
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
    from lib.task_manager_launchd import _write_and_install_plist, is_task_enabled
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
    from lib.task_manager_launchd import disable_task, is_task_enabled
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
def _deep_merge(base: dict, updates: dict) -> None:
    """Deep merge updates into base dict (mutates base)."""
    for key, value in updates.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
