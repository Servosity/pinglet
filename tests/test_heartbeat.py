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
             patch("lib.heartbeat._send_missed_task_notification") as mock_notify, \
             patch("lib.heartbeat.detect_disabled_agents", return_value=[]):
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
             patch("lib.heartbeat._send_slack_message") as mock_slack, \
             patch("lib.heartbeat.detect_disabled_agents", return_value=[]):
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
             patch("lib.heartbeat.detect_disabled_agents", return_value=[]), \
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

    def test_detects_failed_agent(self, sample_config):
        """Failed agent (non-zero, non-78 exit) should be detected."""
        def mock_status(task_id):
            if task_id == "obsidian-tab-archiver":
                return {"installed": True, "running": False, "exit_code": 1, "disabled": False, "status": "failed"}
            return {"installed": True, "running": False, "exit_code": 0, "disabled": False, "status": "idle"}

        with patch("lib.heartbeat._get_launchd_status", side_effect=mock_status):
            from lib.heartbeat import detect_disabled_agents
            disabled = detect_disabled_agents(sample_config)

        task_ids = [d["task_id"] for d in disabled]
        assert "obsidian-tab-archiver" in task_ids

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


class TestHeartbeatAutoRecovery:
    """Tests for auto-recovery of disabled/failed agents."""

    def test_auto_recovers_regular_task(self, sample_config):
        """Disabled regular task should be auto-recovered via disable+enable."""
        disabled = [{"task_id": "uce", "label": "com.pinglet.uce", "exit_code": 78, "status": "disabled"}]

        with patch("lib.heartbeat.detect_disabled_agents", return_value=disabled), \
             patch("lib.heartbeat._attempt_auto_recovery", return_value=True) as mock_recover, \
             patch("lib.heartbeat.detect_missed_tasks", return_value=[]), \
             patch("lib.heartbeat._load_heartbeat_alert_state", return_value={}), \
             patch("lib.heartbeat._save_heartbeat_alert_state"):
            from lib.heartbeat import run_heartbeat

            result = run_heartbeat(sample_config, wake_delay=0)

            mock_recover.assert_called_once_with(disabled[0], sample_config)
            assert "uce" in result["auto_recovered"]

    def test_auto_recovery_failure_sends_alert(self, sample_config):
        """Failed auto-recovery should trigger critical monitoring alert."""
        disabled = [{"task_id": "uce", "label": "com.pinglet.uce", "exit_code": 78, "status": "disabled"}]

        with patch("lib.heartbeat.detect_disabled_agents", return_value=disabled), \
             patch("lib.heartbeat._attempt_auto_recovery", return_value=False), \
             patch("lib.heartbeat.detect_missed_tasks", return_value=[]), \
             patch("lib.heartbeat._load_heartbeat_alert_state", return_value={}), \
             patch("lib.heartbeat._save_heartbeat_alert_state"), \
             patch("lib.alerts.send_critical_monitoring_alert") as mock_alert:
            from lib.heartbeat import run_heartbeat

            result = run_heartbeat(sample_config, wake_delay=0)

            mock_alert.assert_called_once()
            assert "uce" not in result.get("auto_recovered", [])

    def test_auto_recovers_monitoring_agent(self, sample_config):
        """Monitoring agents (not in config.tasks) should be recovered via bootout+bootstrap."""
        disabled = [{"task_id": "healthcheck", "label": "com.pinglet.healthcheck", "exit_code": 78, "status": "disabled"}]

        with patch("lib.heartbeat.detect_disabled_agents", return_value=disabled), \
             patch("lib.heartbeat._attempt_auto_recovery", return_value=True) as mock_recover, \
             patch("lib.heartbeat.detect_missed_tasks", return_value=[]), \
             patch("lib.heartbeat._load_heartbeat_alert_state", return_value={}), \
             patch("lib.heartbeat._save_heartbeat_alert_state"):
            from lib.heartbeat import run_heartbeat

            result = run_heartbeat(sample_config, wake_delay=0)

            mock_recover.assert_called_once_with(disabled[0], sample_config)
            assert "healthcheck" in result["auto_recovered"]

    def test_auto_recovery_result_in_return(self, sample_config):
        """run_heartbeat return dict must include auto_recovered list."""
        with patch("lib.heartbeat.detect_disabled_agents", return_value=[]), \
             patch("lib.heartbeat.detect_missed_tasks", return_value=[]), \
             patch("lib.heartbeat._load_heartbeat_alert_state", return_value={}), \
             patch("lib.heartbeat._save_heartbeat_alert_state"):
            from lib.heartbeat import run_heartbeat

            result = run_heartbeat(sample_config, wake_delay=0)

            assert "auto_recovered" in result
            assert isinstance(result["auto_recovered"], list)

    def test_attempt_auto_recovery_regular_task(self, sample_config):
        """_attempt_auto_recovery should disable+enable for regular tasks."""
        agent = {"task_id": "uce", "label": "com.pinglet.uce", "exit_code": 78, "status": "disabled"}

        with patch("lib.heartbeat._disable_task", return_value={"ok": True}) as mock_dis, \
             patch("lib.heartbeat._enable_task", return_value={"ok": True, "launchd_status": {"status": "idle"}}) as mock_en:
            from lib.heartbeat import _attempt_auto_recovery

            result = _attempt_auto_recovery(agent, sample_config)

            assert result is True
            mock_dis.assert_called_once_with("uce")
            mock_en.assert_called_once_with("uce")

    def test_attempt_auto_recovery_monitoring_agent(self, sample_config):
        """_attempt_auto_recovery should bootout+bootstrap for monitoring agents."""
        agent = {"task_id": "healthcheck", "label": "com.pinglet.healthcheck", "exit_code": 78, "status": "disabled"}

        with patch("subprocess.run") as mock_run, \
             patch("lib.heartbeat._get_uid", return_value="501"), \
             patch("lib.heartbeat._get_launchd_status", return_value={
                 "installed": True, "running": False, "exit_code": 0,
                 "disabled": False, "status": "idle",
             }):
            mock_run.return_value = MagicMock(returncode=0)
            from lib.heartbeat import _attempt_auto_recovery

            result = _attempt_auto_recovery(agent, sample_config)

            assert result is True
            # Should have called bootout then bootstrap
            assert mock_run.call_count == 2

    def test_attempt_auto_recovery_failure(self, sample_config):
        """_attempt_auto_recovery returns False when enable_task fails."""
        agent = {"task_id": "uce", "label": "com.pinglet.uce", "exit_code": 78, "status": "disabled"}

        with patch("lib.heartbeat._disable_task", return_value={"ok": True}), \
             patch("lib.heartbeat._enable_task", return_value={"ok": False, "error": "still broken"}):
            from lib.heartbeat import _attempt_auto_recovery

            result = _attempt_auto_recovery(agent, sample_config)

            assert result is False


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
