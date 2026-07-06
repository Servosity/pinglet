"""
Heartbeat/Missed Task Detection for Pinglet.

Provides hourly detection of tasks whose last_run exceeds their expected_interval.
When gaps are found, user receives actionable notifications to run or ignore each missed task.
"""
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Tuple, Optional

from lib import state as state_module
from lib import ignored as ignored_module
from lib import alerts as alerts_module
from lib.logging import log
from lib.task_manager import MONITORING_AGENTS, _strip_macl_from_logs

# Re-export for testing patches
STATE_DIR = state_module.STATE_DIR
IGNORED_FILE = ignored_module.IGNORED_FILE
PROJECT_ROOT = Path(__file__).parent.parent


def _load_state(task_name: str):
    """Wrapper for load_state to allow patching."""
    return state_module.load_state(task_name)


def _is_ignored(task_name: str) -> bool:
    """Wrapper for is_ignored to allow patching."""
    return ignored_module.is_ignored(task_name)


def _load_ignored() -> dict:
    """Wrapper for load_ignored to allow patching."""
    return ignored_module.load_ignored()


def _clear_stale_ignores() -> list:
    """Wrapper for clear_stale_ignores to allow patching."""
    return ignored_module.clear_stale_ignores()


def _send_missed_task_notification(task_name, display_name, hours_overdue, threshold):
    """Wrapper for send_missed_task_notification to allow patching."""
    return alerts_module.send_missed_task_notification(task_name, display_name, hours_overdue, threshold)


def _send_slack_message(message):
    """Wrapper for send_slack_message to allow patching."""
    return alerts_module.send_slack_message(message)


def _get_launchd_status(task_id: str) -> dict:
    """Wrapper for get_launchd_status to allow patching."""
    from lib.task_manager import get_launchd_status
    return get_launchd_status(task_id)


def _get_launchd_run_count(task_id: str):
    """Wrapper for get_launchd_run_count to allow patching."""
    from lib.task_manager import get_launchd_run_count
    return get_launchd_run_count(task_id)


HEARTBEAT_ALERT_FILE = state_module.STATE_DIR / "_heartbeat_alerts.json"
ALERT_COOLDOWN_HOURS = 24

# Disabled agent alert tracking — prevents re-alerting every heartbeat cycle
DISABLED_AGENT_ALERT_FILE = state_module.STATE_DIR / "_disabled_agent_alerts.json"
DISABLED_AGENT_COOLDOWN_HOURS = 4

# Monitoring down state — threshold before alerting
MONITORING_DOWN_STATE_FILE = state_module.STATE_DIR / "_monitoring_down_state.json"
MONITORING_ALERT_THRESHOLD = 3  # Consecutive detections before human alert
MONITORING_ALERT_COOLDOWN_HOURS = 24

# Learning state
LEARNING_STATE_FILE = state_module.STATE_DIR / "_learning.json"
LEARNING_FILE = LEARNING_STATE_FILE  # Alias for test patching

# Task diagnosis state — tracks per-task LLM diagnosis attempts
TASK_DIAGNOSIS_STATE_FILE = state_module.STATE_DIR / "_task_diagnosis.json"
TASK_DIAGNOSIS_COOLDOWN_HOURS = 6  # Don't re-invoke LLM more than once per 6h per task
TASK_DIAGNOSIS_MIN_DETECTIONS = 2  # Need 2+ heartbeat detections before invoking LLM
TASK_DIAGNOSIS_CHRONIC_THRESHOLD = 5  # After this many recurring fix failures, stop LLM and alert human
TASK_FIX_DURABILITY_HOURS = 24  # If task re-breaks within this window after a "fix", it's recurring

# Task-level diagnosis prompt — for regular tasks (not monitoring agents)
# Unlike SELF_DIAGNOSIS_PROMPT, this allows fixing the working directory and dependencies
TASK_DIAGNOSE_PROMPT = """Pinglet task '{task_id}' anomaly: {detected_problem}.

TASK CONFIG:
- Name: {task_name}
- Command: {command}
- Working dir: {working_dir}
- Schedule: {schedule}
- Last run: {last_run}
- Consecutive failures: {consecutive_failures}

LAUNCHD STATUS: {launchd_status}
Recovery attempts so far: {recovery_attempts}

READ THESE FILES FIRST:
- tail -50 {log_dir}/pinglet.log | grep {task_id}
- tail -50 {log_dir}/{task_id}.log
- tail -50 {log_dir}/{task_id}.err
- cat {state_file}

LEARNING CONTEXT: Read {learning_file} for previous diagnosis history.

DIAGNOSIS STEPS:
1. Read logs to understand what happened
2. cd to working directory and investigate:
   - Does the command exist and is it executable?
   - Are dependencies intact (.venv/uv, git repos, config files)?
   - Check for broken git state (submodules, nested repos, dirty index)
   - Check for permission issues
   - Check for missing environment variables
3. Fix the root cause in the working directory
4. If launchd disabled (exit 78): fix plist, then bootout+bootstrap
5. If stale trigger: disable+enable the task via pinglet CLI
6. Verify the fix by running: {python_path} {project_root}/pinglet.py --run-now {task_id}

RULES:
- Fix the ROOT CAUSE, not just symptoms
- You MAY modify files in the working directory if that's where the bug is
- Do NOT modify other tasks or their working directories
- Do NOT delete data files unless they are clearly corrupt temp files
- Exit 0 if fixed (task runs successfully). Exit 1 if human intervention needed."""

# Prepended to TASK_DIAGNOSE_PROMPT when a previous fix didn't stick
RECURRING_FAILURE_CONTEXT = """
WARNING - RECURRING FAILURE: This task has been "fixed" {recurring_fix_failures} time(s) but keeps breaking.
Previous fix attempt: {last_fix_summary}
Total LLM diagnosis invocations for this task: {total_diagnose_invocations}

The previous approach did NOT persist. The standard re-bootstrap approach is NOT working.

You MUST try a DIFFERENT approach than re-bootstrapping. Consider:
- If this is a stale StartCalendarInterval: switch the plist to use StartInterval (seconds-based)
- If this is a permission or environment issue: check for machine-level changes
- If the task script itself is broken: fix the script, not just the schedule
- If the working directory has issues (git state, missing deps): fix those
- If you cannot fix the root cause: exit 1 to escalate to human intervention
"""

# Self-diagnosis prompt (monitoring agents only — restricted to plist fixes)
SELF_DIAGNOSIS_PROMPT = """Pinglet monitoring agent '{agent_id}' is DOWN. Fix it.

STATUS: {status} | EXIT: {exit_code} | LABEL: com.pinglet.{agent_id}
Auto-recovery (bootout+bootstrap) FAILED.

READ THESE FILES FIRST:
- tail -50 {log_dir}/launchd-heartbeat.log
- tail -50 {log_dir}/pinglet.log
- cat ~/Library/LaunchAgents/com.pinglet.{agent_id}.plist

LEARNING CONTEXT: Read {project_root}/state/_learning.json for previous diagnosis history and known issues for this agent. Check the agents.{agent_id}.known_issues array.

KNOWN FAILURE MODES:
- Exit 78 = KeepAlive + StartCalendarInterval conflict (remove KeepAlive from plist)
- Exit 1 after bootstrap = plist syntax error or missing binary
- "not_loaded" = plist removed from ~/Library/LaunchAgents/

FIX SEQUENCE:
1. Read logs + plist + learning context to identify root cause
2. Fix the root cause (edit plist, fix permissions, etc.)
3. launchctl bootout gui/$(id -u)/com.pinglet.{agent_id} 2>/dev/null
4. launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pinglet.{agent_id}.plist
5. Verify: launchctl list | grep {agent_id} shows PID or exit 0

RULES:
- Do NOT modify task scripts, only LaunchAgent plists
- Do NOT disable other agents
- Exit 0 if fixed. Exit 1 if you cannot fix it."""



__all__ = [name for name in globals() if not name.startswith("__")]
