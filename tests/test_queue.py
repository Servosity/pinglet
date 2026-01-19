"""Tests for Task Queue system (Feature 2)."""
import pytest
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestTaskQueue:
    """Tests for the ephemeral task queue."""

    def test_queue_creation(self):
        """Test creating an empty queue."""
        from lib.queue import TaskQueue

        queue = TaskQueue()
        assert queue.is_empty()
        assert len(queue) == 0

    def test_queue_add_task(self):
        """Test adding a task to the queue."""
        from lib.queue import TaskQueue

        queue = TaskQueue()
        queue.add("uce")

        assert not queue.is_empty()
        assert len(queue) == 1
        assert "uce" in queue

    def test_queue_add_multiple_tasks(self):
        """Test adding multiple tasks."""
        from lib.queue import TaskQueue

        queue = TaskQueue()
        queue.add("uce")
        queue.add("git-sync")
        queue.add("claude-backup")

        assert len(queue) == 3

    def test_queue_duplicate_ignored(self):
        """Test duplicate tasks are ignored with logging."""
        from lib.queue import TaskQueue

        queue = TaskQueue()
        queue.add("uce")
        queue.add("uce")  # Duplicate

        assert len(queue) == 1

    def test_queue_get_next(self):
        """Test getting next task from queue."""
        from lib.queue import TaskQueue

        queue = TaskQueue()
        queue.add("uce")
        queue.add("git-sync")

        task = queue.get_next()
        assert task == "uce"
        assert len(queue) == 1

        task = queue.get_next()
        assert task == "git-sync"
        assert queue.is_empty()

    def test_queue_get_next_empty(self):
        """Test getting from empty queue returns None."""
        from lib.queue import TaskQueue

        queue = TaskQueue()
        assert queue.get_next() is None

    def test_queue_peek(self):
        """Test peeking at next task without removing."""
        from lib.queue import TaskQueue

        queue = TaskQueue()
        queue.add("uce")

        task = queue.peek()
        assert task == "uce"
        assert len(queue) == 1  # Still in queue

    def test_queue_clear(self):
        """Test clearing the queue."""
        from lib.queue import TaskQueue

        queue = TaskQueue()
        queue.add("uce")
        queue.add("git-sync")
        queue.clear()

        assert queue.is_empty()


class TestQueueExecution:
    """Tests for sequential queue execution."""

    def test_execute_queue_sequential(self):
        """Test tasks execute sequentially."""
        from lib.queue import TaskQueue, QueueExecutor

        queue = TaskQueue()
        queue.add("uce")
        queue.add("git-sync")

        execution_order = []

        def mock_run_task(task_name):
            execution_order.append(task_name)
            return 0  # Success

        with patch("time.sleep"):  # Skip delays in test
            executor = QueueExecutor(queue, run_task_fn=mock_run_task, gap_seconds=0)
            results = executor.execute_all()

        assert execution_order == ["uce", "git-sync"]
        assert results["uce"]["success"] is True
        assert results["git-sync"]["success"] is True

    def test_execute_queue_with_30s_gap(self):
        """Test 30 second gap between tasks."""
        from lib.queue import TaskQueue, QueueExecutor

        queue = TaskQueue()
        queue.add("uce")
        queue.add("git-sync")

        def mock_run_task(task_name):
            return 0

        with patch("time.sleep") as mock_sleep:
            executor = QueueExecutor(queue, run_task_fn=mock_run_task, gap_seconds=30)
            executor.execute_all()

        # Should sleep between tasks (not after last one)
        assert mock_sleep.call_count == 1
        mock_sleep.assert_called_with(30)

    def test_execute_queue_continues_on_failure(self):
        """Test queue continues executing after task failure."""
        from lib.queue import TaskQueue, QueueExecutor

        queue = TaskQueue()
        queue.add("uce")
        queue.add("git-sync")
        queue.add("claude-backup")

        execution_order = []

        def mock_run_task(task_name):
            execution_order.append(task_name)
            if task_name == "git-sync":
                return 1  # Failure
            return 0  # Success

        with patch("time.sleep"):
            executor = QueueExecutor(queue, run_task_fn=mock_run_task, gap_seconds=0)
            results = executor.execute_all()

        # All tasks should have executed
        assert execution_order == ["uce", "git-sync", "claude-backup"]
        assert results["uce"]["success"] is True
        assert results["git-sync"]["success"] is False
        assert results["claude-backup"]["success"] is True

    def test_execute_queue_failure_result(self):
        """Test failed task returns correct result."""
        from lib.queue import TaskQueue, QueueExecutor

        queue = TaskQueue()
        queue.add("git-sync")

        def mock_run_task(task_name):
            return 1  # Failure

        with patch("time.sleep"):
            executor = QueueExecutor(queue, run_task_fn=mock_run_task, gap_seconds=0)
            results = executor.execute_all()

        # Verify failure is recorded in results
        assert results["git-sync"]["success"] is False
        assert results["git-sync"]["exit_code"] == 1

    def test_execute_empty_queue(self):
        """Test executing empty queue does nothing."""
        from lib.queue import TaskQueue, QueueExecutor

        queue = TaskQueue()

        def mock_run_task(task_name):
            return 0

        executor = QueueExecutor(queue, run_task_fn=mock_run_task, gap_seconds=30)
        results = executor.execute_all()

        assert results == {}


class TestQueueEphemeralNature:
    """Tests verifying queue is ephemeral (not persisted)."""

    def test_queue_not_persisted(self):
        """Test queue is memory-only and not saved to disk."""
        from lib.queue import TaskQueue

        queue = TaskQueue()
        queue.add("uce")

        # Queue should have no file I/O
        # This is verified by the absence of file-related code
        assert not hasattr(queue, '_file_path')

    def test_new_queue_is_empty(self):
        """Test new queue instance starts empty."""
        from lib.queue import TaskQueue

        queue1 = TaskQueue()
        queue1.add("uce")

        queue2 = TaskQueue()  # New instance
        assert queue2.is_empty()
