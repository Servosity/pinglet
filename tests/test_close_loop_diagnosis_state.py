"""Tests for close-the-loop system: on_failure callbacks, self-diagnosis, learning loop, notifications."""
import json
import pytest
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class MockTaskState:
    """Mock task state for testing."""
    task_name: str
    last_run: str = None
    last_status: str = "never_run"
    last_error: str = None
    consecutive_failures: int = 0
    total_runs: int = 0
class TestTaskDiagnosisState:
    """Tests for task diagnosis state tracking."""

    def test_record_task_detection_creates_entry(self):
        """First detection creates entry with consecutive_detections=1."""
        from lib.heartbeat import _record_task_detection

        state = {}
        state = _record_task_detection("clawdbot-sync", state, "stale (24h overdue)")

        assert "clawdbot-sync" in state
        entry = state["clawdbot-sync"]
        assert entry["consecutive_detections"] == 1
        assert entry["detected_problem"] == "stale (24h overdue)"
        assert entry["diagnosis_attempted"] is False

    def test_record_task_detection_increments(self):
        """Subsequent detections increment counter."""
        from lib.heartbeat import _record_task_detection

        state = {}
        state = _record_task_detection("clawdbot-sync", state, "stale")
        state = _record_task_detection("clawdbot-sync", state, "stale")
        state = _record_task_detection("clawdbot-sync", state, "stale")

        assert state["clawdbot-sync"]["consecutive_detections"] == 3

    def test_clear_task_diagnosis_soft_clears_entry(self):
        """Soft clear preserves entry but resets counters and sets fixed_at."""
        from lib.heartbeat import _record_task_detection, _clear_task_diagnosis

        state = {}
        state = _record_task_detection("clawdbot-sync", state, "stale (2h overdue)")
        state = _record_task_detection("clawdbot-sync", state, "stale (2h overdue)")
        assert state["clawdbot-sync"]["consecutive_detections"] == 2

        state = _clear_task_diagnosis("clawdbot-sync", state)

        # Entry still exists (soft clear)
        assert "clawdbot-sync" in state
        entry = state["clawdbot-sync"]
        # Counters reset
        assert entry["consecutive_detections"] == 0
        assert entry["diagnosis_attempted"] is False
        assert entry["last_diagnosis"] is None
        # Fixed timestamp set
        assert entry["fixed_at"] is not None
        # Fix summary captured from detected_problem
        assert entry["last_fix_summary"] == "stale (2h overdue)"

    def test_recurring_fix_increments_on_redetection(self):
        """Re-detecting within 24h of fix increments recurring_fix_failures."""
        from lib.heartbeat import _record_task_detection, _clear_task_diagnosis

        state = {}
        state = _record_task_detection("test-task", state, "stale")
        state = _clear_task_diagnosis("test-task", state)
        assert state["test-task"]["recurring_fix_failures"] == 0

        # Re-detect immediately (within 24h)
        state = _record_task_detection("test-task", state, "stale")
        assert state["test-task"]["recurring_fix_failures"] == 1

        # Fix and break again
        state = _clear_task_diagnosis("test-task", state)
        state = _record_task_detection("test-task", state, "stale")
        assert state["test-task"]["recurring_fix_failures"] == 2

    def test_recurring_fix_skips_after_24h(self):
        """Re-detecting after 24h of fix does NOT increment recurring_fix_failures."""
        from lib.heartbeat import _record_task_detection, _clear_task_diagnosis

        state = {}
        state = _record_task_detection("test-task", state, "stale")
        state = _clear_task_diagnosis("test-task", state)

        # Backdate fixed_at to 25h ago
        state["test-task"]["fixed_at"] = (
            datetime.now() - timedelta(hours=25)
        ).isoformat()

        state = _record_task_detection("test-task", state, "stale")
        assert state["test-task"]["recurring_fix_failures"] == 0

    def test_should_attempt_below_threshold(self):
        """Should not attempt diagnosis below TASK_DIAGNOSIS_MIN_DETECTIONS."""
        from lib.heartbeat import _should_attempt_task_diagnosis, _record_task_detection

        state = {}
        state = _record_task_detection("test-task", state, "stale")
        # Only 1 detection — below threshold of 2
        assert _should_attempt_task_diagnosis("test-task", state) is False

    def test_should_attempt_at_threshold(self):
        """Should attempt diagnosis at TASK_DIAGNOSIS_MIN_DETECTIONS."""
        from lib.heartbeat import _should_attempt_task_diagnosis, _record_task_detection

        state = {}
        state = _record_task_detection("test-task", state, "stale")
        state = _record_task_detection("test-task", state, "stale")
        # 2 detections — meets threshold
        assert _should_attempt_task_diagnosis("test-task", state) is True

    def test_should_attempt_respects_cooldown(self):
        """Should not re-attempt within TASK_DIAGNOSIS_COOLDOWN_HOURS."""
        from lib.heartbeat import _should_attempt_task_diagnosis

        state = {
            "test-task": {
                "consecutive_detections": 5,
                "last_diagnosis": datetime.now().isoformat(),
                "detected_problem": "stale",
            },
        }
        # Just attempted — cooldown active
        assert _should_attempt_task_diagnosis("test-task", state) is False

    def test_should_attempt_after_cooldown(self):
        """Should re-attempt after cooldown expires."""
        from lib.heartbeat import _should_attempt_task_diagnosis

        state = {
            "test-task": {
                "consecutive_detections": 5,
                "last_diagnosis": (datetime.now() - timedelta(hours=7)).isoformat(),
                "detected_problem": "stale",
            },
        }
        # 7 hours ago > 6h cooldown
        assert _should_attempt_task_diagnosis("test-task", state) is True

    def test_exponential_backoff_with_recurring_failures(self):
        """Cooldown doubles for each recurring fix failure."""
        from lib.heartbeat import _should_attempt_task_diagnosis

        # recurring=2 → cooldown = 6 * 2^2 = 24h
        state = {
            "test-task": {
                "consecutive_detections": 5,
                "recurring_fix_failures": 2,
                "last_diagnosis": (datetime.now() - timedelta(hours=12)).isoformat(),
                "detected_problem": "stale",
            },
        }
        # 12h < 24h cooldown → should NOT attempt
        assert _should_attempt_task_diagnosis("test-task", state) is False

        # 25h ago > 24h cooldown → should attempt
        state["test-task"]["last_diagnosis"] = (
            datetime.now() - timedelta(hours=25)
        ).isoformat()
        assert _should_attempt_task_diagnosis("test-task", state) is True

    def test_chronic_threshold_stops_llm(self):
        """At chronic threshold, LLM diagnosis is blocked entirely."""
        from lib.heartbeat import _should_attempt_task_diagnosis

        state = {
            "test-task": {
                "consecutive_detections": 10,
                "recurring_fix_failures": 5,  # At chronic threshold
                "last_diagnosis": (datetime.now() - timedelta(hours=999)).isoformat(),
                "detected_problem": "stale",
            },
        }
        # Should NOT attempt even though cooldown is well expired
        assert _should_attempt_task_diagnosis("test-task", state) is False
