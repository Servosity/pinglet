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

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib.alerts import test_alerts
from lib.pinglet_runtime import run_task, run_healthcheck, run_now, ignore_task, run_heartbeat_command
from lib.pinglet_launch import install_heartbeat, uninstall_heartbeat, list_tasks, _check_monitoring_agents

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
    # on_failure callback
    if args.on_failure_command:
        on_failure = {"command": args.on_failure_command}
        if args.on_failure_prompt:
            on_failure["args"] = ["-p", args.on_failure_prompt]
        if args.on_failure_timeout:
            on_failure["timeout"] = args.on_failure_timeout
        if args.on_failure_max_turns:
            on_failure["max_turns"] = args.on_failure_max_turns
        config["on_failure"] = on_failure
    # on_diagnose callback
    if args.on_diagnose_command:
        on_diagnose = {"command": args.on_diagnose_command}
        if args.on_diagnose_prompt:
            on_diagnose["args"] = ["-p", args.on_diagnose_prompt]
        if args.on_diagnose_timeout:
            on_diagnose["timeout"] = args.on_diagnose_timeout
        if args.on_diagnose_max_turns:
            on_diagnose["max_turns"] = args.on_diagnose_max_turns
        config["on_diagnose"] = on_diagnose
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
  --heartbeat                Check for missed pinglets (3-tier recovery cascade)
  --status                   System status JSON (agent-parseable)
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
  --on-failure-command CMD   Command to run on failure (e.g. 'codex')
  --on-failure-prompt TEXT   Prompt for on_failure (supports {template_vars})
  --on-failure-timeout SECS  Timeout for on_failure callback (default: 180)
  --on-failure-max-turns N   Max turns for on_failure LLM (default: 5)
  --on-diagnose-command CMD  Command for anomaly diagnosis (e.g. 'codex')
  --on-diagnose-prompt TEXT  Prompt for on_diagnose (supports {template_vars})
  --on-diagnose-timeout SECS Timeout for on_diagnose callback (default: 300)
  --on-diagnose-max-turns N  Max turns for on_diagnose LLM (default: 10)

AGENT RUNNERS:
  config.yaml: agent_runners.primary/secondary/tertiary
  Slot order controls fallback; legacy agent_runners.providers still works.

SCHEDULE SYNTAX:
  every 1h                   Every hour (StartInterval)
  every 30m                  Every 30 minutes
  every 3600s                Every 3600 seconds
  daily 7:00                 Daily at 7:00 AM
  daily 7:00,19:00           Daily at 7 AM and 7 PM
  hourly :22                 Every hour at :22 past
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


def run_status() -> int:
    """Output structured JSON status for the entire system.

    Designed for agent consumption: any LLM or script can parse this
    to understand pinglet health at a glance.
    """
    from lib.task_manager import get_system_status
    status = get_system_status()
    print(json.dumps(status, indent=2))
    return 0 if status.get("ok", False) else 1

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

    # on_failure callback flags (for --task-add and --task-edit)
    parser.add_argument("--on-failure-command", help="Command to run on failure (e.g. 'codex')")
    parser.add_argument("--on-failure-prompt", help="Prompt for on_failure command (supports {template_vars})")
    parser.add_argument("--on-failure-timeout", type=int, help="Timeout for on_failure callback (default: 180)")
    parser.add_argument("--on-failure-max-turns", type=int, help="Max turns for on_failure (default: 5)")

    # on_diagnose callback flags (for --task-add and --task-edit)
    parser.add_argument("--on-diagnose-command", help="Command for anomaly diagnosis (e.g. 'codex')")
    parser.add_argument("--on-diagnose-prompt", help="Prompt for on_diagnose (supports {template_vars})")
    parser.add_argument("--on-diagnose-timeout", type=int, help="Timeout for on_diagnose callback (default: 300)")
    parser.add_argument("--on-diagnose-max-turns", type=int, help="Max turns for on_diagnose (default: 10)")

    # Status command
    parser.add_argument("--status", action="store_true", help="System status JSON (agent-parseable)")

    args = parser.parse_args()

    # Help / no args
    if args.help or len(sys.argv) == 1:
        print(HELP_TEXT)
        sys.exit(0)

    # Status command (run before self-check to avoid noise)
    if args.status:
        sys.exit(run_status())

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
