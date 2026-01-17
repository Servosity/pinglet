#!/usr/bin/env python3
"""
Pinglet - Universal task wrapper that guarantees no silent failures.

Usage:
    pinglet.py --task <task_name>     Run a registered task
    pinglet.py --healthcheck          Run daily health summary
    pinglet.py --list                 List registered tasks
    pinglet.py --test-alerts          Test notification system
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

# Add lib to path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib.alerts import send_critical, send_recovery, send_health_summary, test_alerts
from lib.logging import log, log_run_start, log_run_end, get_log_file_path
from lib.reliability import ReliabilityConfig, ReliabilityManager
from lib.state import (
    load_state,
    update_state_success,
    update_state_failure,
    load_all_states,
)


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
            )
            reliability.record_alert_sent()

    return exit_code


def run_healthcheck() -> int:
    """
    Run daily health check - summarizes all task states.

    Returns:
        Exit code (0 for healthy, 1 for issues)
    """
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

        # Calculate time since last run
        issue = None
        status = "OK"

        if state.last_run:
            last_run_dt = datetime.fromisoformat(state.last_run)
            hours_since = (now - last_run_dt).total_seconds() / 3600
            last_run_str = last_run_dt.strftime("%I:%M %p")

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
            last_run_str = "Never"
            status = "UNKNOWN"
            issue = "Task has never run"
            all_healthy = False

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
            task_summaries.append({
                "name": display_name,
                "last_run": "Never",
                "status": "UNKNOWN",
                "runs_today": 0,
                "issue": "No state file",
            })
            all_healthy = False

    # Send summary
    send_health_summary(task_summaries, healthy=all_healthy)

    log(f"Health check complete: {'All healthy' if all_healthy else 'Issues found'}", "healthcheck")
    return 0 if all_healthy else 1


def list_tasks() -> None:
    """List all registered tasks."""
    config = load_config()
    tasks = config.get("tasks", {})

    print("\nRegistered Tasks:")
    print("-" * 60)

    for task_name, task_config in tasks.items():
        display_name = task_config.get("name", task_name)
        command = task_config.get("command", "N/A")
        timeout = task_config.get("timeout", 300)

        # Load state
        state = load_state(task_name)
        last_run = state.last_run or "Never"
        status = state.last_status

        print(f"\n  {task_name}:")
        print(f"    Name: {display_name}")
        print(f"    Command: {command}")
        print(f"    Timeout: {timeout}s")
        print(f"    Last Run: {last_run}")
        print(f"    Status: {status}")
        if state.consecutive_failures > 0:
            print(f"    Consecutive Failures: {state.consecutive_failures}")

    print()


def main():
    parser = argparse.ArgumentParser(
        description="Pinglet - Universal task wrapper that guarantees no silent failures"
    )
    parser.add_argument("--task", "-t", help="Run a registered task")
    parser.add_argument("--healthcheck", "-H", action="store_true", help="Run daily health summary")
    parser.add_argument("--list", "-l", action="store_true", help="List registered tasks")
    parser.add_argument("--test-alerts", action="store_true", help="Test notification system")

    args = parser.parse_args()

    if args.task:
        sys.exit(run_task(args.task))
    elif args.healthcheck:
        sys.exit(run_healthcheck())
    elif args.list:
        list_tasks()
    elif args.test_alerts:
        print("Testing alerts...")
        results = test_alerts()
        print(f"Slack: {'OK' if results['slack'] else 'FAILED'}")
        print(f"macOS: {'OK' if results['macos'] else 'FAILED'}")
        sys.exit(0 if all(results.values()) else 1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
