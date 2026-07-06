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


def send_critical_monitoring_alert(disabled_agents: list, llm_result: dict = None) -> bool:
    """Send urgent Slack alert when monitoring agents themselves are dead.

    This is a distinct, high-priority alert separate from regular task failures.
    Includes fix commands, auto-recovery status, and LLM diagnosis results.

    Args:
        disabled_agents: List of dicts with keys: task_id, label, exit_code, status
        llm_result: Optional dict with keys: attempted, success, log_file

    Returns:
        True if alert sent successfully
    """
    from lib.alerts import send_macos_notification, send_slack_message

    if not disabled_agents:
        return True

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    agent_lines = []
    fix_lines = []
    for agent in disabled_agents:
        agent_lines.append(f"\u2022 `{agent['task_id']}` \u2014 {agent['status']} (exit {agent['exit_code']})")
        fix_lines.append(f"  uv run python pinglet.py --task-enable {agent['task_id']}")

    slack_message = f"""*Pinglet: MONITORING DOWN*

*Timestamp:* {timestamp}
*Status:* CRITICAL \u2014 monitoring agents are dead

*Disabled agents:*
{chr(10).join(agent_lines)}
"""

    # LLM diagnosis status
    if llm_result and llm_result.get("attempted"):
        if llm_result.get("success"):
            slack_message += "*LLM Self-Diagnosis:* Invoked \u2713 (exit 0 \u2014 may be fixed, verify next cycle)\n"
        else:
            slack_message += "*LLM Self-Diagnosis:* Invoked \u2717 (failed \u2014 human intervention required)\n"
            if llm_result.get("log_file"):
                slack_message += f"  Output: `{llm_result['log_file']}`\n"
    else:
        slack_message += "*LLM Self-Diagnosis:* Auto-recovery + LLM both failed\n"

    slack_message += f"""
*Fix (run from {PROJECT_ROOT}):*
```
{chr(10).join(fix_lines)}
```

_This means task failures may go undetected until agents are re-enabled._
_System status: `uv run python pinglet.py --status`_"""

    result = send_slack_message(slack_message)

    # Also send macOS notification
    agent_names = ", ".join(a["task_id"] for a in disabled_agents[:3])
    send_macos_notification(
        "Pinglet: MONITORING DOWN",
        f"Disabled agents: {agent_names}\nTask failures may go undetected!",
    )

    return result
