"""
Task Queue for Pinglet.

Provides an ephemeral (memory-only) queue for sequential task execution
with configurable gaps between tasks. The queue is not persisted - if the
process dies, the queue is lost and tasks will be re-detected on next heartbeat.
"""
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from lib.logging import log


class TaskQueue:
    """
    Ephemeral task queue for sequential execution.

    The queue is memory-only and not persisted to disk.
    Duplicate tasks are silently ignored (logged).
    """

    def __init__(self):
        self._queue: deque = deque()
        self._seen: set = set()

    def add(self, task_name: str) -> bool:
        """
        Add a task to the queue.

        Args:
            task_name: Name of the task to queue

        Returns:
            True if added, False if duplicate
        """
        if task_name in self._seen:
            log(f"Ignoring duplicate task in queue: {task_name}", "queue")
            return False

        self._queue.append(task_name)
        self._seen.add(task_name)
        log(f"Added task to queue: {task_name}", "queue")
        return True

    def get_next(self) -> Optional[str]:
        """
        Get and remove the next task from the queue.

        Returns:
            Task name, or None if queue is empty
        """
        if not self._queue:
            return None
        return self._queue.popleft()

    def peek(self) -> Optional[str]:
        """
        Peek at the next task without removing it.

        Returns:
            Task name, or None if queue is empty
        """
        if not self._queue:
            return None
        return self._queue[0]

    def is_empty(self) -> bool:
        """Check if the queue is empty."""
        return len(self._queue) == 0

    def clear(self) -> None:
        """Clear all tasks from the queue."""
        self._queue.clear()
        self._seen.clear()

    def __len__(self) -> int:
        """Return the number of tasks in the queue."""
        return len(self._queue)

    def __contains__(self, task_name: str) -> bool:
        """Check if a task is in the queue."""
        return task_name in self._seen


@dataclass
class TaskResult:
    """Result of a single task execution."""
    task_name: str
    success: bool
    exit_code: int = 0
    error: Optional[str] = None


class QueueExecutor:
    """
    Executes tasks from a queue sequentially with configurable gaps.

    Continues execution even if individual tasks fail.
    """

    def __init__(
        self,
        queue: TaskQueue,
        run_task_fn: Callable[[str], int],
        gap_seconds: int = 30
    ):
        """
        Initialize the queue executor.

        Args:
            queue: TaskQueue to execute from
            run_task_fn: Function that runs a task, returns exit code
            gap_seconds: Seconds to wait between tasks (default 30)
        """
        self.queue = queue
        self.run_task_fn = run_task_fn
        self.gap_seconds = gap_seconds

    def execute_all(self) -> Dict[str, Dict]:
        """
        Execute all tasks in the queue sequentially.

        Continues on failure. Waits gap_seconds between tasks.

        Returns:
            Dict mapping task names to result dicts with 'success' and 'exit_code'
        """
        results = {}
        first_task = True

        while not self.queue.is_empty():
            task_name = self.queue.get_next()

            if not first_task and self.gap_seconds > 0:
                log(f"Waiting {self.gap_seconds}s before next task", "queue")
                time.sleep(self.gap_seconds)

            first_task = False

            log(f"Executing queued task: {task_name}", "queue")

            try:
                exit_code = self.run_task_fn(task_name)
                success = exit_code == 0

                results[task_name] = {
                    "success": success,
                    "exit_code": exit_code,
                }

                if success:
                    log(f"Task completed successfully: {task_name}", "queue")
                else:
                    log(f"Task failed: {task_name} (exit code {exit_code})", "queue")

            except Exception as e:
                log(f"Task execution error: {task_name} - {e}", "queue")
                results[task_name] = {
                    "success": False,
                    "exit_code": 1,
                    "error": str(e),
                }

        log(f"Queue execution complete. Processed {len(results)} tasks.", "queue")
        return results

    def execute_one(self) -> Optional[TaskResult]:
        """
        Execute just the next task in the queue.

        Returns:
            TaskResult or None if queue is empty
        """
        if self.queue.is_empty():
            return None

        task_name = self.queue.get_next()

        try:
            exit_code = self.run_task_fn(task_name)
            return TaskResult(
                task_name=task_name,
                success=exit_code == 0,
                exit_code=exit_code,
            )
        except Exception as e:
            return TaskResult(
                task_name=task_name,
                success=False,
                exit_code=1,
                error=str(e),
            )
