"""Logging utilities for Pinglet."""
import os
from datetime import datetime
from pathlib import Path

# Resolve project root
PROJECT_ROOT = Path(__file__).parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"


def ensure_logs_dir() -> None:
    """Ensure logs directory exists."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def get_log_file_path() -> Path:
    """Get the path to today's log file."""
    ensure_logs_dir()
    return LOGS_DIR / "pinglet.log"


def log(message: str, task_name: str = None) -> None:
    """
    Log a message to stdout and the log file.

    Args:
        message: Message to log
        task_name: Optional task name for context
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = f"[{task_name}] " if task_name else ""
    formatted = f"{timestamp} | {prefix}{message}"

    # Print to stdout
    print(formatted)

    # Append to log file
    try:
        log_file = get_log_file_path()
        with open(log_file, "a") as f:
            f.write(formatted + "\n")
    except Exception as e:
        print(f"{timestamp} | LOG_ERROR | Failed to write to log file: {e}")


def log_run_start(task_name: str) -> None:
    """Log the start of a task run."""
    log("=" * 60, task_name)
    log("RUN_START", task_name)


def log_run_end(task_name: str, status: str, duration: float, details: dict = None) -> None:
    """
    Log the end of a task run.

    Args:
        task_name: Name of the task
        status: Status string (success/failed/timeout)
        duration: Duration in seconds
        details: Optional dict of additional details
    """
    detail_str = ""
    if details:
        detail_str = " | " + " | ".join(f"{k}={v}" for k, v in details.items())

    log(f"RUN_END | status={status} | duration={duration:.1f}s{detail_str}", task_name)
    log("=" * 60, task_name)
