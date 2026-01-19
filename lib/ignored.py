"""
Ignored tasks management for Pinglet.

Tracks tasks that user has chosen to ignore via notification action.
Ignored tasks are suppressed from heartbeat notifications until next scheduled run.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

# Resolve project root
PROJECT_ROOT = Path(__file__).parent.parent
STATE_DIR = PROJECT_ROOT / "state"
IGNORED_FILE = STATE_DIR / "ignored.json"


def ensure_state_dir() -> None:
    """Ensure state directory exists."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_ignored() -> dict:
    """
    Load ignored tasks state.

    Returns:
        Dictionary of ignored tasks with their metadata
    """
    if not IGNORED_FILE.exists():
        return {}

    try:
        return json.loads(IGNORED_FILE.read_text())
    except (json.JSONDecodeError, IOError):
        return {}


def save_ignored(data: dict) -> None:
    """
    Save ignored tasks state.

    Args:
        data: Dictionary of ignored tasks to save
    """
    ensure_state_dir()

    try:
        # Write to temp file first for atomicity
        temp_file = IGNORED_FILE.with_suffix(".tmp")
        temp_file.write_text(json.dumps(data, indent=2))
        temp_file.rename(IGNORED_FILE)
    except IOError as e:
        print(f"IGNORED | Error saving ignored state: {e}")


def ignore_task(
    task_name: str,
    last_run: str,
    threshold: int,
    hours_overdue: float
) -> None:
    """
    Mark a task as ignored.

    Args:
        task_name: Name of the task to ignore
        last_run: ISO timestamp of last run when ignored
        threshold: Expected interval threshold in hours
        hours_overdue: Hours past threshold when ignored
    """
    ignored = load_ignored()
    ignored[task_name] = {
        "ignored_at": datetime.now().isoformat(),
        "reason": "Manual skip via notification",
        "last_run_at_ignore": last_run,
        "threshold_hours": threshold,
        "hours_overdue": round(hours_overdue, 1),
    }
    save_ignored(ignored)


def clear_ignored(task_name: str) -> None:
    """
    Clear ignored status for a task.

    Called when:
    - Task completes a successful scheduled run
    - User manually runs `pinglet --run-now <task>`

    Args:
        task_name: Name of the task to clear
    """
    ignored = load_ignored()
    if task_name in ignored:
        del ignored[task_name]
        save_ignored(ignored)


def is_ignored(task_name: str) -> bool:
    """
    Check if task is currently ignored.

    Args:
        task_name: Name of the task to check

    Returns:
        True if task is ignored, False otherwise
    """
    ignored = load_ignored()
    return task_name in ignored and ignored[task_name] is not None


def get_ignored_info(task_name: str) -> Optional[dict]:
    """
    Get ignored info for a task.

    Args:
        task_name: Name of the task

    Returns:
        Dictionary with ignored info, or None if not ignored
    """
    ignored = load_ignored()
    return ignored.get(task_name)
