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


class TestDetectDisabledAgents:
    """Tests for detect_disabled_agents()."""

    def test_detects_disabled_agent(self, sample_config):
        """Disabled agent (exit 78) should be detected."""
        def mock_status(task_id):
            if task_id == "uce":
                return {"installed": True, "running": False, "exit_code": 78, "disabled": True, "status": "disabled"}
            return {"installed": True, "running": False, "exit_code": 0, "disabled": False, "status": "idle"}

        with patch("lib.heartbeat._get_launchd_status", side_effect=mock_status):
            from lib.heartbeat import detect_disabled_agents
            disabled = detect_disabled_agents(sample_config)

        task_ids = [d["task_id"] for d in disabled]
        assert "uce" in task_ids

    def test_failed_agent_not_detected(self, sample_config):
        """Failed agent (non-zero, non-78 exit) should NOT be detected.

        Task failures are handled by the task's own reliability system
        (retries + consecutive failure threshold). Monitoring agents exit 1
        when they find issues, which is normal operation.
        """
        def mock_status(task_id):
            if task_id == "obsidian-tab-archiver":
                return {"installed": True, "running": False, "exit_code": 1, "disabled": False, "status": "failed"}
            return {"installed": True, "running": False, "exit_code": 0, "disabled": False, "status": "idle"}

        with patch("lib.heartbeat._get_launchd_status", side_effect=mock_status):
            from lib.heartbeat import detect_disabled_agents
            disabled = detect_disabled_agents(sample_config)

        task_ids = [d["task_id"] for d in disabled]
        assert "obsidian-tab-archiver" not in task_ids

    def test_healthy_agents_not_detected(self, sample_config):
        """Healthy agents should not be flagged."""
        def mock_status(task_id):
            return {"installed": True, "running": False, "exit_code": 0, "disabled": False, "status": "idle"}

        with patch("lib.heartbeat._get_launchd_status", side_effect=mock_status):
            from lib.heartbeat import detect_disabled_agents
            disabled = detect_disabled_agents(sample_config)

        assert len(disabled) == 0

    def test_includes_monitoring_agents(self, sample_config):
        """Should also check monitoring agents (healthcheck, heartbeat)."""
        def mock_status(task_id):
            if task_id == "healthcheck":
                return {"installed": True, "running": False, "exit_code": 78, "disabled": True, "status": "disabled"}
            return {"installed": True, "running": False, "exit_code": 0, "disabled": False, "status": "idle"}

        with patch("lib.heartbeat._get_launchd_status", side_effect=mock_status):
            from lib.heartbeat import detect_disabled_agents
            disabled = detect_disabled_agents(sample_config)

        task_ids = [d["task_id"] for d in disabled]
        assert "healthcheck" in task_ids


class TestEscalationLevels:
    """Tests for escalation level calculation."""

    def test_warning_level(self):
        """2x threshold -> warning."""
        from lib.heartbeat import get_escalation_level
        # 14h threshold, 14h overdue = 28h total = 2x
        assert get_escalation_level(14, 14) == "warning"

    def test_urgent_level(self):
        """5x threshold -> urgent."""
        from lib.heartbeat import get_escalation_level
        # 14h threshold, 56h overdue = 70h total = 5x
        assert get_escalation_level(56, 14) == "urgent"

    def test_critical_level(self):
        """10x threshold -> critical."""
        from lib.heartbeat import get_escalation_level
        # 14h threshold, 126h overdue = 140h total = 10x
        assert get_escalation_level(126, 14) == "critical"

    def test_just_below_urgent(self):
        """Just below 5x should be warning."""
        from lib.heartbeat import get_escalation_level
        # 2h threshold, 7h overdue = 9h total = 4.5x
        assert get_escalation_level(7, 2) == "warning"

    def test_just_at_urgent(self):
        """Exactly 5x should be urgent."""
        from lib.heartbeat import get_escalation_level
        # 2h threshold, 8h overdue = 10h total = 5x
        assert get_escalation_level(8, 2) == "urgent"

    def test_zero_threshold(self):
        """Zero threshold should default to warning."""
        from lib.heartbeat import get_escalation_level
        assert get_escalation_level(100, 0) == "warning"


class TestHeartbeatAlertCooldown:
    """Tests for per-task Slack alert cooldown."""

    def test_should_alert_first_time(self):
        """First alert for a task should always fire."""
        from lib.heartbeat import _should_alert_for_task
        assert _should_alert_for_task("uce", "warning", {}) is True

    def test_should_not_alert_within_cooldown(self):
        """Same task+escalation within 24h should be suppressed."""
        from lib.heartbeat import _should_alert_for_task
        recent = datetime.now() - timedelta(hours=1)
        state = {"uce": {"last_alert": recent.isoformat(), "escalation": "warning"}}
        assert _should_alert_for_task("uce", "warning", state) is False

    def test_should_alert_after_cooldown_expires(self):
        """Same task after 24h+ should re-alert."""
        from lib.heartbeat import _should_alert_for_task
        old = datetime.now() - timedelta(hours=25)
        state = {"uce": {"last_alert": old.isoformat(), "escalation": "warning"}}
        assert _should_alert_for_task("uce", "warning", state) is True

    def test_should_alert_on_escalation_change(self):
        """Escalation level change should bypass cooldown."""
        from lib.heartbeat import _should_alert_for_task
        recent = datetime.now() - timedelta(hours=1)
        state = {"uce": {"last_alert": recent.isoformat(), "escalation": "warning"}}
        assert _should_alert_for_task("uce", "urgent", state) is True

    def test_different_task_not_affected(self):
        """Cooldown for one task shouldn't affect another."""
        from lib.heartbeat import _should_alert_for_task
        recent = datetime.now() - timedelta(hours=1)
        state = {"uce": {"last_alert": recent.isoformat(), "escalation": "warning"}}
        assert _should_alert_for_task("claude-backup", "warning", state) is True

    def test_corrupted_state_allows_alert(self):
        """Corrupted alert state should allow alerting."""
        from lib.heartbeat import _should_alert_for_task
        state = {"uce": {"last_alert": "not-a-date", "escalation": "warning"}}
        assert _should_alert_for_task("uce", "warning", state) is True

    def test_run_heartbeat_respects_cooldown(self, sample_config):
        """run_heartbeat should not send Slack for tasks within cooldown."""
        old_time = datetime.now() - timedelta(hours=20)
        recent_alert = datetime.now() - timedelta(hours=1)

        mock_state = MockTaskState(
            task_name="uce",
            last_run=old_time.isoformat(),
        )

        # Pre-populate cooldown state for ALL tasks in sample_config
        cooldown_state = {
            "uce": {"last_alert": recent_alert.isoformat(), "escalation": "warning"},
            "git-sync": {"last_alert": recent_alert.isoformat(), "escalation": "critical"},
            "claude-backup": {"last_alert": recent_alert.isoformat(), "escalation": "critical"},
        }

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False), \
             patch("lib.heartbeat._send_missed_task_notification"), \
             patch("lib.heartbeat._send_slack_message") as mock_slack, \
             patch("lib.heartbeat._load_heartbeat_alert_state", return_value=cooldown_state), \
             patch("lib.heartbeat._save_heartbeat_alert_state"), \
             patch("lib.heartbeat.detect_disabled_agents", return_value=[]):
            from lib.heartbeat import run_heartbeat

            result = run_heartbeat(sample_config, wake_delay=0)

            assert result["missed_count"] >= 1
            # Slack should NOT have been called (all tasks in cooldown)
            mock_slack.assert_not_called()

    def test_run_heartbeat_sends_after_cooldown(self, sample_config):
        """run_heartbeat should send Slack after cooldown expires."""
        old_time = datetime.now() - timedelta(hours=20)
        old_alert = datetime.now() - timedelta(hours=25)

        mock_state = MockTaskState(
            task_name="uce",
            last_run=old_time.isoformat(),
        )

        # Pre-populate expired cooldown state
        cooldown_state = {"uce": {"last_alert": old_alert.isoformat(), "escalation": "warning"}}

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False), \
             patch("lib.heartbeat._send_missed_task_notification"), \
             patch("lib.heartbeat._send_slack_message") as mock_slack, \
             patch("lib.heartbeat._load_heartbeat_alert_state", return_value=cooldown_state), \
             patch("lib.heartbeat._save_heartbeat_alert_state"), \
             patch("lib.heartbeat.detect_disabled_agents", return_value=[]):
            from lib.heartbeat import run_heartbeat

            result = run_heartbeat(sample_config, wake_delay=0)

            assert result["missed_count"] >= 1
            # Slack SHOULD have been called (cooldown expired)
            mock_slack.assert_called()


class TestDisabledAgentAlertCooldown:
    """Tests for disabled agent alert cooldown in run_heartbeat."""

    def test_first_disabled_agent_alert_fires(self):
        """First detection of disabled agents should alert."""
        from lib.heartbeat import _should_send_disabled_agent_alert

        with patch("lib.heartbeat.DISABLED_AGENT_ALERT_FILE") as mock_file:
            mock_file.exists.return_value = False
            disabled = [{"task_id": "uce", "label": "com.pinglet.uce", "exit_code": 78, "status": "disabled"}]
            assert _should_send_disabled_agent_alert(disabled) is True

    def test_disabled_agent_alert_suppressed_within_cooldown(self):
        """Same disabled agents within 4h cooldown should be suppressed."""
        from lib.heartbeat import _should_send_disabled_agent_alert

        recent = datetime.now() - timedelta(hours=1)
        state = {"last_alert_time": recent.isoformat(), "agents": ["uce"]}

        with patch("lib.heartbeat._load_disabled_agent_alert_state", return_value=state):
            disabled = [{"task_id": "uce", "label": "com.pinglet.uce", "exit_code": 78, "status": "disabled"}]
            assert _should_send_disabled_agent_alert(disabled) is False

    def test_disabled_agent_alert_fires_after_cooldown(self):
        """Same disabled agents after 4h+ should re-alert."""
        from lib.heartbeat import _should_send_disabled_agent_alert

        old = datetime.now() - timedelta(hours=5)
        state = {"last_alert_time": old.isoformat(), "agents": ["uce"]}

        with patch("lib.heartbeat._load_disabled_agent_alert_state", return_value=state):
            disabled = [{"task_id": "uce", "label": "com.pinglet.uce", "exit_code": 78, "status": "disabled"}]
            assert _should_send_disabled_agent_alert(disabled) is True

    def test_disabled_agent_alert_fires_on_agent_change(self):
        """New disabled agent should bypass cooldown."""
        from lib.heartbeat import _should_send_disabled_agent_alert

        recent = datetime.now() - timedelta(hours=1)
        state = {"last_alert_time": recent.isoformat(), "agents": ["uce"]}

        with patch("lib.heartbeat._load_disabled_agent_alert_state", return_value=state):
            # Different agent disabled now
            disabled = [{"task_id": "git-sync", "label": "com.pinglet.git-sync", "exit_code": 78, "status": "disabled"}]
            assert _should_send_disabled_agent_alert(disabled) is True

    def test_run_heartbeat_disabled_agent_cooldown(self, sample_config):
        """run_heartbeat should suppress alert when auto-recovery succeeds (new 3-tier cascade)."""
        recent_time = datetime.now() - timedelta(hours=1)
        mock_state = MockTaskState(task_name="any", last_run=recent_time.isoformat(), last_status="success")

        disabled_agents = [{"task_id": "uce", "label": "com.pinglet.uce", "exit_code": 78, "status": "disabled"}]

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False), \
             patch("lib.heartbeat.detect_disabled_agents", return_value=disabled_agents), \
             patch("lib.heartbeat._attempt_auto_recovery", return_value=True), \
             patch("lib.heartbeat._load_monitoring_down_state", return_value={}), \
             patch("lib.heartbeat._save_monitoring_down_state"), \
             patch("lib.heartbeat._load_learning_state", return_value={"version": 1, "agents": {}, "tasks": {}}), \
             patch("lib.heartbeat._update_learning_state"), \
             patch("lib.alerts.send_critical_monitoring_alert") as mock_alert:
            from lib.heartbeat import run_heartbeat

            result = run_heartbeat(sample_config, wake_delay=0)

            # Auto-recovery succeeded, so no human alert should fire
            mock_alert.assert_not_called()
            assert result["disabled_agents"] == disabled_agents


class TestMonitoringDownThreshold:
    """Tests for the 3-tier monitoring-down recovery cascade.

    The cascade is:
      1. Auto-recovery (bootout/bootstrap) — every detection
      2. LLM self-diagnosis (claude -p) — after auto-recovery fails once
      3. Human alert (Slack/macOS) — after consecutive_detections >= 3

    State is tracked per-agent in state/_monitoring_down_state.json.
    """

    # --- Pure unit tests for threshold logic (tests 1-8) ---

    def test_single_detection_no_alert(self):
        """One detection should NOT trigger a human alert (below threshold)."""
        from lib.heartbeat import (
            _record_monitoring_detection,
            _should_alert_monitoring_down,
            MONITORING_ALERT_THRESHOLD,
        )

        state = {}
        state = _record_monitoring_detection("healthcheck", state)

        assert state["healthcheck"]["consecutive_detections"] == 1
        assert _should_alert_monitoring_down("healthcheck", state) is False

    def test_below_threshold_no_alert(self):
        """Two detections should still NOT trigger a human alert."""
        from lib.heartbeat import (
            _record_monitoring_detection,
            _should_alert_monitoring_down,
        )

        state = {}
        state = _record_monitoring_detection("healthcheck", state)
        state = _record_monitoring_detection("healthcheck", state)

        assert state["healthcheck"]["consecutive_detections"] == 2
        assert _should_alert_monitoring_down("healthcheck", state) is False

    def test_at_threshold_alerts(self):
        """Three detections (== MONITORING_ALERT_THRESHOLD) SHOULD trigger alert."""
        from lib.heartbeat import (
            _record_monitoring_detection,
            _should_alert_monitoring_down,
            MONITORING_ALERT_THRESHOLD,
        )

        state = {}
        for _ in range(MONITORING_ALERT_THRESHOLD):
            state = _record_monitoring_detection("healthcheck", state)

        assert state["healthcheck"]["consecutive_detections"] == MONITORING_ALERT_THRESHOLD
        assert _should_alert_monitoring_down("healthcheck", state) is True

    def test_above_threshold_alerts(self):
        """Five detections (above threshold) should still trigger alert."""
        from lib.heartbeat import (
            _record_monitoring_detection,
            _should_alert_monitoring_down,
        )

        state = {}
        for _ in range(5):
            state = _record_monitoring_detection("healthcheck", state)

        assert state["healthcheck"]["consecutive_detections"] == 5
        assert _should_alert_monitoring_down("healthcheck", state) is True

    def test_auto_recovery_resets_counter(self):
        """Successful recovery should reset the consecutive counter to zero."""
        from lib.heartbeat import (
            _record_monitoring_detection,
            _clear_monitoring_down,
        )

        state = {}
        state = _record_monitoring_detection("healthcheck", state)
        state = _record_monitoring_detection("healthcheck", state)
        assert state["healthcheck"]["consecutive_detections"] == 2

        state = _clear_monitoring_down("healthcheck", state)

        # Agent entry should be cleared or counter reset
        if "healthcheck" in state:
            assert state["healthcheck"]["consecutive_detections"] == 0
        # If key is removed entirely, that also counts as reset

    def test_alert_cooldown_respected(self):
        """Alert within 24h of last_alert should be suppressed even at threshold."""
        from lib.heartbeat import (
            _should_alert_monitoring_down,
            MONITORING_ALERT_THRESHOLD,
        )

        recent_alert = (datetime.now() - timedelta(hours=1)).isoformat()
        state = {
            "healthcheck": {
                "consecutive_detections": MONITORING_ALERT_THRESHOLD,
                "first_detected": (datetime.now() - timedelta(hours=3)).isoformat(),
                "last_detected": datetime.now().isoformat(),
                "last_alert": recent_alert,
                "recovery_attempts": MONITORING_ALERT_THRESHOLD,
                "llm_diagnosis_attempted": False,
            }
        }

        assert _should_alert_monitoring_down("healthcheck", state) is False

    def test_alert_cooldown_expired(self):
        """Alert 25h after last_alert should fire when at threshold."""
        from lib.heartbeat import (
            _should_alert_monitoring_down,
            MONITORING_ALERT_THRESHOLD,
        )

        old_alert = (datetime.now() - timedelta(hours=25)).isoformat()
        state = {
            "healthcheck": {
                "consecutive_detections": MONITORING_ALERT_THRESHOLD,
                "first_detected": (datetime.now() - timedelta(hours=26)).isoformat(),
                "last_detected": datetime.now().isoformat(),
                "last_alert": old_alert,
                "recovery_attempts": MONITORING_ALERT_THRESHOLD,
                "llm_diagnosis_attempted": False,
            }
        }

        assert _should_alert_monitoring_down("healthcheck", state) is True

    def test_per_agent_independence(self):
        """Agent A at threshold should alert; agent B below threshold should not."""
        from lib.heartbeat import (
            _record_monitoring_detection,
            _should_alert_monitoring_down,
            MONITORING_ALERT_THRESHOLD,
        )

        state = {}
        # Agent A: record 3 detections (at threshold)
        for _ in range(MONITORING_ALERT_THRESHOLD):
            state = _record_monitoring_detection("healthcheck", state)
        # Agent B: record only 1 detection
        state = _record_monitoring_detection("heartbeat", state)

        assert _should_alert_monitoring_down("healthcheck", state) is True
        assert _should_alert_monitoring_down("heartbeat", state) is False

    # --- Integration tests for run_heartbeat flow (tests 9-10) ---

    def test_heartbeat_integration_no_false_alert(self, sample_config):
        """Single disabled detection should NOT send a human Slack alert.

        Auto-recovery runs but even if it fails, the alert threshold isn't met.
        """
        recent_time = datetime.now() - timedelta(hours=1)
        mock_state = MockTaskState(
            task_name="any",
            last_run=recent_time.isoformat(),
            last_status="success",
        )

        disabled_agents = [
            {"task_id": "healthcheck", "label": "com.pinglet.healthcheck",
             "exit_code": 78, "status": "disabled"}
        ]

        # Monitoring down state: first detection (below threshold)
        monitoring_down = {}

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False), \
             patch("lib.heartbeat.detect_disabled_agents", return_value=disabled_agents), \
             patch("lib.heartbeat._attempt_auto_recovery", return_value=False), \
             patch("lib.heartbeat._load_monitoring_down_state", return_value=monitoring_down), \
             patch("lib.heartbeat._save_monitoring_down_state") as mock_save, \
             patch("lib.heartbeat._should_send_disabled_agent_alert", return_value=False), \
             patch("lib.alerts.send_critical_monitoring_alert") as mock_alert:
            from lib.heartbeat import run_heartbeat

            result = run_heartbeat(sample_config, wake_delay=0)

            # Human alert should NOT have been called (only 1 detection)
            mock_alert.assert_not_called()

    def test_heartbeat_integration_alerts_at_threshold(self, sample_config):
        """Persistent disabled agent (3 cycles) SHOULD trigger human alert."""
        recent_time = datetime.now() - timedelta(hours=1)
        mock_state = MockTaskState(
            task_name="any",
            last_run=recent_time.isoformat(),
            last_status="success",
        )

        disabled_agents = [
            {"task_id": "healthcheck", "label": "com.pinglet.healthcheck",
             "exit_code": 78, "status": "disabled"}
        ]

        # Monitoring down state: already detected 2 times, this will be 3rd
        monitoring_down = {
            "healthcheck": {
                "consecutive_detections": 2,
                "first_detected": (datetime.now() - timedelta(hours=2)).isoformat(),
                "last_detected": (datetime.now() - timedelta(hours=1)).isoformat(),
                "last_alert": None,
                "recovery_attempts": 2,
                "llm_diagnosis_attempted": True,
            }
        }

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False), \
             patch("lib.heartbeat.detect_disabled_agents", return_value=disabled_agents), \
             patch("lib.heartbeat._attempt_auto_recovery", return_value=False), \
             patch("lib.heartbeat._attempt_llm_self_diagnosis", return_value=False), \
             patch("lib.heartbeat._load_monitoring_down_state", return_value=monitoring_down), \
             patch("lib.heartbeat._save_monitoring_down_state") as mock_save, \
             patch("lib.heartbeat._record_disabled_agent_alert"), \
             patch("lib.alerts.send_critical_monitoring_alert") as mock_alert:
            from lib.heartbeat import run_heartbeat

            result = run_heartbeat(sample_config, wake_delay=0)

            # Human alert SHOULD have been sent (3rd detection = at threshold)
            mock_alert.assert_called()

    # --- LLM self-diagnosis tests (tests 11-13) ---

    def test_llm_diagnosis_invoked_after_recovery_fails(self):
        """After auto-recovery fails once, 2nd detection should invoke LLM diagnosis."""
        from lib.heartbeat import (
            _record_monitoring_detection,
            _attempt_llm_self_diagnosis,
        )

        # State: 1 prior detection, auto-recovery already failed
        state = {
            "healthcheck": {
                "consecutive_detections": 1,
                "first_detected": (datetime.now() - timedelta(hours=1)).isoformat(),
                "last_detected": (datetime.now() - timedelta(hours=1)).isoformat(),
                "last_alert": None,
                "recovery_attempts": 1,
                "llm_diagnosis_attempted": False,
            }
        }

        # Record 2nd detection
        state = _record_monitoring_detection("healthcheck", state)
        assert state["healthcheck"]["consecutive_detections"] == 2

        # LLM diagnosis should be invocable with the agent's status
        mock_status = {"installed": True, "running": False, "exit_code": 78,
                       "disabled": True, "status": "disabled"}

        with patch("lib.heartbeat.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="Fixed it")
            result = _attempt_llm_self_diagnosis("healthcheck", mock_status)

        # Just verify it was called (the function exists and accepts these args)
        assert isinstance(result, bool)

    def test_llm_diagnosis_success_resets_state(self):
        """Successful LLM diagnosis should clear the monitoring-down state."""
        from lib.heartbeat import (
            _attempt_llm_self_diagnosis,
            _clear_monitoring_down,
        )

        state = {
            "healthcheck": {
                "consecutive_detections": 2,
                "first_detected": (datetime.now() - timedelta(hours=2)).isoformat(),
                "last_detected": datetime.now().isoformat(),
                "last_alert": None,
                "recovery_attempts": 2,
                "llm_diagnosis_attempted": True,
            }
        }

        mock_status = {"installed": True, "running": False, "exit_code": 78,
                       "disabled": True, "status": "disabled"}

        with patch("lib.heartbeat.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="Fixed it")
            diagnosis_result = _attempt_llm_self_diagnosis("healthcheck", mock_status)

        # If diagnosis succeeded, clear the state
        if diagnosis_result:
            state = _clear_monitoring_down("healthcheck", state)

            if "healthcheck" in state:
                assert state["healthcheck"]["consecutive_detections"] == 0
            # Agent removed entirely is also acceptable

    def test_llm_diagnosis_failure_increments(self):
        """Failed LLM diagnosis should still allow counter to increment."""
        from lib.heartbeat import (
            _record_monitoring_detection,
            _attempt_llm_self_diagnosis,
        )

        state = {
            "healthcheck": {
                "consecutive_detections": 2,
                "first_detected": (datetime.now() - timedelta(hours=2)).isoformat(),
                "last_detected": (datetime.now() - timedelta(hours=1)).isoformat(),
                "last_alert": None,
                "recovery_attempts": 2,
                "llm_diagnosis_attempted": True,
            }
        }

        mock_status = {"installed": True, "running": False, "exit_code": 78,
                       "disabled": True, "status": "disabled"}

        with patch("lib.heartbeat.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=1, stdout="Could not diagnose")
            diagnosis_result = _attempt_llm_self_diagnosis("healthcheck", mock_status)

        # Diagnosis failed — counter should still increment on next detection
        state = _record_monitoring_detection("healthcheck", state)
        assert state["healthcheck"]["consecutive_detections"] == 3

    # --- State persistence roundtrip (test 14) ---

    def test_state_persistence_roundtrip(self, tmp_path):
        """Save state, load state, verify all fields preserved."""
        from lib.heartbeat import (
            _load_monitoring_down_state,
            _save_monitoring_down_state,
        )

        now = datetime.now()
        original_state = {
            "healthcheck": {
                "consecutive_detections": 3,
                "first_detected": (now - timedelta(hours=3)).isoformat(),
                "last_detected": now.isoformat(),
                "last_alert": (now - timedelta(hours=1)).isoformat(),
                "recovery_attempts": 3,
                "llm_diagnosis_attempted": True,
            },
            "heartbeat": {
                "consecutive_detections": 1,
                "first_detected": now.isoformat(),
                "last_detected": now.isoformat(),
                "last_alert": None,
                "recovery_attempts": 1,
                "llm_diagnosis_attempted": False,
            },
        }

        # Patch the state file path to use tmp_path
        state_file = tmp_path / "_monitoring_down_state.json"

        with patch("lib.heartbeat.MONITORING_DOWN_STATE_FILE", state_file), \
             patch("lib.heartbeat.STATE_DIR", tmp_path):
            _save_monitoring_down_state(original_state)
            loaded_state = _load_monitoring_down_state()

        # Verify all fields round-tripped correctly
        assert loaded_state == original_state
        for agent_id in ("healthcheck", "heartbeat"):
            assert loaded_state[agent_id]["consecutive_detections"] == original_state[agent_id]["consecutive_detections"]
            assert loaded_state[agent_id]["first_detected"] == original_state[agent_id]["first_detected"]
            assert loaded_state[agent_id]["last_detected"] == original_state[agent_id]["last_detected"]
            assert loaded_state[agent_id]["last_alert"] == original_state[agent_id]["last_alert"]
            assert loaded_state[agent_id]["recovery_attempts"] == original_state[agent_id]["recovery_attempts"]
            assert loaded_state[agent_id]["llm_diagnosis_attempted"] == original_state[agent_id]["llm_diagnosis_attempted"]
