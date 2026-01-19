"""Tests for heartbeat/missed task detection (Feature 1)."""
import json
import pytest
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class MockTaskState:
    """Mock task state for testing."""
    task_name: str
    last_run: str = None
    last_status: str = "never_run"
    consecutive_failures: int = 0


class TestMissedTaskDetection:
    """Tests for missed task detection logic."""

    def test_detect_missed_task_over_threshold(self, sample_config):
        """Test detecting a task that exceeds expected interval."""
        old_time = datetime.now() - timedelta(hours=20)  # 20 hours ago, threshold is 14

        mock_state = MockTaskState(
            task_name="uce",
            last_run=old_time.isoformat(),
            last_status="success",
        )

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False):
            from lib.heartbeat import detect_missed_tasks

            missed = detect_missed_tasks(sample_config)

            assert len(missed) >= 1
            uce_missed = [m for m in missed if m["task_name"] == "uce"]
            assert len(uce_missed) == 1
            assert uce_missed[0]["hours_overdue"] > 5  # 20 - 14 = 6

    def test_detect_missed_task_under_threshold(self, sample_config):
        """Test task within expected interval is not flagged."""
        recent_time = datetime.now() - timedelta(hours=10)  # 10 hours ago, threshold is 14

        mock_state = MockTaskState(
            task_name="uce",
            last_run=recent_time.isoformat(),
            last_status="success",
        )

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False):
            from lib.heartbeat import detect_missed_tasks

            missed = detect_missed_tasks(sample_config)

            # uce should not be missed
            uce_missed = [m for m in missed if m["task_name"] == "uce"]
            assert len(uce_missed) == 0

    def test_detect_missed_task_no_expected_interval(self):
        """Test task without expected_interval is skipped."""
        config = {
            "healthcheck": {
                "expected_intervals": {}  # No intervals defined
            },
            "tasks": {
                "some-task": {"name": "Some Task"}
            }
        }

        with patch("lib.heartbeat._load_state"), \
             patch("lib.heartbeat._is_ignored", return_value=False):
            from lib.heartbeat import detect_missed_tasks

            missed = detect_missed_tasks(config)
            assert len(missed) == 0

    def test_detect_missed_task_never_run(self, sample_config):
        """Test task that has never run is detected as missed."""
        mock_state = MockTaskState(
            task_name="uce",
            last_run=None,  # Never run
        )

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False):
            from lib.heartbeat import detect_missed_tasks

            missed = detect_missed_tasks(sample_config)

            uce_missed = [m for m in missed if m["task_name"] == "uce"]
            assert len(uce_missed) == 1
            assert uce_missed[0]["never_run"] is True

    def test_filter_ignored_tasks(self, sample_config):
        """Test that ignored tasks are filtered out."""
        old_time = datetime.now() - timedelta(hours=20)

        mock_state = MockTaskState(
            task_name="uce",
            last_run=old_time.isoformat(),
        )

        def mock_is_ignored(task_name):
            return task_name == "uce"  # Only uce is ignored

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", side_effect=mock_is_ignored):
            from lib.heartbeat import detect_missed_tasks

            missed = detect_missed_tasks(sample_config)

            uce_missed = [m for m in missed if m["task_name"] == "uce"]
            assert len(uce_missed) == 0


class TestHeartbeatExecution:
    """Tests for heartbeat execution flow."""

    def test_heartbeat_with_no_missed_tasks(self, sample_config):
        """Test heartbeat when all tasks are up to date."""
        recent_time = datetime.now() - timedelta(hours=1)

        mock_state = MockTaskState(
            task_name="any",
            last_run=recent_time.isoformat(),
            last_status="success",
        )

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False), \
             patch("lib.heartbeat._send_missed_task_notification") as mock_notify:
            from lib.heartbeat import run_heartbeat

            result = run_heartbeat(sample_config, wake_delay=0)

            assert result["missed_count"] == 0
            mock_notify.assert_not_called()

    def test_heartbeat_sends_notifications(self, sample_config):
        """Test heartbeat sends notifications for missed tasks."""
        old_time = datetime.now() - timedelta(hours=20)

        mock_state = MockTaskState(
            task_name="uce",
            last_run=old_time.isoformat(),
        )

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False), \
             patch("lib.heartbeat._send_missed_task_notification") as mock_notify, \
             patch("lib.heartbeat._send_slack_message") as mock_slack:
            from lib.heartbeat import run_heartbeat

            result = run_heartbeat(sample_config, wake_delay=0)

            assert result["missed_count"] >= 1
            mock_notify.assert_called()

    def test_heartbeat_wake_delay(self, sample_config):
        """Test heartbeat respects wake delay."""
        old_time = datetime.now() - timedelta(hours=20)

        mock_state = MockTaskState(
            task_name="uce",
            last_run=old_time.isoformat(),
        )

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False), \
             patch("lib.heartbeat._send_missed_task_notification"), \
             patch("lib.heartbeat._send_slack_message"), \
             patch("time.sleep") as mock_sleep:
            from lib.heartbeat import run_heartbeat

            run_heartbeat(sample_config, wake_delay=30)

            # Should have slept for wake delay
            mock_sleep.assert_called_once_with(30)


class TestStaleNotificationHandling:
    """Tests for handling stale notifications (task already ran)."""

    def test_run_now_skips_if_task_already_ran(self, sample_config):
        """Test --run-now skips if task no longer qualifies as missed."""
        # Task ran recently (no longer missed)
        recent_time = datetime.now() - timedelta(hours=1)

        mock_state = MockTaskState(
            task_name="uce",
            last_run=recent_time.isoformat(),
        )

        with patch("lib.heartbeat._load_state", return_value=mock_state):
            from lib.heartbeat import should_run_task

            should_run, reason = should_run_task("uce", sample_config)

            assert should_run is False
            assert "already ran" in reason.lower()

    def test_run_now_proceeds_if_still_missed(self, sample_config):
        """Test --run-now proceeds if task is still missed."""
        # Task is still old
        old_time = datetime.now() - timedelta(hours=20)

        mock_state = MockTaskState(
            task_name="uce",
            last_run=old_time.isoformat(),
        )

        with patch("lib.heartbeat._load_state", return_value=mock_state):
            from lib.heartbeat import should_run_task

            should_run, reason = should_run_task("uce", sample_config)

            assert should_run is True
