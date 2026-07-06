import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

# Add lib to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib.alerts import (
    send_critical,
    send_recovery,
    send_health_summary,
    send_macos_notification,
    send_manual_complete_notification,
    test_alerts,
)
from lib.logging import log, log_run_start, log_run_end, get_log_file_path
from lib.reliability import ReliabilityConfig, ReliabilityManager
from lib.state import (
    load_state,
    update_state_success,
    update_state_failure,
    load_all_states,
)
from lib.heartbeat import run_heartbeat as heartbeat_run, should_run_task
from lib.ignored import ignore_task as mark_ignored, clear_ignored, is_ignored

from lib.pinglet_callbacks import _run_on_failure_callback

def load_config() -> dict:
    """Load configuration from config.yaml."""
    config_path = PROJECT_ROOT / "config.yaml"
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def run_task(task_name: str) -> int:
    """
    Run a registered task with reliability features.

    Includes:
    - Automatic retry with exponential backoff
    - Consecutive failure threshold for alert gating
    - Alert cooldown to prevent spam
    - Recovery notifications

    Args:
        task_name: Name of the task from config.yaml

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    config = load_config()
    tasks = config.get("tasks", {})

    if task_name not in tasks:
        print(f"ERROR: Unknown task '{task_name}'")
        print(f"Available tasks: {', '.join(tasks.keys())}")
        return 1

    task_config = tasks[task_name]
    display_name = task_config.get("name", task_name)
    command = task_config.get("command")
    args = task_config.get("args", [])
    working_dir = task_config.get("working_dir", str(PROJECT_ROOT))
    timeout = task_config.get("timeout", 300)  # Default 5 minutes
    env_vars = task_config.get("env", [])

    # Build reliability configuration (global defaults + task overrides)
    reliability_config = ReliabilityConfig.from_config(
        config.get("reliability", {}),
        task_config.get("reliability", {}),
    )
    reliability = ReliabilityManager(task_name, reliability_config)

    # Build command list
    cmd = [command] + args

    # Build environment (inherit current + add specified)
    env = os.environ.copy()
    for var_name in env_vars:
        if var_name in os.environ:
            env[var_name] = os.environ[var_name]

    log_run_start(task_name)
    log(f"Running: {' '.join(cmd)}", task_name)
    log(f"Working dir: {working_dir}", task_name)
    log(f"Timeout: {timeout}s", task_name)
    log(f"Reliability: threshold={reliability_config.alert_threshold}, max_retries={reliability_config.max_retry_attempts}", task_name)

    overall_start_time = time.time()

    def execute_once() -> tuple:
        """Execute the task once, returning (exit_code, stdout, stderr)."""
        start_time = time.time()
        try:
            result = subprocess.run(
                cmd,
                cwd=working_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.returncode, result.stdout or "", result.stderr or ""

        except subprocess.TimeoutExpired as e:
            log(f"TIMEOUT | Task timed out after {timeout} seconds", task_name)
            return 124, e.stdout or "", e.stderr or ""

        except Exception as e:
            log(f"EXCEPTION | {str(e)}", task_name)
            return 1, "", str(e)

    # Execute with automatic retry
    success, exit_code, stdout_data, stderr_data = reliability.execute_with_retry(execute_once)

    # Log output (last 20 lines)
    if stdout_data:
        for line in stdout_data.strip().split("\n")[-20:]:
            log(f"STDOUT | {line}", task_name)
    if stderr_data:
        for line in stderr_data.strip().split("\n")[-20:]:
            log(f"STDERR | {line}", task_name)

    duration = time.time() - overall_start_time
    error_message = stderr_data or stdout_data or f"Exit code {exit_code}"

    # Update state and handle alerts
    if success:
        # Track previous failures for recovery notification
        previous_failures = reliability.get_previous_failures()

        update_state_success(task_name, duration)
        log_run_end(task_name, "success", duration)

        # Check for recovery (success after failures)
        if reliability.is_recovery():
            log(f"RECOVERY | Task recovered after {previous_failures} consecutive failures", task_name)
            send_recovery(
                task_name=display_name,
                previous_failures=previous_failures,
                details={"Duration": f"{duration:.1f}s"},
            )
    else:
        is_timeout = exit_code == 124
        update_state_failure(task_name, error_message, duration, timeout=is_timeout)
        log_run_end(task_name, "timeout" if is_timeout else "failed", duration, {"error": error_message[:50]})

        # Check if we should alert (threshold + cooldown)
        should_alert, alert_reason = reliability.should_alert()
        log(f"Alert decision: {should_alert} ({alert_reason})", task_name)

        if should_alert:
            # Reload state to get current consecutive_failures count
            current_state = load_state(task_name)

            # Run on_failure callback if configured
            llm_result = _run_on_failure_callback(
                task_name, task_config, exit_code, error_message,
                current_state.consecutive_failures,
                config.get("agent_runners", {}),
            )

            send_critical(
                task_name=display_name,
                error=error_message,
                details={
                    "Exit code": exit_code,
                    "Duration": f"{duration:.1f}s",
                    "Consecutive failures": current_state.consecutive_failures,
                    "Retry attempts": reliability_config.max_retry_attempts,
                },
                log_file=str(get_log_file_path()),
                task_id=task_name,
                llm_result=llm_result if llm_result.get("invoked") else None,
            )
            reliability.record_alert_sent()

    # After each task run, check if monitoring agents are dead
    # This is the "who watches the watchmen" safety net
    if success:
        from lib.pinglet_launch import _check_monitoring_agents, _maybe_send_monitoring_alert
        dead = _check_monitoring_agents()
        if dead:
            _maybe_send_monitoring_alert(dead)

    return exit_code


def run_healthcheck() -> int:
    """
    Run daily health check - summarizes all task states.

    Checks both state timestamps AND launchd agent status.
    Agents disabled by launchd (exit 78) are flagged regardless of
    what the state file says — this catches the "task stopped after
    success" blindspot.

    Returns:
        Exit code (0 for healthy, 1 for issues)
    """
    from lib.task_manager import get_launchd_status

    config = load_config()
    expected_intervals = config.get("healthcheck", {}).get("expected_intervals", {})
    tasks_config = config.get("tasks", {})

    log("Running health check", "healthcheck")

    states = load_all_states()
    now = datetime.now()
    task_summaries = []
    all_healthy = True

    for state in states:
        task_name = state.task_name
        display_name = tasks_config.get(task_name, {}).get("name", task_name)

        # Check launchd status first — overrides state-file checks
        launchd = get_launchd_status(task_name)

        # Calculate time since last run
        issue = None
        status = "OK"

        if launchd["disabled"]:
            status = "DISABLED"
            issue = f"LaunchAgent disabled (exit 78) — run: --task-enable {task_name}"
            all_healthy = False
        elif launchd["status"] == "failed":
            status = "AGENT_FAILED"
            issue = f"LaunchAgent failed (exit {launchd['exit_code']})"
            all_healthy = False
        elif state.last_run:
            last_run_dt = datetime.fromisoformat(state.last_run)
            hours_since = (now - last_run_dt).total_seconds() / 3600

            # Check expected interval
            expected_hours = expected_intervals.get(task_name)
            if expected_hours and hours_since > expected_hours:
                status = "STALE"
                issue = f"Last run {hours_since:.1f}h ago (expected: {expected_hours}h)"
                all_healthy = False

            # Check for consecutive failures
            if state.consecutive_failures > 0:
                status = "FAILING"
                issue = f"{state.consecutive_failures} consecutive failures"
                all_healthy = False
        else:
            status = "UNKNOWN"
            issue = "Task has never run"
            all_healthy = False

        last_run_str = "Never"
        if state.last_run:
            try:
                last_run_str = datetime.fromisoformat(state.last_run).strftime("%I:%M %p")
            except ValueError:
                last_run_str = state.last_run

        task_summaries.append({
            "name": display_name,
            "last_run": last_run_str,
            "status": status,
            "runs_today": state.runs_today,
            "issue": issue or "-",
        })

    # Also add tasks that have no state yet
    for task_name in tasks_config:
        if not any(s.task_name == task_name for s in states):
            display_name = tasks_config[task_name].get("name", task_name)
            launchd = get_launchd_status(task_name)

            if launchd["disabled"]:
                issue = f"LaunchAgent disabled (exit 78) — run: --task-enable {task_name}"
                status = "DISABLED"
            else:
                issue = "No state file"
                status = "UNKNOWN"

            task_summaries.append({
                "name": display_name,
                "last_run": "Never",
                "status": status,
                "runs_today": 0,
                "issue": issue,
            })
            all_healthy = False

    # Send summary
    send_health_summary(task_summaries, healthy=all_healthy)

    log(f"Health check complete: {'All healthy' if all_healthy else 'Issues found'}", "healthcheck")
    return 0 if all_healthy else 1


def run_now(task_name: str, config: dict = None) -> int:
    """
    Run a task immediately (used by notification actions).

    Handles stale notification case where task already ran.

    Args:
        task_name: Name of the task to run
        config: Optional config dict (loaded if not provided)

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    if config is None:
        config = load_config()

    tasks = config.get("tasks", {})
    if task_name not in tasks:
        print(f"ERROR: Unknown task '{task_name}'")
        return 1

    display_name = tasks[task_name].get("name", task_name)

    # Check if task still qualifies as missed
    should_run, reason = should_run_task(task_name, config)

    if not should_run:
        log(f"Task already ran, skipping: {reason}", task_name)
        send_macos_notification(
            f"Pinglet: {display_name}",
            f"Task {reason}, skipping",
        )
        return 0

    log(f"Running task manually: {task_name} ({reason})", task_name)

    # Clear ignored status before running
    clear_ignored(task_name)

    # Run the task
    exit_code = run_task(task_name)

    # Send completion notification
    send_manual_complete_notification(
        task_name=task_name,
        display_name=display_name,
        success=exit_code == 0,
        config=config,
        error=None if exit_code == 0 else f"Exit code {exit_code}",
    )

    return exit_code


def ignore_task(task_name: str, config: dict = None) -> int:
    """
    Mark a task as ignored (used by notification actions).

    Args:
        task_name: Name of the task to ignore
        config: Optional config dict (loaded if not provided)

    Returns:
        Exit code (always 0)
    """
    if config is None:
        config = load_config()

    tasks = config.get("tasks", {})
    expected_intervals = config.get("healthcheck", {}).get("expected_intervals", {})

    if task_name not in tasks:
        print(f"ERROR: Unknown task '{task_name}'")
        return 1

    display_name = tasks[task_name].get("name", task_name)
    threshold = expected_intervals.get(task_name, 0)

    # Get current state
    state = load_state(task_name)
    last_run = state.last_run or "Never"

    # Calculate hours overdue
    hours_overdue = 0.0
    if state.last_run and threshold > 0:
        try:
            last_run_dt = datetime.fromisoformat(state.last_run)
            hours_since = (datetime.now() - last_run_dt).total_seconds() / 3600
            hours_overdue = max(0, hours_since - threshold)
        except ValueError:
            pass

    # Mark as ignored
    mark_ignored(
        task_name=task_name,
        last_run=last_run,
        threshold=threshold,
        hours_overdue=hours_overdue,
    )

    log(f"Manual skip: last_run {hours_overdue + threshold:.1f}h ago (threshold {threshold}h)", task_name)
    print(f"Task '{display_name}' marked as ignored until next scheduled run.")

    return 0


def run_heartbeat_command(config: dict = None) -> int:
    """
    Run heartbeat check for missed tasks (used by LaunchAgent).

    Args:
        config: Optional config dict (loaded if not provided)

    Returns:
        Exit code (0 for no missed tasks, 1 for missed tasks found)
    """
    if config is None:
        config = load_config()

    result = heartbeat_run(config)

    if result["missed_count"] > 0:
        return 1
    return 0
