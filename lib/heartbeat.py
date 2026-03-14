"""
Heartbeat/Missed Task Detection for Pinglet.

Provides hourly detection of tasks whose last_run exceeds their expected_interval.
When gaps are found, user receives actionable notifications to run or ignore each missed task.
"""
import json
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Tuple, Optional

from lib import state as state_module
from lib import ignored as ignored_module
from lib import alerts as alerts_module
from lib.logging import log

# Re-export for testing patches
STATE_DIR = state_module.STATE_DIR
IGNORED_FILE = ignored_module.IGNORED_FILE


def _load_state(task_name: str):
    """Wrapper for load_state to allow patching."""
    return state_module.load_state(task_name)


def _is_ignored(task_name: str) -> bool:
    """Wrapper for is_ignored to allow patching."""
    return ignored_module.is_ignored(task_name)


def _load_ignored() -> dict:
    """Wrapper for load_ignored to allow patching."""
    return ignored_module.load_ignored()


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


def _disable_task(task_id: str) -> dict:
    """Wrapper for disable_task to allow patching."""
    from lib.task_manager import disable_task
    return disable_task(task_id)


def _enable_task(task_id: str) -> dict:
    """Wrapper for enable_task to allow patching."""
    from lib.task_manager import enable_task
    return enable_task(task_id)


def _get_uid() -> str:
    """Wrapper for _get_uid to allow patching."""
    from lib.task_manager import _get_uid
    return _get_uid()


HEARTBEAT_ALERT_FILE = state_module.STATE_DIR / "_heartbeat_alerts.json"
ALERT_COOLDOWN_HOURS = 24


def _load_heartbeat_alert_state() -> dict:
    """Load per-task heartbeat alert state from disk."""
    if HEARTBEAT_ALERT_FILE.exists():
        try:
            with open(HEARTBEAT_ALERT_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _save_heartbeat_alert_state(state: dict) -> None:
    """Save per-task heartbeat alert state to disk."""
    state_module.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(HEARTBEAT_ALERT_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _should_alert_for_task(task_name: str, escalation: str, alert_state: dict) -> bool:
    """Check if we should send a Slack alert for this task.

    Only re-alerts after ALERT_COOLDOWN_HOURS or if escalation level changed.

    Args:
        task_name: Task identifier
        escalation: Current escalation level (warning/urgent/critical)
        alert_state: Current alert state dict

    Returns:
        True if alert should be sent
    """
    prev = alert_state.get(task_name)
    if not prev:
        return True

    try:
        prev_time = datetime.fromisoformat(prev.get("last_alert", "2000-01-01"))
        hours_since = (datetime.now() - prev_time).total_seconds() / 3600

        # Re-alert if cooldown expired or escalation level increased
        if hours_since >= ALERT_COOLDOWN_HOURS:
            return True
        if prev.get("escalation") != escalation:
            return True
        return False
    except (ValueError, TypeError):
        return True


def _attempt_auto_recovery(agent: Dict, config: dict) -> bool:
    """Attempt to auto-recover a disabled/failed LaunchAgent.

    For regular tasks (in config.tasks): disable + enable (regenerates plist).
    For monitoring agents (healthcheck, heartbeat): bootout + bootstrap existing plist.

    Returns True if recovery succeeded, False otherwise.
    """
    import subprocess
    from lib.task_manager import USER_LAUNCHAGENTS_DIR

    task_id = agent["task_id"]
    label = agent["label"]
    tasks_config = config.get("tasks", {})

    if task_id in tasks_config:
        # Regular task — use disable+enable to regenerate plist
        log(f"Auto-recovering regular task: {task_id}", "heartbeat")
        try:
            _disable_task(task_id)
            result = _enable_task(task_id)
            if result.get("ok"):
                log(f"Auto-recovery succeeded for {task_id}", "heartbeat")
                return True
            else:
                log(f"Auto-recovery failed for {task_id}: {result.get('error')}", "heartbeat")
                return False
        except Exception as e:
            log(f"Auto-recovery exception for {task_id}: {e}", "heartbeat")
            return False
    else:
        # Monitoring agent — bootout + bootstrap existing plist
        log(f"Auto-recovering monitoring agent: {task_id}", "heartbeat")
        try:
            uid = _get_uid()
            plist_path = USER_LAUNCHAGENTS_DIR / f"{label}.plist"

            if not plist_path.exists():
                log(f"Cannot recover {task_id}: plist not found at {plist_path}", "heartbeat")
                return False

            # Bootout (ignore errors — may not be loaded)
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}/{label}"],
                capture_output=True,
            )

            # Bootstrap
            result = subprocess.run(
                ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
                capture_output=True, text=True,
            )

            if result.returncode != 0:
                log(f"Bootstrap failed for {task_id}: {result.stderr.strip()}", "heartbeat")
                return False

            # Verify recovery
            status = _get_launchd_status(task_id)
            if status.get("disabled"):
                log(f"Recovery failed — {task_id} still disabled after bootstrap", "heartbeat")
                return False

            log(f"Auto-recovery succeeded for monitoring agent {task_id}", "heartbeat")
            return True
        except Exception as e:
            log(f"Auto-recovery exception for {task_id}: {e}", "heartbeat")
            return False


def detect_disabled_agents(config: dict) -> List[Dict]:
    """Detect LaunchAgents that are disabled by launchd (exit code 78).

    Args:
        config: Full configuration dictionary

    Returns:
        List of dicts with keys: task_id, label, exit_code, status
    """
    from lib.task_manager import MONITORING_AGENTS

    tasks_config = config.get("tasks", {})
    all_ids = list(tasks_config.keys()) + [a for a in MONITORING_AGENTS if a not in tasks_config]

    disabled = []
    for task_id in all_ids:
        status = _get_launchd_status(task_id)
        if status["disabled"] or status["status"] == "failed":
            disabled.append({
                "task_id": task_id,
                "label": f"com.pinglet.{task_id}",
                "exit_code": status["exit_code"],
                "status": status["status"],
            })

    return disabled


def get_escalation_level(hours_overdue: float, threshold: float) -> str:
    """Determine escalation level based on staleness multiplier.

    Args:
        hours_overdue: Hours past threshold
        threshold: Expected interval in hours

    Returns:
        Escalation level: "warning", "urgent", or "critical"
    """
    total_hours = hours_overdue + threshold
    if threshold <= 0:
        return "warning"

    multiplier = total_hours / threshold
    if multiplier >= 10:
        return "critical"
    elif multiplier >= 5:
        return "urgent"
    return "warning"


def detect_missed_tasks(config: dict) -> List[Dict]:
    """
    Detect tasks that haven't run within their expected interval.

    Uses existing healthcheck.expected_intervals configuration.

    Args:
        config: Full configuration dictionary

    Returns:
        List of missed task info dicts with keys:
        - task_name: Task identifier
        - display_name: Human-readable name
        - hours_overdue: Hours past threshold
        - threshold: Expected interval in hours
        - last_run: ISO timestamp of last run
        - never_run: True if task has never run
    """
    expected_intervals = config.get("healthcheck", {}).get("expected_intervals", {})
    tasks_config = config.get("tasks", {})

    missed = []
    now = datetime.now()

    for task_name, threshold in expected_intervals.items():
        # Skip ignored tasks
        if _is_ignored(task_name):
            log(f"Skipping ignored task: {task_name}", "heartbeat")
            continue

        # Load task state
        state = _load_state(task_name)
        display_name = tasks_config.get(task_name, {}).get("name", task_name)

        if not state.last_run:
            # Task has never run
            missed.append({
                "task_name": task_name,
                "display_name": display_name,
                "hours_overdue": float(threshold),  # Treat as fully overdue
                "threshold": threshold,
                "last_run": None,
                "never_run": True,
            })
            continue

        # Calculate hours since last run
        try:
            last_run_dt = datetime.fromisoformat(state.last_run)
            hours_since = (now - last_run_dt).total_seconds() / 3600

            if hours_since > threshold:
                missed.append({
                    "task_name": task_name,
                    "display_name": display_name,
                    "hours_overdue": round(hours_since - threshold, 1),
                    "threshold": threshold,
                    "last_run": state.last_run,
                    "never_run": False,
                })
        except ValueError:
            log(f"Invalid last_run timestamp for {task_name}", "heartbeat")

    return missed


def should_run_task(task_name: str, config: dict) -> Tuple[bool, str]:
    """
    Check if a task should be run via --run-now.

    Handles stale notification case where task already ran.

    Args:
        task_name: Name of the task to check
        config: Full configuration dictionary

    Returns:
        (should_run, reason) tuple
    """
    expected_intervals = config.get("healthcheck", {}).get("expected_intervals", {})
    threshold = expected_intervals.get(task_name)

    if not threshold:
        # No expected interval defined, always allow run
        return True, "no_threshold_defined"

    state = _load_state(task_name)

    if not state.last_run:
        return True, "never_run"

    try:
        last_run_dt = datetime.fromisoformat(state.last_run)
        hours_since = (datetime.now() - last_run_dt).total_seconds() / 3600

        if hours_since > threshold:
            return True, f"missed_by_{hours_since - threshold:.1f}h"
        else:
            last_run_str = last_run_dt.strftime("%I:%M%p").lower()
            return False, f"already ran at {last_run_str}"
    except ValueError:
        return True, "invalid_timestamp"


def run_heartbeat(config: dict, wake_delay: Optional[int] = None) -> dict:
    """
    Run the heartbeat check.

    Detects missed tasks and sends notifications.

    Args:
        config: Full configuration dictionary
        wake_delay: Seconds to wait before notifying (default from config)

    Returns:
        Result dict with missed_count and tasks
    """
    heartbeat_config = config.get("heartbeat", {})

    if wake_delay is None:
        wake_delay = heartbeat_config.get("wake_delay_seconds", 30)

    log("Running heartbeat check", "heartbeat")

    # Check for disabled agents and attempt auto-recovery
    disabled = detect_disabled_agents(config)
    auto_recovered = []
    still_disabled = []

    if disabled:
        disabled_names = [d["task_id"] for d in disabled]
        log(f"Found {len(disabled)} disabled/failed agent(s): {', '.join(disabled_names)}", "heartbeat")

        for agent in disabled:
            if _attempt_auto_recovery(agent, config):
                auto_recovered.append(agent["task_id"])
            else:
                still_disabled.append(agent)

        if still_disabled:
            still_names = [d["task_id"] for d in still_disabled]
            log(f"ALERT: {len(still_disabled)} agent(s) could not be recovered: {', '.join(still_names)}", "heartbeat")
            from lib.alerts import send_critical_monitoring_alert
            send_critical_monitoring_alert(still_disabled)

    # Detect missed tasks
    missed_tasks = detect_missed_tasks(config)

    if not missed_tasks and not still_disabled:
        log("All tasks up to date", "heartbeat")
        return {"missed_count": 0, "tasks": [], "disabled_agents": [], "auto_recovered": auto_recovered}

    if missed_tasks:
        log(f"Found {len(missed_tasks)} missed task(s)", "heartbeat")

    # Wait for wake delay (allows system to stabilize after wake)
    if wake_delay > 0 and missed_tasks:
        log(f"Waiting {wake_delay}s wake delay before notifying", "heartbeat")
        time.sleep(wake_delay)

    # Load per-task alert cooldown state
    alert_state = _load_heartbeat_alert_state()

    # Send notifications for each missed task
    for task in missed_tasks:
        task_name = task["task_name"]
        display_name = task["display_name"]
        hours_overdue = task["hours_overdue"]
        threshold = task["threshold"]
        last_run = task.get("last_run")

        # Determine escalation level
        level = get_escalation_level(hours_overdue, threshold)
        task["escalation"] = level

        level_prefix = ""
        if level == "critical":
            level_prefix = "CRITICAL: "
        elif level == "urgent":
            level_prefix = "URGENT: "

        log(f"Notifying for missed task: {task_name} ({hours_overdue:.1f}h overdue, {level})", "heartbeat")

        # Send macOS notification with actions (always — lightweight, local)
        _send_missed_task_notification(
            task_name=task_name,
            display_name=display_name,
            hours_overdue=hours_overdue,
            threshold=threshold,
        )

        # Send Slack notification only if cooldown allows
        if _should_alert_for_task(task_name, level, alert_state):
            if last_run:
                try:
                    last_run_str = datetime.fromisoformat(last_run).strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    last_run_str = "Unknown"
            else:
                last_run_str = "Never"

            slack_message = f"""*Pinglet: {level_prefix}Missed Task*
`{task_name}` hasn't run in {hours_overdue + threshold:.1f} hours (threshold: {threshold}h)
Last successful run: {last_run_str}
Escalation: {level.upper()}

_Use macOS notification to Run or Ignore_"""

            _send_slack_message(slack_message)

            # Update cooldown state for this task
            alert_state[task_name] = {
                "last_alert": datetime.now().isoformat(),
                "escalation": level,
            }
        else:
            log(f"Slack alert suppressed for {task_name} (cooldown active)", "heartbeat")

    # Save updated alert state
    _save_heartbeat_alert_state(alert_state)

    return {
        "missed_count": len(missed_tasks),
        "tasks": missed_tasks,
        "disabled_agents": still_disabled,
        "auto_recovered": auto_recovered,
    }
