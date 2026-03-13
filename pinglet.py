#!/usr/bin/env python3
"""
Pinglet - Universal task wrapper that guarantees no silent failures.

Run with --help or no arguments for full agent-friendly reference.
"""
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
PROJECT_ROOT = Path(__file__).parent
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

# Directories for LaunchAgents
LAUNCHAGENTS_DIR = PROJECT_ROOT / "launchagents"
USER_LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


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

    # After each task run, check if monitoring agents are dead
    # This is the "who watches the watchmen" safety net
    if success:
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


def install_heartbeat() -> int:
    """
    Install heartbeat LaunchAgent.

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    LAUNCHAGENTS_DIR.mkdir(parents=True, exist_ok=True)
    USER_LAUNCHAGENTS_DIR.mkdir(parents=True, exist_ok=True)

    plist_name = "com.pinglet.heartbeat.plist"
    plist_path = LAUNCHAGENTS_DIR / plist_name
    target_path = USER_LAUNCHAGENTS_DIR / plist_name

    # Generate plist content
    python_path = PROJECT_ROOT / "venv" / "bin" / "python"
    script_path = PROJECT_ROOT / "pinglet.py"
    log_path = PROJECT_ROOT / "logs" / "launchd-heartbeat.log"

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.pinglet.heartbeat</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>{script_path}</string>
        <string>--heartbeat</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{PROJECT_ROOT}</string>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
"""

    # Write plist file
    plist_path.write_text(plist_content)
    print(f"Generated plist: {plist_path}")

    # Copy to LaunchAgents
    shutil.copy(plist_path, target_path)
    print(f"Installed to: {target_path}")

    # Unload if already loaded (ignore errors)
    subprocess.run(
        ["launchctl", "unload", str(target_path)],
        capture_output=True,
    )

    # Load the agent
    result = subprocess.run(
        ["launchctl", "load", str(target_path)],
        capture_output=True,
    )

    if result.returncode == 0:
        print("Heartbeat LaunchAgent installed and loaded successfully!")
        return 0
    else:
        print(f"ERROR loading LaunchAgent: {result.stderr.decode()}")
        return 1


def uninstall_heartbeat() -> int:
    """
    Uninstall heartbeat LaunchAgent.

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    plist_name = "com.pinglet.heartbeat.plist"
    target_path = USER_LAUNCHAGENTS_DIR / plist_name

    if not target_path.exists():
        print("Heartbeat LaunchAgent is not installed.")
        return 0

    # Unload the agent
    result = subprocess.run(
        ["launchctl", "unload", str(target_path)],
        capture_output=True,
    )

    # Remove the plist file
    target_path.unlink()
    print(f"Removed: {target_path}")

    if result.returncode == 0:
        print("Heartbeat LaunchAgent uninstalled successfully!")
    else:
        print(f"Warning: unload returned error (may already be unloaded)")

    return 0


def list_tasks(json_mode: bool = False) -> None:
    """List all registered pinglets."""
    if json_mode:
        from lib.task_manager import list_tasks_json
        print(json.dumps(list_tasks_json(), indent=2))
        return

    from lib.task_manager import get_launchd_status

    config = load_config()
    tasks = config.get("tasks", {})

    print("\nRegistered Pinglets:")
    print("-" * 60)

    disabled_agents = []

    for task_name, task_config in tasks.items():
        display_name = task_config.get("name", task_name)
        command = task_config.get("command", "N/A")
        timeout = task_config.get("timeout", 300)

        # Load state
        state = load_state(task_name)
        last_run = state.last_run or "Never"
        status = state.last_status

        # Get launchd status
        launchd = get_launchd_status(task_name)
        launchd_str = launchd["status"].upper()
        if launchd["disabled"]:
            launchd_str = f"\033[1;31mDISABLED (exit 78)\033[0m"
            disabled_agents.append(task_name)
        elif launchd["status"] == "failed":
            launchd_str = f"\033[1;33mFAILED (exit {launchd['exit_code']})\033[0m"
        elif launchd["status"] == "running":
            launchd_str = f"\033[32mRUNNING\033[0m"

        print(f"\n  {task_name}:")
        print(f"    Name: {display_name}")
        print(f"    Command: {command}")
        print(f"    Timeout: {timeout}s")
        print(f"    Last Run: {last_run}")
        print(f"    Status: {status}")
        print(f"    LaunchAgent: {launchd_str}")
        if state.consecutive_failures > 0:
            print(f"    Consecutive Failures: {state.consecutive_failures}")

    # Warning banner if any agents are disabled
    if disabled_agents:
        print()
        print("\033[1;31m" + "=" * 60)
        print("  WARNING: DISABLED LaunchAgents detected!")
        print("=" * 60 + "\033[0m")
        print(f"  The following agents have exit code 78 (disabled by launchd):")
        for agent in disabled_agents:
            print(f"    - {agent}")
        print(f"\n  Fix: Re-enable each agent:")
        for agent in disabled_agents:
            print(f"    ./venv/bin/python pinglet.py --task-enable {agent}")
        print()
    else:
        print()


# =============================================================================
# Task Management CLI Handlers
# =============================================================================

def _build_task_config_from_args(args) -> dict:
    """Build a task config dict from CLI arguments."""
    config = {}
    if args.name:
        config["name"] = args.name
    if args.command:
        config["command"] = args.command
    if args.task_args:
        config["args"] = args.task_args
    if args.working_dir:
        config["working_dir"] = args.working_dir
    if args.timeout is not None:
        config["timeout"] = args.timeout
    if args.env:
        config["env"] = args.env
    if args.output_format or args.summary_template:
        config["output"] = {}
        if args.output_format:
            config["output"]["format"] = args.output_format
        if args.summary_template:
            config["output"]["summary_template"] = args.summary_template
    if args.failures_before_alert is not None:
        config["reliability"] = {"alert": {"consecutive_failures": args.failures_before_alert}}
    # Inline schedule
    if args.schedule_spec:
        config["schedule"] = args.schedule_spec
    return config


def handle_task_add(args) -> int:
    from lib.task_manager import add_task
    task_config = _build_task_config_from_args(args)

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "task_id": args.task_add, "config": task_config}, indent=2))
        return 0

    result = add_task(args.task_add, task_config)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else (2 if "already exists" in result.get("error", "") else 1)


def handle_task_edit(args) -> int:
    from lib.task_manager import edit_task
    updates = _build_task_config_from_args(args)

    if not updates:
        print(json.dumps({"ok": False, "error": "No fields to update. Use --name, --command, --timeout, etc."}))
        return 2

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "task_id": args.task_edit, "updates": updates}, indent=2))
        return 0

    result = edit_task(args.task_edit, updates)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def handle_task_remove(args) -> int:
    from lib.task_manager import remove_task

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "task_id": args.task_remove, "action": "would remove task + disable LaunchAgent if active"}))
        return 0

    result = remove_task(args.task_remove)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def handle_task_show(args) -> int:
    from lib.task_manager import show_task
    result = show_task(args.task_show)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def handle_task_logs(args) -> int:
    from lib.task_manager import get_task_logs
    task_id = args.task_logs[0]
    lines = int(args.task_logs[1]) if len(args.task_logs) > 1 else 50
    result = get_task_logs(task_id, lines)
    print(json.dumps(result, indent=2))
    return 0


def handle_task_enable(args) -> int:
    from lib.task_manager import enable_task

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "task_id": args.task_enable, "action": "would generate plist + load LaunchAgent"}))
        return 0

    result = enable_task(args.task_enable)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def handle_task_disable(args) -> int:
    from lib.task_manager import disable_task

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "task_id": args.task_disable, "action": "would unload LaunchAgent + remove plist"}))
        return 0

    result = disable_task(args.task_disable)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def handle_schedule(args) -> int:
    from lib.task_manager import set_schedule
    task_id, spec = args.schedule

    if args.dry_run:
        from lib.task_manager import parse_schedule, schedule_to_human
        try:
            parsed = parse_schedule(spec)
            print(json.dumps({"ok": True, "dry_run": True, "task_id": task_id, "schedule": spec, "parsed": schedule_to_human(parsed)}))
        except ValueError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2
        return 0

    result = set_schedule(task_id, spec)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


# =============================================================================
# Agent-Friendly Help
# =============================================================================

HELP_TEXT = """Pinglet — No silent failures

A "pinglet" is an individual scheduled task managed by this system.

PINGLET MANAGEMENT:
  --task-add ID              Add a new pinglet (requires --command)
  --task-edit ID             Edit a pinglet (only specified fields update)
  --task-remove ID           Remove a pinglet (auto-disables if scheduled)
  --task-show ID             Full pinglet details + state + recent logs (JSON)
  --task-logs ID [N]         Last N lines of pinglet logs (default: 50)
  --task-enable ID           Generate plist + load LaunchAgent
  --task-disable ID          Unload LaunchAgent + remove plist
  --list [--json]            List all pinglets (--json for structured output)
  --schedule ID SPEC         Set pinglet schedule (see syntax below)
  --dry-run                  Preview changes without modifying

EXECUTION:
  --task ID, -t ID           Run a pinglet now
  --run-now TASK             Run a missed pinglet immediately
  --ignore TASK              Mark missed pinglet as ignored
  --healthcheck, -H          Run daily health summary
  --heartbeat                Check for missed pinglets
  --test-alerts              Test Slack + macOS notifications
  --install-heartbeat        Install heartbeat LaunchAgent
  --uninstall-heartbeat      Uninstall heartbeat LaunchAgent

CONFIG FLAGS (use with --task-add or --task-edit):
  --command PATH             Executable path (required for add)
  --name TEXT                Display name (default: title-cased pinglet ID)
  --args ARG [ARG ...]       Command arguments
  --working-dir PATH         Working directory
  --timeout SECS             Timeout in seconds (default: 300)
  --env VAR [VAR ...]        Env vars to pass through
  --output-format text|json  Output format (default: text)
  --summary-template TEXT    Template for JSON output (e.g. "Processed {count} items")
  --failures-before-alert N  Consecutive failures before alerting (default: 3)
  --schedule-spec SPEC       Schedule (inline with add/edit, see syntax below)

SCHEDULE SYNTAX:
  every 1h                   Every hour (StartInterval)
  every 30m                  Every 30 minutes
  every 3600s                Every 3600 seconds
  daily 7:00                 Daily at 7:00 AM
  daily 7:00,19:00           Daily at 7 AM and 7 PM
  weekly mon 7:33            Weekly on Monday at 7:33 AM

OUTPUT:
  All management commands output JSON to stdout.
  Success: {"ok": true, "task_id": "...", ...}
  Error:   {"ok": false, "error": "..."}
  Exit codes: 0=success, 1=error, 2=validation error

EXAMPLES:
  # Add a pinglet with schedule
  pinglet.py --task-add my-task --command /usr/bin/python3 --args script.py --schedule-spec "daily 7:00"

  # Enable scheduling (generates plist + loads LaunchAgent)
  pinglet.py --task-enable my-task

  # Show full pinglet details including logs
  pinglet.py --task-show my-task

  # View recent pinglet logs
  pinglet.py --task-logs my-task 100

  # Edit timeout and reschedule
  pinglet.py --task-edit my-task --timeout 600
  pinglet.py --schedule my-task "every 2h"
  pinglet.py --task-enable my-task

  # List all pinglets as JSON
  pinglet.py --list --json

  # Remove a pinglet (auto-disables)
  pinglet.py --task-remove my-task

  # Dry-run any mutating command
  pinglet.py --task-add test --command /usr/bin/echo --dry-run
"""


def _check_monitoring_agents() -> list:
    """Check if monitoring agents (healthcheck, heartbeat) are dead.

    Returns list of dead agent task_ids. Prints warning to stderr.
    """
    from lib.task_manager import get_launchd_status, MONITORING_AGENTS

    dead = []
    for agent_id in MONITORING_AGENTS:
        status = get_launchd_status(agent_id)
        if status["disabled"] or status["status"] in ("failed", "not_loaded", "not_installed"):
            dead.append(agent_id)

    if dead:
        print(
            f"\033[1;31mWARNING: Monitoring agent(s) DOWN: {', '.join(dead)}\033[0m",
            file=sys.stderr,
        )
        print(
            f"  Fix: ./venv/bin/python pinglet.py --task-enable <agent>",
            file=sys.stderr,
        )

    return dead


def _maybe_send_monitoring_alert(dead_agents: list) -> None:
    """Send a debounced Slack alert if monitoring agents are dead.

    Only re-alerts after 24h or if the set of dead agents changed.
    Uses state/_monitoring_alert.json for debounce tracking.
    """
    if not dead_agents:
        return

    from lib.state import STATE_DIR
    alert_file = STATE_DIR / "_monitoring_alert.json"
    now = datetime.now()
    dead_set = sorted(dead_agents)

    # Check debounce
    if alert_file.exists():
        try:
            with open(alert_file, "r") as f:
                prev = json.load(f)
            prev_agents = sorted(prev.get("agents", []))
            prev_time = datetime.fromisoformat(prev.get("timestamp", "2000-01-01"))
            hours_since = (now - prev_time).total_seconds() / 3600

            # Skip if same set of dead agents and less than 24h since last alert
            if prev_agents == dead_set and hours_since < 24:
                return
        except (json.JSONDecodeError, ValueError, KeyError):
            pass  # Corrupted file, re-alert

    # Send alert
    from lib.alerts import send_critical_monitoring_alert
    disabled_info = [{"task_id": a, "label": f"com.pinglet.{a}", "exit_code": 78, "status": "disabled"} for a in dead_agents]
    send_critical_monitoring_alert(disabled_info)

    # Write debounce file
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(alert_file, "w") as f:
        json.dump({"agents": dead_set, "timestamp": now.isoformat()}, f)


def main():
    parser = argparse.ArgumentParser(
        description="Pinglet - Universal task wrapper that guarantees no silent failures",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_TEXT,
        add_help=False,
    )

    # Existing commands
    parser.add_argument("--help", "-h", action="store_true", help="Show help")
    parser.add_argument("--task", "-t", help="Run a registered task")
    parser.add_argument("--healthcheck", "-H", action="store_true", help="Run daily health summary")
    parser.add_argument("--list", "-l", action="store_true", help="List registered tasks")
    parser.add_argument("--test-alerts", action="store_true", help="Test notification system")
    parser.add_argument("--run-now", metavar="TASK", help="Run a missed task immediately")
    parser.add_argument("--ignore", metavar="TASK", help="Mark a missed task as ignored")
    parser.add_argument("--heartbeat", action="store_true", help="Run heartbeat check for missed tasks")
    parser.add_argument("--install-heartbeat", action="store_true", help="Install heartbeat LaunchAgent")
    parser.add_argument("--uninstall-heartbeat", action="store_true", help="Uninstall heartbeat LaunchAgent")

    # Task management commands
    parser.add_argument("--task-add", metavar="ID", help="Add a new task")
    parser.add_argument("--task-edit", metavar="ID", help="Edit an existing task")
    parser.add_argument("--task-remove", metavar="ID", help="Remove a task")
    parser.add_argument("--task-show", metavar="ID", help="Show task details (JSON)")
    parser.add_argument("--task-logs", nargs="+", metavar="ARG", help="Show task logs: ID [LINES]")
    parser.add_argument("--task-enable", metavar="ID", help="Enable task scheduling")
    parser.add_argument("--task-disable", metavar="ID", help="Disable task scheduling")
    parser.add_argument("--schedule", nargs=2, metavar=("ID", "SPEC"), help="Set task schedule")
    parser.add_argument("--json", action="store_true", help="JSON output (with --list)")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes")

    # Task config flags (for --task-add and --task-edit)
    parser.add_argument("--name", help="Task display name")
    parser.add_argument("--command", help="Command to execute")
    parser.add_argument("--args", nargs="*", dest="task_args", help="Command arguments")
    parser.add_argument("--working-dir", help="Working directory")
    parser.add_argument("--timeout", type=int, help="Timeout in seconds")
    parser.add_argument("--env", nargs="*", help="Environment variables to pass through")
    parser.add_argument("--output-format", choices=["text", "json"], help="Output format")
    parser.add_argument("--summary-template", help="Template for JSON output")
    parser.add_argument("--failures-before-alert", type=int, help="Failures before alert")
    parser.add_argument("--schedule-spec", help="Schedule spec (inline with add/edit)")

    args = parser.parse_args()

    # Help / no args
    if args.help or len(sys.argv) == 1:
        print(HELP_TEXT)
        sys.exit(0)

    # Lightweight self-check: warn if monitoring agents are dead
    # This runs on every CLI invocation so any surviving agent detects dead watchdogs
    _check_monitoring_agents()

    # Task management dispatch (before existing commands)
    if args.task_add:
        sys.exit(handle_task_add(args))
    elif args.task_edit:
        sys.exit(handle_task_edit(args))
    elif args.task_remove:
        sys.exit(handle_task_remove(args))
    elif args.task_show:
        sys.exit(handle_task_show(args))
    elif args.task_logs:
        sys.exit(handle_task_logs(args))
    elif args.task_enable:
        sys.exit(handle_task_enable(args))
    elif args.task_disable:
        sys.exit(handle_task_disable(args))
    elif args.schedule:
        sys.exit(handle_schedule(args))

    # Existing commands
    elif args.task:
        sys.exit(run_task(args.task))
    elif args.healthcheck:
        sys.exit(run_healthcheck())
    elif args.list:
        list_tasks(json_mode=args.json)
    elif args.test_alerts:
        print("Testing alerts...")
        results = test_alerts()
        print(f"Slack: {'OK' if results['slack'] else 'FAILED'}")
        print(f"macOS: {'OK' if results['macos'] else 'FAILED'}")
        sys.exit(0 if all(results.values()) else 1)
    elif args.run_now:
        sys.exit(run_now(args.run_now))
    elif args.ignore:
        sys.exit(ignore_task(args.ignore))
    elif args.heartbeat:
        sys.exit(run_heartbeat_command())
    elif args.install_heartbeat:
        sys.exit(install_heartbeat())
    elif args.uninstall_heartbeat:
        sys.exit(uninstall_heartbeat())
    else:
        print(HELP_TEXT)
        sys.exit(1)


if __name__ == "__main__":
    main()
