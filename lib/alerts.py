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

# Configuration
SLACK_USER_TOKEN = os.environ.get("SLACK_USER_TOKEN", "")
DEFAULT_SLACK_CHANNEL = "DXXXXXXXXXX"  # Damien's DM


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


def send_macos_notification(title: str, message: str, sound: str = "default") -> bool:
    """
    Send a macOS notification via terminal-notifier (preferred) or osascript.

    Args:
        title: Notification title
        message: Notification body
        sound: Sound name (default: "default")

    Returns:
        True on success, False on failure (non-critical)
    """
    try:
        # Try terminal-notifier first (more reliable, has its own notification permissions)
        result = subprocess.run(
            ["terminal-notifier", "-title", title, "-message", message, "-sound", sound],
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

    # Also send macOS notification
    send_macos_notification(
        f"Pinglet: {task_name} Failed",
        error[:100] if len(error) > 100 else error
    )

    print(f"ALERT | Critical alert sent for {task_name}: {error[:50]}...")


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
        send_macos_notification(
            "Pinglet Health Alert",
            f"{failed_count} task(s) need attention"
        )


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

    # Test macOS
    macos_ok = send_macos_notification(
        "Pinglet Test",
        "If you see this, macOS alerts are working."
    )

    return {"slack": slack_ok, "macos": macos_ok}
