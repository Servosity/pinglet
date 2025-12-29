"""Task state management for Pinglet."""
import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# Resolve project root
PROJECT_ROOT = Path(__file__).parent.parent
STATE_DIR = PROJECT_ROOT / "state"


def ensure_state_dir() -> None:
    """Ensure state directory exists."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class TaskState:
    """State for a single task."""
    task_name: str
    last_run: Optional[str] = None  # ISO timestamp
    last_status: str = "never_run"  # success | failed | timeout | never_run
    last_error: Optional[str] = None
    last_duration_seconds: float = 0.0
    consecutive_failures: int = 0
    total_runs: int = 0
    total_failures: int = 0
    runs_today: int = 0
    runs_today_date: Optional[str] = None  # YYYY-MM-DD to reset counter

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskState":
        """Create from dictionary."""
        return cls(**data)


def get_state_file(task_name: str) -> Path:
    """Get the path to a task's state file."""
    ensure_state_dir()
    return STATE_DIR / f"{task_name}.json"


def load_state(task_name: str) -> TaskState:
    """
    Load state for a task.

    Args:
        task_name: Name of the task

    Returns:
        TaskState object (new if file doesn't exist)
    """
    state_file = get_state_file(task_name)

    if not state_file.exists():
        return TaskState(task_name=task_name)

    try:
        with open(state_file, "r") as f:
            data = json.load(f)
        return TaskState.from_dict(data)
    except Exception as e:
        print(f"STATE | Error loading state for {task_name}: {e}")
        return TaskState(task_name=task_name)


def save_state(state: TaskState) -> None:
    """
    Save state for a task.

    Args:
        state: TaskState object to save
    """
    state_file = get_state_file(state.task_name)

    try:
        # Write to temp file first for atomicity
        temp_file = state_file.with_suffix(".tmp")
        with open(temp_file, "w") as f:
            json.dump(state.to_dict(), f, indent=2)

        # Atomic rename
        temp_file.rename(state_file)
    except Exception as e:
        print(f"STATE | Error saving state for {state.task_name}: {e}")


def update_state_success(task_name: str, duration: float) -> TaskState:
    """
    Update state after successful run.

    Args:
        task_name: Name of the task
        duration: Run duration in seconds

    Returns:
        Updated TaskState
    """
    state = load_state(task_name)
    today = datetime.now().strftime("%Y-%m-%d")

    state.last_run = datetime.now().isoformat()
    state.last_status = "success"
    state.last_error = None
    state.last_duration_seconds = duration
    state.consecutive_failures = 0
    state.total_runs += 1

    # Update runs_today counter
    if state.runs_today_date == today:
        state.runs_today += 1
    else:
        state.runs_today = 1
        state.runs_today_date = today

    save_state(state)
    return state


def update_state_failure(task_name: str, error: str, duration: float, timeout: bool = False) -> TaskState:
    """
    Update state after failed run.

    Args:
        task_name: Name of the task
        error: Error message
        duration: Run duration in seconds
        timeout: Whether this was a timeout

    Returns:
        Updated TaskState
    """
    state = load_state(task_name)
    today = datetime.now().strftime("%Y-%m-%d")

    state.last_run = datetime.now().isoformat()
    state.last_status = "timeout" if timeout else "failed"
    state.last_error = error[:1000] if len(error) > 1000 else error  # Truncate long errors
    state.last_duration_seconds = duration
    state.consecutive_failures += 1
    state.total_runs += 1
    state.total_failures += 1

    # Update runs_today counter
    if state.runs_today_date == today:
        state.runs_today += 1
    else:
        state.runs_today = 1
        state.runs_today_date = today

    save_state(state)
    return state


def load_all_states() -> list:
    """
    Load all task states from state directory.

    Returns:
        List of TaskState objects
    """
    ensure_state_dir()
    states = []

    for state_file in STATE_DIR.glob("*.json"):
        task_name = state_file.stem
        states.append(load_state(task_name))

    return states
