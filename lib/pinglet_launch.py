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
LAUNCHAGENTS_DIR = PROJECT_ROOT / "launchagents"
USER_LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
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

from lib.pinglet_runtime import load_config

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
        <string>uv</string>
        <string>run</string>
        <string>python</string>
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

    # Strip MACL xattrs from heartbeat log to prevent launchd exit 78
    # (macOS Sequoia adds com.apple.macl, which blocks launchd writes)
    from lib.task_manager import _strip_macl_from_logs
    _strip_macl_from_logs("heartbeat")

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
            print(f"    uv run python pinglet.py --task-enable {agent}")
        print()
    else:
        print()


# =============================================================================
# Task Management CLI Handlers
# =============================================================================
def _check_monitoring_agents() -> list:
    """Check if monitoring agents (healthcheck, heartbeat) are dead.

    Returns list of dead agent task_ids. Prints warning to stderr.
    """
    from lib.task_manager import get_launchd_status, MONITORING_AGENTS

    dead = []
    for agent_id in MONITORING_AGENTS:
        status = get_launchd_status(agent_id)
        # Note: "failed" (non-zero exit) is NOT included — monitoring agents
        # exit 1 when they find issues (missed tasks, disabled agents), which
        # is normal operation, not a dead agent.
        if status["disabled"] or status["status"] in ("not_loaded", "not_installed"):
            dead.append(agent_id)

    if dead:
        print(
            f"\033[1;31mWARNING: Monitoring agent(s) DOWN: {', '.join(dead)}\033[0m",
            file=sys.stderr,
        )
        print(
            f"  Fix: uv run python pinglet.py --task-enable <agent>",
            file=sys.stderr,
        )

    return dead


def _maybe_send_monitoring_alert(dead_agents: list) -> None:
    """Monitoring alerts now handled by heartbeat's 3-tier cascade.

    This function is kept for backward compatibility with the
    'every CLI invocation' self-check, but only logs a warning.
    The actual alerting + recovery happens in run_heartbeat().
    """
    if not dead_agents:
        return
    # Just log — the heartbeat cascade handles thresholds, auto-recovery,
    # LLM diagnosis, and human alerting with learning-based suppression.
    from lib.logging import log
    log(f"Monitoring self-check: {len(dead_agents)} dead agent(s) detected. Next heartbeat will handle recovery.", "pinglet")
