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


class TestMonitoringAgentFalsePositive:
    """Bug fix: monitoring agents exiting 1 (found issues) should NOT be flagged as DOWN.

    The heartbeat exits 1 when it finds missed tasks — this is normal operation.
    _check_monitoring_agents was treating status='failed' as dead, causing
    false 'WARNING: Monitoring agent(s) DOWN: heartbeat' on every CLI invocation.
    """

    def test_monitoring_agent_exit_1_not_dead(self):
        """Heartbeat exiting 1 (found missed tasks) should NOT be flagged as dead."""
        from pinglet import _check_monitoring_agents

        def mock_status(task_id):
            if task_id == "heartbeat":
                # Exit 1 = found missed tasks, normal operation
                return {"installed": True, "running": False, "exit_code": 1,
                        "disabled": False, "status": "failed"}
            return {"installed": True, "running": False, "exit_code": 0,
                    "disabled": False, "status": "idle"}

        with patch("lib.task_manager.get_launchd_status", side_effect=mock_status):
            dead = _check_monitoring_agents()

        assert "heartbeat" not in dead

    def test_monitoring_agent_exit_0_not_dead(self):
        """Heartbeat exiting 0 (no issues found) should NOT be flagged as dead."""
        from pinglet import _check_monitoring_agents

        def mock_status(task_id):
            return {"installed": True, "running": False, "exit_code": 0,
                    "disabled": False, "status": "idle"}

        with patch("lib.task_manager.get_launchd_status", side_effect=mock_status):
            dead = _check_monitoring_agents()

        assert len(dead) == 0

    def test_monitoring_agent_disabled_is_dead(self):
        """Monitoring agent with exit 78 (disabled by launchd) SHOULD be flagged as dead."""
        from pinglet import _check_monitoring_agents

        def mock_status(task_id):
            if task_id == "heartbeat":
                return {"installed": True, "running": False, "exit_code": 78,
                        "disabled": True, "status": "disabled"}
            return {"installed": True, "running": False, "exit_code": 0,
                    "disabled": False, "status": "idle"}

        with patch("lib.task_manager.get_launchd_status", side_effect=mock_status):
            dead = _check_monitoring_agents()

        assert "heartbeat" in dead

    def test_monitoring_agent_not_loaded_is_dead(self):
        """Monitoring agent not loaded in launchd SHOULD be flagged as dead."""
        from pinglet import _check_monitoring_agents

        def mock_status(task_id):
            if task_id == "healthcheck":
                return {"installed": True, "running": False, "exit_code": None,
                        "disabled": False, "status": "not_loaded"}
            return {"installed": True, "running": False, "exit_code": 0,
                    "disabled": False, "status": "idle"}

        with patch("lib.task_manager.get_launchd_status", side_effect=mock_status):
            dead = _check_monitoring_agents()

        assert "healthcheck" in dead

    def test_monitoring_agent_not_installed_is_dead(self):
        """Monitoring agent with missing plist SHOULD be flagged as dead."""
        from pinglet import _check_monitoring_agents

        def mock_status(task_id):
            if task_id == "heartbeat":
                return {"installed": False, "running": False, "exit_code": None,
                        "disabled": False, "status": "not_installed"}
            return {"installed": True, "running": False, "exit_code": 0,
                    "disabled": False, "status": "idle"}

        with patch("lib.task_manager.get_launchd_status", side_effect=mock_status):
            dead = _check_monitoring_agents()

        assert "heartbeat" in dead

    def test_monitoring_agent_running_not_dead(self):
        """Monitoring agent that is currently running should NOT be flagged as dead."""
        from pinglet import _check_monitoring_agents

        def mock_status(task_id):
            if task_id == "heartbeat":
                return {"installed": True, "running": True, "exit_code": None,
                        "disabled": False, "status": "running"}
            return {"installed": True, "running": False, "exit_code": 0,
                    "disabled": False, "status": "idle"}

        with patch("lib.task_manager.get_launchd_status", side_effect=mock_status):
            dead = _check_monitoring_agents()

        assert "heartbeat" not in dead


class TestStaleLaunchdTriggerDetection:
    """Bug fix: detect and recover stale launchd calendar triggers.

    When launchd bootstraps an agent but the calendar event trigger gets stuck,
    the agent shows runs=0 and never fires. The heartbeat detects the missed task
    but never attempts to fix the root cause (stale trigger).

    The fix adds stale trigger detection: when a task is overdue AND launchd
    shows runs=0 (never fired since bootstrap), attempt bootout+bootstrap recovery.
    """

    def test_get_launchd_run_count_parses_runs(self):
        """get_launchd_run_count should parse 'runs = N' from launchctl print."""
        from lib.task_manager import get_launchd_run_count

        mock_output = "\truns = 0\n\tlast exit code = (never exited)\n"
        mock_result = MagicMock(returncode=0, stdout=mock_output, stderr="")

        with patch("lib.task_manager._get_uid", return_value="501"), \
             patch("subprocess.run", return_value=mock_result):
            count = get_launchd_run_count("obsidian-tab-archiver")

        assert count == 0

    def test_get_launchd_run_count_nonzero(self):
        """get_launchd_run_count should return actual run count."""
        from lib.task_manager import get_launchd_run_count

        mock_output = "\truns = 267\n\tlast exit code = 0\n"
        mock_result = MagicMock(returncode=0, stdout=mock_output, stderr="")

        with patch("lib.task_manager._get_uid", return_value="501"), \
             patch("subprocess.run", return_value=mock_result):
            count = get_launchd_run_count("heartbeat")

        assert count == 267

    def test_get_launchd_run_count_not_loaded(self):
        """get_launchd_run_count should return None for unloaded agents."""
        from lib.task_manager import get_launchd_run_count

        mock_result = MagicMock(returncode=1, stdout="", stderr="not found")

        with patch("lib.task_manager._get_uid", return_value="501"), \
             patch("subprocess.run", return_value=mock_result):
            count = get_launchd_run_count("nonexistent")

        assert count is None

    def test_detect_stale_triggers_finds_zero_runs(self, sample_config):
        """detect_stale_triggers should identify tasks with runs=0 that are overdue."""
        from lib.heartbeat import detect_stale_triggers

        old_time = datetime.now() - timedelta(hours=30)
        mock_state = MockTaskState(
            task_name="obsidian-tab-archiver",
            last_run=old_time.isoformat(),
            last_status="success",
        )

        missed_tasks = [{
            "task_name": "obsidian-tab-archiver",
            "display_name": "Obsidian Tab Archiver",
            "hours_overdue": 16.0,
            "threshold": 14,
            "last_run": old_time.isoformat(),
            "never_run": False,
        }]

        with patch("lib.heartbeat._get_launchd_run_count", return_value=0), \
             patch("lib.heartbeat._get_launchd_status", return_value={
                 "installed": True, "running": False, "exit_code": 0,
                 "disabled": False, "status": "idle"}):
            stale = detect_stale_triggers(missed_tasks)

        assert len(stale) == 1
        assert stale[0]["task_name"] == "obsidian-tab-archiver"

    def test_detect_stale_triggers_ignores_healthy(self, sample_config):
        """Tasks with runs > 0 should NOT be flagged as stale triggers."""
        from lib.heartbeat import detect_stale_triggers

        old_time = datetime.now() - timedelta(hours=30)
        missed_tasks = [{
            "task_name": "obsidian-tab-archiver",
            "display_name": "Obsidian Tab Archiver",
            "hours_overdue": 16.0,
            "threshold": 14,
            "last_run": old_time.isoformat(),
            "never_run": False,
        }]

        with patch("lib.heartbeat._get_launchd_run_count", return_value=42), \
             patch("lib.heartbeat._get_launchd_status", return_value={
                 "installed": True, "running": False, "exit_code": 0,
                 "disabled": False, "status": "idle"}):
            stale = detect_stale_triggers(missed_tasks)

        assert len(stale) == 0

    def test_recover_stale_trigger_bootout_bootstrap(self):
        """recover_stale_trigger should bootout+bootstrap to reset the trigger."""
        from lib.heartbeat import recover_stale_trigger

        mock_uid_result = MagicMock(returncode=0, stdout=b"501\n")
        mock_bootout_result = MagicMock(returncode=0)
        mock_bootstrap_result = MagicMock(returncode=0)

        call_count = {"n": 0}

        def mock_run(cmd, **kwargs):
            call_count["n"] += 1
            if cmd == ["id", "-u"]:
                return MagicMock(stdout=b"501")
            elif "bootout" in cmd:
                return mock_bootout_result
            elif "bootstrap" in cmd:
                return mock_bootstrap_result
            return MagicMock(returncode=0)

        plist = Path.home() / "Library" / "LaunchAgents" / "com.pinglet.obsidian-tab-archiver.plist"

        with patch("subprocess.run", side_effect=mock_run), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("lib.heartbeat._get_launchd_run_count", return_value=1):
            result = recover_stale_trigger("obsidian-tab-archiver")

        assert result == "recovered"

    def test_recover_stale_trigger_no_plist(self):
        """recover_stale_trigger should fail gracefully when plist is missing."""
        from lib.heartbeat import recover_stale_trigger

        with patch("pathlib.Path.exists", return_value=False):
            result = recover_stale_trigger("nonexistent-task")

        assert result == "failed"

    def test_heartbeat_recovers_stale_triggers(self, sample_config):
        """run_heartbeat should attempt recovery for tasks with stale triggers."""
        old_time = datetime.now() - timedelta(hours=30)
        mock_state = MockTaskState(
            task_name="obsidian-tab-archiver",
            last_run=old_time.isoformat(),
            last_status="success",
        )

        stale_tasks = [{
            "task_name": "obsidian-tab-archiver",
            "display_name": "Obsidian Tab Archiver",
            "hours_overdue": 16.0,
            "threshold": 14,
            "last_run": old_time.isoformat(),
            "never_run": False,
        }]

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False), \
             patch("lib.heartbeat._send_missed_task_notification"), \
             patch("lib.heartbeat._send_slack_message"), \
             patch("lib.heartbeat._load_heartbeat_alert_state", return_value={}), \
             patch("lib.heartbeat._save_heartbeat_alert_state"), \
             patch("lib.heartbeat.detect_disabled_agents", return_value=[]), \
             patch("lib.heartbeat._load_monitoring_down_state", return_value={}), \
             patch("lib.heartbeat._save_monitoring_down_state"), \
             patch("lib.heartbeat.detect_stale_triggers", return_value=stale_tasks), \
             patch("lib.heartbeat.recover_stale_trigger", return_value="recovered") as mock_recover:
            from lib.heartbeat import run_heartbeat

            result = run_heartbeat(sample_config, wake_delay=0)

            mock_recover.assert_called_once_with("obsidian-tab-archiver")
            assert result.get("stale_triggers_recovered", 0) >= 1


class TestStaleRecoveryCooldown:
    """Bug fix: stale trigger recovery must not repeat every heartbeat cycle.

    When a weekly task (e.g., pipeline-review) is overdue, the heartbeat detects
    runs=0 and re-bootstraps it. But re-bootstrapping resets runs to 0, so the
    NEXT heartbeat cycle sees runs=0 again and re-bootstraps again — an infinite
    loop that never lets the agent fire on its next scheduled day.

    The fix: after a stale trigger recovery, apply a cooldown equal to the task's
    expected_interval before attempting another recovery for the same task.
    """

    def test_first_stale_recovery_always_proceeds(self):
        """First stale recovery for a task should always be attempted."""
        from lib.heartbeat import _should_recover_stale_trigger

        # No prior recovery state
        task_diagnosis_state = {}
        assert _should_recover_stale_trigger("pipeline-review", task_diagnosis_state, 180) is True

    def test_stale_recovery_blocked_within_cooldown(self):
        """Stale recovery within expected_interval of last recovery should be blocked."""
        from lib.heartbeat import _should_recover_stale_trigger

        # Recovery attempted 2 hours ago, expected_interval is 180h
        recent = (datetime.now() - timedelta(hours=2)).isoformat()
        task_diagnosis_state = {
            "pipeline-review": {
                "last_stale_recovery": recent,
                "consecutive_detections": 3,
            }
        }
        assert _should_recover_stale_trigger("pipeline-review", task_diagnosis_state, 180) is False

    def test_stale_recovery_allowed_after_cooldown(self):
        """Stale recovery after expected_interval should be allowed."""
        from lib.heartbeat import _should_recover_stale_trigger

        # Recovery attempted 200 hours ago, expected_interval is 180h
        old = (datetime.now() - timedelta(hours=200)).isoformat()
        task_diagnosis_state = {
            "pipeline-review": {
                "last_stale_recovery": old,
                "consecutive_detections": 10,
            }
        }
        assert _should_recover_stale_trigger("pipeline-review", task_diagnosis_state, 180) is True

    def test_stale_recovery_cooldown_uses_expected_interval(self):
        """Short-interval tasks (e.g., hourly) should have short cooldowns."""
        from lib.heartbeat import _should_recover_stale_trigger

        # Recovery 3 hours ago, expected_interval is 1.5h — cooldown expired
        three_h_ago = (datetime.now() - timedelta(hours=3)).isoformat()
        state = {"git-sync": {"last_stale_recovery": three_h_ago, "consecutive_detections": 2}}
        assert _should_recover_stale_trigger("git-sync", state, 1.5) is True

        # Recovery 30 min ago, expected_interval is 1.5h — still in cooldown
        recent = (datetime.now() - timedelta(minutes=30)).isoformat()
        state = {"git-sync": {"last_stale_recovery": recent, "consecutive_detections": 2}}
        assert _should_recover_stale_trigger("git-sync", state, 1.5) is False

    def test_record_stale_recovery_sets_timestamp(self):
        """_record_stale_recovery should set last_stale_recovery timestamp."""
        from lib.heartbeat import _record_stale_recovery

        state = {}
        _record_stale_recovery("pipeline-review", state)

        assert "pipeline-review" in state
        assert "last_stale_recovery" in state["pipeline-review"]
        # Timestamp should be recent (within 5 seconds)
        ts = datetime.fromisoformat(state["pipeline-review"]["last_stale_recovery"])
        assert (datetime.now() - ts).total_seconds() < 5

    def test_stale_recovery_then_detection_no_keyerror(self):
        """Regression: _record_stale_recovery + _record_task_detection on a new
        task must not raise KeyError.

        This crash-looped heartbeat 689 times between 2026-05-15 and 2026-05-27.
        _record_stale_recovery used to init a bare {} entry, then
        _record_task_detection tried to increment "consecutive_detections" on it.
        """
        from lib.heartbeat import _record_stale_recovery, _record_task_detection

        state = {}
        _record_stale_recovery("brand-new-task", state)
        # Must not raise — the partial entry must be self-healed.
        state = _record_task_detection("brand-new-task", state, "stale (2h overdue)")
        assert state["brand-new-task"]["consecutive_detections"] == 1
        assert state["brand-new-task"]["recovery_attempts"] == 1
        assert state["brand-new-task"]["detected_problem"] == "stale (2h overdue)"
        # Stale-recovery timestamp must be preserved through detection.
        assert "last_stale_recovery" in state["brand-new-task"]

    def test_record_task_detection_heals_partial_entry(self):
        """_record_task_detection must merge in missing schema keys, not crash."""
        from lib.heartbeat import _record_task_detection

        # Simulate a legacy/partial entry written by an earlier code path.
        state = {"legacy-task": {"last_stale_recovery": "2026-01-01T00:00:00"}}
        state = _record_task_detection("legacy-task", state, "disabled (exit 78)")
        assert state["legacy-task"]["consecutive_detections"] == 1
        assert state["legacy-task"]["recovery_attempts"] == 1
        # Pre-existing keys are preserved.
        assert state["legacy-task"]["last_stale_recovery"] == "2026-01-01T00:00:00"

    def test_heartbeat_skips_stale_recovery_when_cooldown_active(self, sample_config):
        """run_heartbeat should NOT re-recover stale triggers within cooldown.

        This is the core regression test for the pipeline-review infinite loop bug.
        """
        old_time = datetime.now() - timedelta(hours=200)  # 200h ago — overdue
        mock_state = MockTaskState(
            task_name="pipeline-review",
            last_run=old_time.isoformat(),
            last_status="success",
        )

        stale_tasks = [{
            "task_name": "pipeline-review",
            "display_name": "Weekly Pipeline Review",
            "hours_overdue": 20.0,
            "threshold": 180,
            "last_run": old_time.isoformat(),
            "never_run": False,
        }]

        # Prior recovery 2 hours ago — still within 180h cooldown
        recent_recovery = (datetime.now() - timedelta(hours=2)).isoformat()
        task_diag_state = {
            "pipeline-review": {
                "consecutive_detections": 3,
                "first_detected": (datetime.now() - timedelta(hours=3)).isoformat(),
                "last_detected": datetime.now().isoformat(),
                "detected_problem": "stale (20h overdue)",
                "diagnosis_attempted": False,
                "last_diagnosis": None,
                "recovery_attempts": 3,
                "fixed_at": None,
                "recurring_fix_failures": 0,
                "last_fix_summary": "",
                "last_stale_recovery": recent_recovery,
            }
        }

        # Add pipeline-review to sample_config
        config = dict(sample_config)
        config["healthcheck"] = dict(config["healthcheck"])
        config["healthcheck"]["expected_intervals"] = dict(config["healthcheck"]["expected_intervals"])
        config["healthcheck"]["expected_intervals"]["pipeline-review"] = 180
        config["tasks"] = dict(config["tasks"])
        config["tasks"]["pipeline-review"] = {
            "name": "Weekly Pipeline Review",
            "command": "/opt/homebrew/bin/claude",
            "schedule": "weekly mon 7:44",
        }

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False), \
             patch("lib.heartbeat._send_missed_task_notification"), \
             patch("lib.heartbeat._send_slack_message"), \
             patch("lib.heartbeat._load_heartbeat_alert_state", return_value={}), \
             patch("lib.heartbeat._save_heartbeat_alert_state"), \
             patch("lib.heartbeat.detect_disabled_agents", return_value=[]), \
             patch("lib.heartbeat._load_monitoring_down_state", return_value={}), \
             patch("lib.heartbeat._save_monitoring_down_state"), \
             patch("lib.heartbeat.detect_stale_triggers", return_value=stale_tasks), \
             patch("lib.heartbeat.recover_stale_trigger") as mock_recover, \
             patch("lib.heartbeat._load_task_diagnosis_state", return_value=task_diag_state), \
             patch("lib.heartbeat._save_task_diagnosis_state"), \
             patch("lib.heartbeat._attempt_task_diagnosis", return_value=False):
            from lib.heartbeat import run_heartbeat

            result = run_heartbeat(config, wake_delay=0)

            # Recovery should NOT have been called — cooldown active
            mock_recover.assert_not_called()
            assert result.get("stale_triggers_recovered", 0) == 0
