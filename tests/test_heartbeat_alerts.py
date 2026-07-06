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
             patch("lib.heartbeat._load_task_diagnosis_state", return_value={}), \
             patch("lib.heartbeat._save_task_diagnosis_state"), \
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
