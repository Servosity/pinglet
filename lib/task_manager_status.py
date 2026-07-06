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
            "has_on_diagnose": "on_diagnose" in task_cfg,
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
