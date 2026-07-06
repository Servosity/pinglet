"""Unified notification system for Pinglet."""
import os
import subprocess
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Load environment variables
PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# Configuration (loaded from environment)
SLACK_USER_TOKEN = os.environ.get("SLACK_USER_TOKEN", "")
DEFAULT_SLACK_CHANNEL = os.environ.get("SLACK_DEFAULT_CHANNEL", "")

# Notification icon
NOTIFICATION_ICON = PROJECT_ROOT / "assets" / "pinglet-icon-512.png"
def send_missed_task_notification(
    task_name: str,
    display_name: str,
    hours_overdue: float,
    threshold: int
) -> bool:
    """
    Send macOS notification with Run/Ignore actions for a missed task.

    Args:
        task_name: Task identifier for commands
        display_name: Human-readable task name
        hours_overdue: Hours past threshold
        threshold: Expected interval in hours

    Returns:
        True on success, False on failure
    """
    pinglet_script = PROJECT_ROOT / "pinglet.py"

    # Build commands that open Terminal and execute
    run_command = f'open -a Terminal "uv run python {pinglet_script} --run-now {task_name}"'
    ignore_command = f'open -a Terminal "uv run python {pinglet_script} --ignore {task_name}"'

    total_hours = hours_overdue + threshold
    message = f"{display_name} last ran {total_hours:.1f}h ago (threshold: {threshold}h)"

    try:
        # Try terminal-notifier with actions
        cmd = [
            "terminal-notifier",
            "-title", "Pinglet: Missed Task",
            "-message", message,
            "-actions", "Run,Ignore",
            "-execute", run_command,
            "-sound", "default",
            "-timeout", "0",  # Don't auto-dismiss
        ]

        # Add custom app icon if it exists
        if NOTIFICATION_ICON.exists():
            cmd.extend(["-appIcon", str(NOTIFICATION_ICON)])

        result = subprocess.run(cmd, capture_output=True, timeout=5)

        if result.returncode == 0:
            print(f"ALERT | Missed task notification sent for {task_name}")
            return True

        # Fallback to AppleScript dialog with buttons
        print(f"ALERT | terminal-notifier failed, trying AppleScript dialog")
        return _send_missed_task_dialog(task_name, display_name, hours_overdue, threshold)

    except FileNotFoundError:
        print("ALERT | terminal-notifier not available, trying AppleScript")
        return _send_missed_task_dialog(task_name, display_name, hours_overdue, threshold)
    except Exception as e:
        print(f"ALERT | Missed task notification error: {e}")
        return False


def _send_missed_task_dialog(
    task_name: str,
    display_name: str,
    hours_overdue: float,
    threshold: int
) -> bool:
    """
    Send AppleScript dialog with Run/Ignore buttons as fallback.

    Args:
        task_name: Task identifier for commands
        display_name: Human-readable task name
        hours_overdue: Hours past threshold
        threshold: Expected interval in hours

    Returns:
        True on success, False on failure
    """
    pinglet_script = PROJECT_ROOT / "pinglet.py"

    total_hours = hours_overdue + threshold

    script = f'''
    set dialogResult to display dialog "Task '{display_name}' hasn't run in {total_hours:.1f} hours (threshold: {threshold}h)." ¬
        buttons {{"Ignore", "Run Now"}} default button "Run Now" ¬
        with title "Pinglet: Missed Task" ¬
        giving up after 300

    if button returned of dialogResult is "Run Now" then
        do shell script "open -a Terminal \\"uv run python {pinglet_script} --run-now {task_name}\\""
    else if button returned of dialogResult is "Ignore" then
        do shell script "open -a Terminal \\"uv run python {pinglet_script} --ignore {task_name}\\""
    end if
    '''

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=310,  # Slightly longer than dialog timeout
        )
        return result.returncode == 0
    except Exception as e:
        print(f"ALERT | AppleScript dialog error: {e}")
        return False
