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
class TestTaskDiagnosisInvocation:
    """Tests for _attempt_task_diagnosis LLM invocation."""

    def test_diagnosis_uses_default_prompt_when_no_on_diagnose(self, tmp_path):
        """Without on_diagnose config, uses TASK_DIAGNOSE_PROMPT."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        task_config = {
            "name": "Clawdbot Sync",
            "command": "/bin/bash",
            "args": ["sync.sh"],
            "working_dir": "/tmp/clawdbot",
            "schedule": "hourly :22",
        }

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Fixed broken git repo"},
        })
        mock_proc.stderr = ""

        healthy_status = {"installed": True, "running": False, "exit_code": 0, "disabled": False, "status": "idle"}
        mock_state = MockTaskState(task_name="clawdbot-sync", last_run="2026-03-19T17:41:09", consecutive_failures=1)

        with patch("subprocess.run", return_value=mock_proc) as mock_run, \
             patch("lib.heartbeat._get_launchd_status", return_value=healthy_status), \
             patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._update_task_learning"), \
             patch("lib.heartbeat.PROJECT_ROOT", tmp_path), \
             patch("lib.heartbeat.LEARNING_FILE", state_dir / "_learning.json"), \
             patch("lib.heartbeat.log"):
            from lib.heartbeat import _attempt_task_diagnosis

            diagnosis_state = {"clawdbot-sync": {"recovery_attempts": 3}}
            result = _attempt_task_diagnosis(
                "clawdbot-sync", task_config,
                "stale (24h overdue)",
                diagnosis_state,
            )

            assert result is True
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            # Should use configured default provider order.
            assert cmd[0] == "codex"
            assert "exec" in cmd

    def test_diagnosis_uses_on_diagnose_config(self, tmp_path):
        """With on_diagnose config, uses custom command and prompt."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        task_config = {
            "name": "Clawdbot Sync",
            "command": "/bin/bash",
            "args": ["sync.sh"],
            "working_dir": "/tmp/clawdbot",
            "schedule": "hourly :22",
            "on_diagnose": {
                "command": "claude",
                "args": ["-p", "Task {task_id} is {detected_problem}. Fix it."],
                "timeout": 120,
                "max_turns": 3,
                "max_budget_usd": 1.50,
            },
        }

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '{"result": "Fixed"}'
        mock_proc.stderr = ""

        healthy_status = {"installed": True, "running": False, "exit_code": 0, "disabled": False, "status": "idle"}
        mock_state = MockTaskState(task_name="clawdbot-sync", last_run="2026-03-19T17:41:09")

        with patch("subprocess.run", return_value=mock_proc) as mock_run, \
             patch("lib.heartbeat._get_launchd_status", return_value=healthy_status), \
             patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._update_task_learning"), \
             patch("lib.heartbeat.PROJECT_ROOT", tmp_path), \
             patch("lib.heartbeat.LEARNING_FILE", state_dir / "_learning.json"), \
             patch("lib.heartbeat.log"):
            from lib.heartbeat import _attempt_task_diagnosis

            diagnosis_state = {"clawdbot-sync": {"recovery_attempts": 2}}
            result = _attempt_task_diagnosis(
                "clawdbot-sync", task_config,
                "stale (24h overdue)",
                diagnosis_state,
            )

            assert result is True
            # Verify template substitution
            cmd = mock_run.call_args[0][0]
            cmd_str = " ".join(str(c) for c in cmd)
            assert "clawdbot-sync" in cmd_str
            assert "{task_id}" not in cmd_str
            assert "{detected_problem}" not in cmd_str

    def test_diagnosis_returns_false_on_llm_failure(self, tmp_path):
        """LLM exits non-zero -> returns False."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        task_config = {
            "name": "Test Task",
            "command": "/bin/echo",
            "working_dir": "/tmp",
            "schedule": "daily 7:00",
        }

        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = "Cannot fix this"
        mock_proc.stderr = ""

        mock_state = MockTaskState(task_name="test-task")

        with patch("subprocess.run", return_value=mock_proc), \
             patch("lib.heartbeat._get_launchd_status", return_value={"disabled": True}), \
             patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._update_task_learning"), \
             patch("lib.heartbeat.PROJECT_ROOT", tmp_path), \
             patch("lib.heartbeat.LEARNING_FILE", state_dir / "_learning.json"), \
             patch("lib.heartbeat.log"):
            from lib.heartbeat import _attempt_task_diagnosis

            result = _attempt_task_diagnosis(
                "test-task", task_config, "disabled", {},
            )
            assert result is False

    def test_recurring_context_prepended_to_prompt(self, tmp_path):
        """With recurring_fix_failures > 0, prompt includes RECURRING FAILURE warning."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        task_config = {
            "name": "Clawdbot Sync",
            "command": "/bin/bash",
            "args": ["sync.sh"],
            "working_dir": "/tmp/clawdbot",
            "schedule": "hourly :22",
        }

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Switched to StartInterval"},
        })
        mock_proc.stderr = ""

        healthy_status = {"installed": True, "running": False, "exit_code": 0, "disabled": False, "status": "idle"}
        mock_state = MockTaskState(task_name="clawdbot-sync", last_run="2026-04-06T10:00:00")

        learning_state = {"version": 1, "agents": {}, "tasks": {
            "clawdbot-sync": {"total_diagnose_invocations": 15}
        }}

        with patch("subprocess.run", return_value=mock_proc) as mock_run, \
             patch("lib.heartbeat._get_launchd_status", return_value=healthy_status), \
             patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._load_learning_state", return_value=learning_state), \
             patch("lib.heartbeat._update_task_learning"), \
             patch("lib.heartbeat.PROJECT_ROOT", tmp_path), \
             patch("lib.heartbeat.LEARNING_FILE", state_dir / "_learning.json"), \
             patch("lib.heartbeat.log"):
            from lib.heartbeat import _attempt_task_diagnosis

            diagnosis_state = {
                "clawdbot-sync": {
                    "recovery_attempts": 5,
                    "recurring_fix_failures": 3,
                    "last_fix_summary": "stale trigger re-bootstrapped",
                },
            }
            result = _attempt_task_diagnosis(
                "clawdbot-sync", task_config,
                "stale (2h overdue)",
                diagnosis_state,
            )

            assert result is True
            cmd = mock_run.call_args[0][0]
            prompt = cmd[-1]
            assert "RECURRING FAILURE" in prompt
            assert "DIFFERENT approach" in prompt
            assert "3 time(s)" in prompt
            assert "stale trigger re-bootstrapped" in prompt

    def test_no_recurring_context_at_zero_failures(self, tmp_path):
        """With recurring_fix_failures == 0, no RECURRING FAILURE prefix."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        task_config = {
            "name": "Test Task",
            "command": "/bin/echo",
            "working_dir": "/tmp",
            "schedule": "daily 7:00",
        }

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Fixed"},
        })
        mock_proc.stderr = ""

        healthy_status = {"installed": True, "running": False, "exit_code": 0, "disabled": False, "status": "idle"}
        mock_state = MockTaskState(task_name="test-task")

        with patch("subprocess.run", return_value=mock_proc) as mock_run, \
             patch("lib.heartbeat._get_launchd_status", return_value=healthy_status), \
             patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._load_learning_state", return_value={"version": 1, "agents": {}, "tasks": {}}), \
             patch("lib.heartbeat._update_task_learning"), \
             patch("lib.heartbeat.PROJECT_ROOT", tmp_path), \
             patch("lib.heartbeat.LEARNING_FILE", state_dir / "_learning.json"), \
             patch("lib.heartbeat.log"):
            from lib.heartbeat import _attempt_task_diagnosis

            diagnosis_state = {"test-task": {"recovery_attempts": 1, "recurring_fix_failures": 0}}
            _attempt_task_diagnosis("test-task", task_config, "stale", diagnosis_state)

            cmd = mock_run.call_args[0][0]
            prompt = cmd[-1]
            assert "RECURRING FAILURE" not in prompt


class TestTaskDiagnosisLearning:
    """Tests for learning state updates from task diagnosis."""

    def _make_empty_learning_state(self):
        return {"version": 1, "agents": {}, "tasks": {}}

    def test_diagnosed_fixed_records_to_learning(self):
        """diagnosed_fixed outcome creates diagnoses array entry."""
        learning_state = self._make_empty_learning_state()

        with patch("lib.heartbeat._load_learning_state", return_value=learning_state), \
             patch("lib.heartbeat._save_learning_state") as mock_save:
            from lib.heartbeat import _update_task_learning

            _update_task_learning("clawdbot-sync", "diagnosed_fixed", {
                "problem": "stale",
                "summary": "Removed nested .git from darwin-derby/morning-brief/",
            })

            mock_save.assert_called_once()
            saved = mock_save.call_args[0][0]
            task = saved["tasks"]["clawdbot-sync"]
            assert task.get("total_diagnose_invocations") == 1
            assert task.get("total_diagnose_fixes") == 1
            assert len(task.get("diagnoses", [])) == 1
            assert "nested .git" in task["diagnoses"][0]["summary"]

    def test_diagnosed_failed_records_pattern(self):
        """diagnosed_failed outcome records error pattern."""
        learning_state = self._make_empty_learning_state()

        with patch("lib.heartbeat._load_learning_state", return_value=learning_state), \
             patch("lib.heartbeat._save_learning_state") as mock_save:
            from lib.heartbeat import _update_task_learning

            _update_task_learning("test-task", "diagnosed_failed", {
                "error_pattern": "LLM exit 1",
            })

            mock_save.assert_called_once()
            saved = mock_save.call_args[0][0]
            task = saved["tasks"]["test-task"]
            assert task.get("total_diagnose_invocations") == 1
            assert task.get("total_diagnose_fixes", 0) == 0
            assert "LLM exit 1" in task["failure_patterns"]


class TestHeartbeatTaskDiagnosisIntegration:
    """Tests for task diagnosis integration in run_heartbeat()."""

    def test_stale_task_gets_llm_diagnosis_after_threshold(self, sample_config):
        """After 2+ consecutive stale detections, LLM diagnosis is invoked."""
        old_time = (datetime.now() - timedelta(hours=48)).isoformat()
        mock_state = MockTaskState(
            task_name="uce",
            last_run=old_time,
            last_status="success",
        )

        # Pre-seed diagnosis state with 1 prior detection (so this is the 2nd)
        pre_diagnosis_state = {
            "uce": {
                "consecutive_detections": 1,
                "first_detected": old_time,
                "last_detected": old_time,
                "detected_problem": "stale",
                "diagnosis_attempted": False,
                "last_diagnosis": None,
                "recovery_attempts": 1,
            },
        }

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False), \
             patch("lib.heartbeat.detect_disabled_agents", return_value=[]), \
             patch("lib.heartbeat.detect_stale_triggers", return_value=[]), \
             patch("lib.heartbeat._load_monitoring_down_state", return_value={}), \
             patch("lib.heartbeat._save_monitoring_down_state"), \
             patch("lib.heartbeat._load_task_diagnosis_state", return_value=pre_diagnosis_state), \
             patch("lib.heartbeat._save_task_diagnosis_state"), \
             patch("lib.heartbeat._attempt_task_diagnosis", return_value=True) as mock_diagnose, \
             patch("lib.heartbeat._load_heartbeat_alert_state", return_value={}), \
             patch("lib.heartbeat._save_heartbeat_alert_state"), \
             patch("lib.heartbeat._send_missed_task_notification"), \
             patch("lib.heartbeat._send_slack_message"), \
             patch("lib.heartbeat.log"):
            from lib.heartbeat import run_heartbeat

            result = run_heartbeat(sample_config, wake_delay=0)

            # LLM diagnosis should have been attempted for the stale task
            mock_diagnose.assert_called_once()
            call_args = mock_diagnose.call_args
            assert call_args[0][0] == "uce"  # task_id
            assert "stale" in call_args[0][2]  # detected_problem

    def test_disabled_regular_task_uses_task_diagnosis(self, sample_config):
        """Disabled regular task (not monitoring) uses _attempt_task_diagnosis."""
        agent_info = {
            "task_id": "uce",
            "label": "com.pinglet.uce",
            "exit_code": 78,
            "status": "disabled",
        }

        recent_time = datetime.now() - timedelta(hours=1)
        mock_state = MockTaskState(
            task_name="uce",
            last_run=recent_time.isoformat(),
            last_status="success",
        )

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False), \
             patch("lib.heartbeat.detect_disabled_agents", return_value=[agent_info]), \
             patch("lib.heartbeat._attempt_auto_recovery", return_value="failed"), \
             patch("lib.heartbeat._attempt_task_diagnosis", return_value=True) as mock_task_diag, \
             patch("lib.heartbeat._attempt_llm_self_diagnosis") as mock_self_diag, \
             patch("lib.heartbeat._load_monitoring_down_state", return_value={}), \
             patch("lib.heartbeat._save_monitoring_down_state"), \
             patch("lib.heartbeat._record_monitoring_detection", return_value={"uce": {"consecutive_detections": 1, "recovery_attempts": 0}}), \
             patch("lib.heartbeat._load_learning_state", return_value={"version": 1, "agents": {}, "tasks": {}}), \
             patch("lib.heartbeat._update_learning_state"), \
             patch("lib.heartbeat._should_alert_monitoring_down", return_value=False), \
             patch("lib.heartbeat._load_task_diagnosis_state", return_value={}), \
             patch("lib.heartbeat._save_task_diagnosis_state"), \
             patch("lib.heartbeat._load_heartbeat_alert_state", return_value={}), \
             patch("lib.heartbeat._save_heartbeat_alert_state"), \
             patch("lib.heartbeat._send_missed_task_notification"), \
             patch("lib.heartbeat._send_slack_message"), \
             patch("lib.heartbeat.log"):
            from lib.heartbeat import run_heartbeat

            run_heartbeat(sample_config, wake_delay=0)

            # Should use task diagnosis for regular task, NOT monitoring self-diagnosis
            mock_task_diag.assert_called_once()
            mock_self_diag.assert_not_called()

    def test_task_diagnose_prompt_has_required_vars(self):
        """TASK_DIAGNOSE_PROMPT contains all essential template variables."""
        from lib.heartbeat import TASK_DIAGNOSE_PROMPT

        required_vars = [
            "{task_id}", "{task_name}", "{detected_problem}",
            "{working_dir}", "{schedule}", "{last_run}",
            "{launchd_status}", "{recovery_attempts}",
            "{learning_file}",
        ]
        for var in required_vars:
            assert var in TASK_DIAGNOSE_PROMPT, f"Missing {var} in TASK_DIAGNOSE_PROMPT"
