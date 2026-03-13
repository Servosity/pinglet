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


def get_client() -> WebClient:
    """Get Slack WebClient with user token."""
    if not SLACK_USER_TOKEN:
        return None
    return WebClient(token=SLACK_USER_TOKEN)


def send_slack_message(message: str, channel_id: str = None) -> bool:
    """
    Send a message to Slack.

    Args:
        message: Message text to send (supports mrkdwn)
        channel_id: Channel to post to (default: Damien's DM)

    Returns:
        True if sent successfully, False otherwise
    """
    channel_id = channel_id or DEFAULT_SLACK_CHANNEL

    try:
        client = get_client()
        if not client:
            print(f"ALERT | Cannot send Slack alert: no token configured")
            return False

        response = client.chat_postMessage(
            channel=channel_id,
            text=message,
            mrkdwn=True,
        )
        return response.get("ok", False)

    except SlackApiError as e:
        print(f"ALERT | Slack API error: {e}")
        return False
    except Exception as e:
        print(f"ALERT | Error sending Slack message: {e}")
        return False


def send_macos_notification(title: str, message: str, sound: str = "default", open_file: str = None) -> bool:
    """
    Send a macOS notification via terminal-notifier (preferred) or osascript.

    Args:
        title: Notification title
        message: Notification body
        sound: Sound name (default: "default")
        open_file: Optional file path to open when clicking "Show"

    Returns:
        True on success, False on failure (non-critical)
    """
    try:
        # Try terminal-notifier first (more reliable, has its own notification permissions)
        cmd = ["terminal-notifier", "-title", title, "-message", message, "-sound", sound]

        # Add custom app icon if it exists
        if NOTIFICATION_ICON.exists():
            cmd.extend(["-appIcon", str(NOTIFICATION_ICON)])

        # Add action to open file when clicking "Show"
        if open_file:
            cmd.extend(["-open", f"file://{open_file}"])

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=5,
        )

        if result.returncode == 0:
            return True

        # Fallback to osascript
        print(f"ALERT | terminal-notifier failed, trying osascript")
        message_escaped = message.replace('"', '\\"')
        title_escaped = title.replace('"', '\\"')

        result = subprocess.run(
            [
                "osascript", "-e",
                f'display notification "{message_escaped}" with title "{title_escaped}"'
            ],
            capture_output=True,
            timeout=5,
        )

        if result.returncode != 0:
            print(f"ALERT | macOS notification failed: {result.stderr.decode()}")
            return False
        return True

    except FileNotFoundError:
        print("ALERT | Neither terminal-notifier nor osascript available")
        return False
    except Exception as e:
        print(f"ALERT | macOS notification error: {e}")
        return False


def send_critical(task_name: str, error: str, details: dict = None, log_file: str = None, task_id: str = None) -> None:
    """
    Send critical failure alert - Slack + macOS notification.
    Called for any non-zero exit code.

    Args:
        task_name: Name of the failed task
        error: Error message
        details: Optional dict of additional details
        log_file: Path to log file for reference
        task_id: Task identifier for manual retry command
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    slack_message = f"""*Pinglet - Task Failed*

*Task:* {task_name}
*Timestamp:* {timestamp}
*Status:* FAILED

*Error:*
```
{error[:500] if len(error) > 500 else error}
```
"""

    if details:
        slack_message += "\n*Details:*"
        for key, value in details.items():
            slack_message += f"\n• {key}: {value}"

    # Add actionable commands
    slack_message += f"""
*Actions:*
• Retry now: `cd ~/Documents/Dev/pinglet && ./venv/bin/python pinglet.py --task {task_id or 'TASK_NAME'}`
• View logs: `tail -100 {log_file or '~/Documents/Dev/pinglet/logs/pinglet.log'}`
• Check all tasks: `./venv/bin/python pinglet.py --list`
"""

    # Send to Slack
    send_slack_message(slack_message)

    # Also send macOS notification with more context
    # Truncate error for notification but include actionable info
    short_error = error[:80] if len(error) > 80 else error
    notify_message = f"{short_error}\n\nClick Show to view logs."
    send_macos_notification(
        f"Pinglet: {task_name} Failed",
        notify_message,
        open_file=log_file,
    )

    print(f"ALERT | Critical alert sent for {task_name}: {error[:50]}...")


def send_recovery(task_name: str, previous_failures: int, details: dict = None) -> None:
    """
    Send recovery notification when task succeeds after failures.

    Args:
        task_name: Name of the recovered task
        previous_failures: Number of consecutive failures before recovery
        details: Optional additional details
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    slack_message = f"""*Pinglet - Task Recovered*

*Task:* {task_name}
*Timestamp:* {timestamp}
*Status:* RESOLVED

Task has recovered after {previous_failures} consecutive failure(s).
"""

    if details:
        slack_message += "\n*Details:*"
        for key, value in details.items():
            slack_message += f"\n• {key}: {value}"

    send_slack_message(slack_message)
    send_macos_notification(
        f"Pinglet: {task_name} Recovered",
        f"Task recovered after {previous_failures} failure(s)",
    )

    print(f"ALERT | Recovery notification sent for {task_name}")


def send_warning(task_name: str, message: str, details: dict = None) -> None:
    """
    Send warning for degraded runs (partial success).

    Args:
        task_name: Name of the task
        message: Warning message
        details: Optional dict of additional details
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    slack_message = f"""*Pinglet - Task Warning*

*Task:* {task_name}
*Timestamp:* {timestamp}
*Status:* DEGRADED

*Issue:*
{message}
"""

    if details:
        slack_message += "\n*Details:*"
        for key, value in details.items():
            slack_message += f"\n• {key}: {value}"

    send_slack_message(slack_message)
    print(f"ALERT | Warning sent for {task_name}: {message[:50]}...")


def send_health_summary(tasks: list, healthy: bool = True) -> None:
    """
    Send daily health summary to Slack.

    Args:
        tasks: List of task status dicts with keys:
               name, last_run, status, runs_today, issue (optional)
        healthy: True if all tasks are healthy
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_file = PROJECT_ROOT / "logs" / "pinglet.log"

    if healthy:
        header = "*All Systems Healthy*"
    else:
        failed_count = sum(1 for t in tasks if t.get("issue") and t.get("issue") != "-")
        header = f"*ALERT: {failed_count} task(s) require attention*"

    slack_message = f"""*Pinglet Daily Health Check*
{header}

*Timestamp:* {timestamp}

"""

    # Build task table
    slack_message += "| Task | Last Run | Status | Issue |\n"
    slack_message += "|------|----------|--------|-------|\n"

    for task in tasks:
        name = task.get("name", "Unknown")
        last_run = task.get("last_run", "Never")
        status = task.get("status", "UNKNOWN")
        issue = task.get("issue", "-")
        slack_message += f"| {name} | {last_run} | {status} | {issue} |\n"

    # Add actionable section if unhealthy
    if not healthy:
        slack_message += f"""
*Actions:*
• Check status: `cd ~/Documents/Dev/pinglet && ./venv/bin/python pinglet.py --list`
• Run manually: `./venv/bin/python pinglet.py --task <task_name>`
• View logs: `tail -50 {log_file}`
"""

    send_slack_message(slack_message)

    # Also send macOS notification if unhealthy
    if not healthy:
        # Build task names list for notification
        failing_tasks = [t.get("name") for t in tasks if t.get("issue") and t.get("issue") != "-"]
        task_list = ", ".join(failing_tasks[:3])  # Show first 3
        if len(failing_tasks) > 3:
            task_list += f" +{len(failing_tasks) - 3} more"

        notify_message = f"{failed_count} task(s): {task_list}\n\nClick Show to view logs."
        send_macos_notification(
            "Pinglet Health Alert",
            notify_message,
            open_file=str(log_file),
        )


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
    pinglet_path = PROJECT_ROOT / "venv" / "bin" / "python"
    pinglet_script = PROJECT_ROOT / "pinglet.py"

    # Build commands that open Terminal and execute
    run_command = f'open -a Terminal "{pinglet_path} {pinglet_script} --run-now {task_name}"'
    ignore_command = f'open -a Terminal "{pinglet_path} {pinglet_script} --ignore {task_name}"'

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
    pinglet_path = PROJECT_ROOT / "venv" / "bin" / "python"
    pinglet_script = PROJECT_ROOT / "pinglet.py"

    total_hours = hours_overdue + threshold

    script = f'''
    set dialogResult to display dialog "Task '{display_name}' hasn't run in {total_hours:.1f} hours (threshold: {threshold}h)." ¬
        buttons {{"Ignore", "Run Now"}} default button "Run Now" ¬
        with title "Pinglet: Missed Task" ¬
        giving up after 300

    if button returned of dialogResult is "Run Now" then
        do shell script "open -a Terminal \\"{pinglet_path} {pinglet_script} --run-now {task_name}\\""
    else if button returned of dialogResult is "Ignore" then
        do shell script "open -a Terminal \\"{pinglet_path} {pinglet_script} --ignore {task_name}\\""
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


def send_missed_task_slack(
    task_name: str,
    display_name: str,
    hours_overdue: float,
    threshold: int,
    last_run: str
) -> bool:
    """
    Send Slack notification for a missed task (informational only).

    Args:
        task_name: Task identifier
        display_name: Human-readable task name
        hours_overdue: Hours past threshold
        threshold: Expected interval in hours
        last_run: Last run timestamp string

    Returns:
        True on success, False on failure
    """
    total_hours = hours_overdue + threshold

    message = f"""*Pinglet: Missed Task*
`{task_name}` hasn't run in {total_hours:.1f} hours (threshold: {threshold}h)
Last successful run: {last_run}

_Use macOS notification to Run or Ignore_"""

    return send_slack_message(message)


def send_success_notification(
    task_name: str,
    display_name: str,
    summary: str,
    config: dict
) -> bool:
    """
    Send silent success notification (notification center only, no sound/banner).

    Args:
        task_name: Task identifier
        display_name: Human-readable task name
        summary: Formatted output summary
        config: Full configuration dictionary

    Returns:
        True on success, False on failure
    """
    if not config.get("notifications", {}).get("on_success", False):
        return True  # Success notifications disabled

    # Silent notification (no sound)
    message = f"{display_name} completed\n{summary}" if summary else f"{display_name} completed"

    try:
        # Use terminal-notifier without sound for silent notification
        cmd = [
            "terminal-notifier",
            "-title", "Pinglet",
            "-message", message[:200],
            # No -sound flag = silent
        ]

        # Add custom app icon if it exists
        if NOTIFICATION_ICON.exists():
            cmd.extend(["-appIcon", str(NOTIFICATION_ICON)])

        result = subprocess.run(cmd, capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception as e:
        print(f"ALERT | Success notification error: {e}")
        return False


def send_manual_complete_notification(
    task_name: str,
    display_name: str,
    success: bool,
    config: dict,
    error: str = None
) -> bool:
    """
    Send silent notification for manual run completion.

    Args:
        task_name: Task identifier
        display_name: Human-readable task name
        success: Whether the task succeeded
        config: Full configuration dictionary
        error: Error message if failed

    Returns:
        True on success, False on failure
    """
    if success:
        message = f"{display_name} completed successfully"
    else:
        short_error = error[:50] if error else "Unknown error"
        message = f"{display_name} failed: {short_error}"

    try:
        cmd = [
            "terminal-notifier",
            "-title", "Pinglet",
            "-message", message,
            # No -sound flag = silent for success, add sound for failure
        ]

        # Add custom app icon if it exists
        if NOTIFICATION_ICON.exists():
            cmd.extend(["-appIcon", str(NOTIFICATION_ICON)])

        if not success:
            cmd.extend(["-sound", "default"])

        result = subprocess.run(cmd, capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception as e:
        print(f"ALERT | Manual complete notification error: {e}")
        return False


def send_critical_monitoring_alert(disabled_agents: list) -> bool:
    """Send urgent Slack alert when monitoring agents themselves are dead.

    This is a distinct, high-priority alert separate from regular task failures.
    Includes fix commands for each disabled agent.

    Args:
        disabled_agents: List of dicts with keys: task_id, label, exit_code, status

    Returns:
        True if alert sent successfully
    """
    if not disabled_agents:
        return True

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    agent_lines = []
    fix_lines = []
    for agent in disabled_agents:
        agent_lines.append(f"• `{agent['task_id']}` — {agent['status']} (exit {agent['exit_code']})")
        fix_lines.append(f"  ./venv/bin/python pinglet.py --task-enable {agent['task_id']}")

    slack_message = f"""*Pinglet: MONITORING DOWN*

*Timestamp:* {timestamp}
*Status:* CRITICAL — monitoring agents are dead

*Disabled agents:*
{chr(10).join(agent_lines)}

*Fix (run from ~/Documents/Dev/pinglet):*
```
{chr(10).join(fix_lines)}
```

_This means task failures may go undetected until agents are re-enabled._"""

    result = send_slack_message(slack_message)

    # Also send macOS notification
    agent_names = ", ".join(a["task_id"] for a in disabled_agents[:3])
    send_macos_notification(
        "Pinglet: MONITORING DOWN",
        f"Disabled agents: {agent_names}\nTask failures may go undetected!",
    )

    return result


def test_alerts() -> dict:
    """
    Test the notification system.

    Returns:
        Dict with test results: {"slack": bool, "macos": bool}
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Test Slack
    slack_ok = send_slack_message(
        f"*Pinglet Alert Test*\n\nTimestamp: {timestamp}\nThis is a test message. If you see this, Slack alerts are working."
    )

    # Test macOS with log file opening
    log_file = PROJECT_ROOT / "logs" / "pinglet.log"
    macos_ok = send_macos_notification(
        "Pinglet Test",
        "Click Show to open log file.\n\nIf you see this, macOS alerts are working.",
        open_file=str(log_file),
    )

    return {"slack": slack_ok, "macos": macos_ok}
