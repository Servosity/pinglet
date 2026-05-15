"""Tests for the 15-minute alert grace period.

Covers:
- TaskState.first_failure_at is stamped on the first failure of a streak only
- TaskState.alerted_during_streak reflects whether send_critical fired for the streak
- ReliabilityManager.should_alert() suppresses with reason `grace_period_active`
  when the streak is younger than alert_grace_minutes
- ReliabilityManager.is_recovery() returns True only when the streak actually alerted
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.reliability import ReliabilityConfig, ReliabilityManager
from lib.state import (
    load_state,
    update_state_failure,
    update_state_success,
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    with patch("lib.state.STATE_DIR", state_dir):
        yield state_dir


class TestStreakTracking:
    def test_first_failure_stamps_first_failure_at(self):
        state = update_state_failure("task1", "boom", 0.5)
        assert state.first_failure_at is not None
        assert state.consecutive_failures == 1
        # Sanity: the timestamp parses
        datetime.fromisoformat(state.first_failure_at)

    def test_subsequent_failures_preserve_first_failure_at(self):
        first = update_state_failure("task1", "boom", 0.5)
        original_stamp = first.first_failure_at

        second = update_state_failure("task1", "boom again", 0.5)
        assert second.consecutive_failures == 2
        assert second.first_failure_at == original_stamp

    def test_success_clears_first_failure_at(self):
        update_state_failure("task1", "boom", 0.5)
        state = update_state_success("task1", 0.1)
        assert state.first_failure_at is None

    def test_new_streak_after_success_gets_fresh_stamp_and_clears_alerted_flag(self):
        # Streak 1 — fail, send alert, recover
        update_state_failure("task1", "boom", 0.5)
        cfg = ReliabilityConfig(alert_threshold=1, alert_grace_minutes=0)
        mgr = ReliabilityManager("task1", cfg)
        mgr.record_alert_sent()

        recovered = update_state_success("task1", 0.1)
        assert recovered.alerted_during_streak is True  # carry-over for is_recovery

        # Streak 2 starts — alerted flag must reset, new stamp must be set
        new_streak = update_state_failure("task1", "boom2", 0.5)
        assert new_streak.alerted_during_streak is False
        assert new_streak.first_failure_at is not None


class TestShouldAlertGracePeriod:
    def test_within_grace_period_suppresses_alert(self):
        # Three failures (clears threshold) but streak is brand new
        for _ in range(3):
            update_state_failure("task1", "boom", 0.1)

        cfg = ReliabilityConfig(alert_threshold=3, alert_grace_minutes=15)
        mgr = ReliabilityManager("task1", cfg)
        should_alert, reason = mgr.should_alert()
        assert should_alert is False
        assert "grace_period_active" in reason

    def test_after_grace_period_allows_alert(self):
        for _ in range(3):
            update_state_failure("task1", "boom", 0.1)

        # Backdate the streak start to 20 minutes ago
        state = load_state("task1")
        state.first_failure_at = (datetime.now() - timedelta(minutes=20)).isoformat()
        from lib.state import save_state
        save_state(state)

        cfg = ReliabilityConfig(alert_threshold=3, alert_grace_minutes=15)
        mgr = ReliabilityManager("task1", cfg)
        should_alert, reason = mgr.should_alert()
        assert should_alert is True

    def test_grace_zero_disables_gate(self):
        for _ in range(3):
            update_state_failure("task1", "boom", 0.1)

        cfg = ReliabilityConfig(alert_threshold=3, alert_grace_minutes=0)
        mgr = ReliabilityManager("task1", cfg)
        should_alert, _ = mgr.should_alert()
        assert should_alert is True

    def test_threshold_check_still_applies_before_grace(self):
        # One failure — below threshold; should return below_threshold, not grace
        update_state_failure("task1", "boom", 0.1)
        cfg = ReliabilityConfig(alert_threshold=3, alert_grace_minutes=15)
        mgr = ReliabilityManager("task1", cfg)
        should_alert, reason = mgr.should_alert()
        assert should_alert is False
        assert "below_threshold" in reason


class TestIsRecoveryStreakScoped:
    def test_recovery_suppressed_when_streak_never_alerted(self):
        # Short blip: fail then succeed, no alert ever sent
        update_state_failure("task1", "boom", 0.1)
        update_state_success("task1", 0.1)

        cfg = ReliabilityConfig()
        mgr = ReliabilityManager("task1", cfg)
        assert mgr.is_recovery() is False

    def test_recovery_fires_when_streak_alerted(self):
        update_state_failure("task1", "boom", 0.1)
        cfg = ReliabilityConfig(alert_threshold=1, alert_grace_minutes=0)
        mgr = ReliabilityManager("task1", cfg)
        mgr.record_alert_sent()

        update_state_success("task1", 0.1)
        # Fresh manager to pick up current state
        mgr2 = ReliabilityManager("task1", cfg)
        assert mgr2.is_recovery() is True

    def test_recovery_not_carried_into_next_streak(self):
        # Streak 1 alerts and recovers
        update_state_failure("task1", "boom", 0.1)
        cfg = ReliabilityConfig(alert_threshold=1, alert_grace_minutes=0)
        mgr = ReliabilityManager("task1", cfg)
        mgr.record_alert_sent()
        update_state_success("task1", 0.1)

        # Streak 2: brief blip with no alert — should NOT recover
        update_state_failure("task1", "blip", 0.1)
        update_state_success("task1", 0.1)
        mgr2 = ReliabilityManager("task1", cfg)
        assert mgr2.is_recovery() is False


class TestConfigPlumbing:
    def test_grace_minutes_default_is_15(self):
        cfg = ReliabilityConfig()
        assert cfg.alert_grace_minutes == 15

    def test_grace_minutes_from_global_config(self):
        cfg = ReliabilityConfig.from_config(
            {"alert": {"grace_minutes": 7}}, {}
        )
        assert cfg.alert_grace_minutes == 7

    def test_grace_minutes_task_override(self):
        cfg = ReliabilityConfig.from_config(
            {"alert": {"grace_minutes": 7}},
            {"alert": {"grace_minutes": 30}},
        )
        assert cfg.alert_grace_minutes == 30
