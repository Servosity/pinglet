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
    total_failures: int = 0


# =============================================================================
# on_failure callback tests (1-6)
# =============================================================================


class TestOnFailureNotInvoked:
    """Tests for on_failure callback gating."""

    def test_on_failure_not_invoked_when_not_configured(self):
        """No on_failure in config -> callback returns invoked=False."""
        task_config = {
            "name": "UCE Link Collector",
            "command": "/usr/bin/python3",
            "args": ["runner.py"],
            "working_dir": "/tmp/uce",
            # No on_failure key
        }

        from pinglet import _run_on_failure_callback

        result = _run_on_failure_callback(
            task_name="uce",
            task_config=task_config,
            exit_code=1,
            error="Connection refused",
            consecutive_failures=5,
        )

        assert result["invoked"] is False
        assert result["exit_code"] is None

    def test_on_failure_not_invoked_below_threshold(self):
        """Below alert threshold -> callback never called by run_task.

        The threshold gating happens in run_task (only calls _run_on_failure_callback
        inside the `if should_alert:` block). We verify that by checking the
        run_task flow does not invoke the callback when below threshold.
        """
        with patch("pinglet.load_config") as mock_config, \
             patch("subprocess.run") as mock_subprocess, \
             patch("pinglet._run_on_failure_callback") as mock_callback, \
             patch("pinglet._check_monitoring_agents", return_value=[]), \
             patch("pinglet.load_state") as mock_load_state, \
             patch("pinglet.update_state_failure"), \
             patch("pinglet.log_run_start"), \
             patch("pinglet.log_run_end"), \
             patch("pinglet.log"):
            mock_config.return_value = {
                "tasks": {
                    "uce": {
                        "name": "UCE Link Collector",
                        "command": "/usr/bin/echo",
                        "args": ["fail"],
                        "working_dir": "/tmp",
                        "on_failure": {
                            "command": "claude",
                            "args": ["-p", "Fix {task_id}"],
                            "timeout": 180,
                        },
                        "reliability": {
                            "alert": {
                                "consecutive_failures": 5,
                            },
                        },
                    },
                },
                "reliability": {
                    "retry": {"max_attempts": 1, "delays_seconds": [0], "jitter": 0},
                    "alert": {"consecutive_failures": 5, "cooldown_minutes": 30},
                    "notify_on_recovery": True,
                },
            }
            # Task fails
            mock_proc = MagicMock()
            mock_proc.returncode = 1
            mock_proc.stdout = ""
            mock_proc.stderr = "error"
            mock_subprocess.return_value = mock_proc

            # Only 1 consecutive failure -- below threshold of 5
            mock_state = MagicMock()
            mock_state.consecutive_failures = 1
            mock_load_state.return_value = mock_state

            from pinglet import run_task
            run_task("uce")

            # Callback should NOT have been called because we're below threshold
            mock_callback.assert_not_called()


class TestOnFailureInvocation:
    """Tests for on_failure callback execution."""

    def test_on_failure_invoked_after_threshold(self, tmp_path):
        """on_failure configured + above threshold -> subprocess called."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        task_config = {
            "name": "UCE Link Collector",
            "command": "/usr/bin/python3",
            "args": ["runner.py"],
            "working_dir": "/tmp/uce",
            "on_failure": {
                "command": "claude",
                "args": ["-p", "Task {task_id} failed with exit code {exit_code}"],
                "timeout": 180,
            },
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Fixed the issue by restarting the service"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run, \
             patch("pinglet.PROJECT_ROOT", tmp_path), \
             patch("lib.heartbeat._update_task_learning"):
            from pinglet import _run_on_failure_callback

            result = _run_on_failure_callback(
                task_name="uce",
                task_config=task_config,
                exit_code=1,
                error="Connection refused",
                consecutive_failures=5,
            )

            assert result["invoked"] is True
            assert result["exit_code"] == 0
            mock_run.assert_called_once()

    def test_on_failure_template_substitution(self, tmp_path):
        """All {vars} replaced including {learning_file}."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        task_config = {
            "name": "UCE Link Collector",
            "command": "/usr/bin/python3",
            "args": ["runner.py"],
            "working_dir": "/tmp/uce",
            "on_failure": {
                "command": "echo",  # Not claude, to avoid extra flags
                "args": [
                    "Task {task_id} named {task_name} failed with exit {exit_code}. "
                    "Error: {error}. Log: {log_file}. Stderr: {stderr_file}. "
                    "Stdout: {stdout_file}. Dir: {working_dir}. "
                    "Failures: {consecutive_failures}. State: {state_file}. "
                    "Root: {project_root}. Learning: {learning_file}",
                ],
                "timeout": 180,
            },
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Diagnosed"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run, \
             patch("pinglet.PROJECT_ROOT", tmp_path), \
             patch("lib.heartbeat._update_task_learning"):
            from pinglet import _run_on_failure_callback

            result = _run_on_failure_callback(
                task_name="uce",
                task_config=task_config,
                exit_code=1,
                error="Connection refused",
                consecutive_failures=5,
            )

            assert result["invoked"] is True
            # Verify the actual command passed to subprocess.run
            call_args = mock_run.call_args
            cmd = call_args[0][0] if call_args[0] else call_args[1].get("args", [])
            cmd_str = " ".join(str(c) for c in cmd)

            # All template variables should be resolved (no {var} remaining)
            assert "{task_id}" not in cmd_str
            assert "{task_name}" not in cmd_str
            assert "{exit_code}" not in cmd_str
            assert "{error}" not in cmd_str
            assert "{learning_file}" not in cmd_str
            assert "{consecutive_failures}" not in cmd_str
            assert "{project_root}" not in cmd_str

            # Specific values should be present
            assert "uce" in cmd_str
            assert "Connection refused" in cmd_str
            assert "_learning.json" in cmd_str

    def test_on_failure_timeout_respected(self, tmp_path):
        """subprocess.run called with timeout from config."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        task_config = {
            "name": "UCE Link Collector",
            "command": "/usr/bin/python3",
            "args": ["runner.py"],
            "working_dir": "/tmp/uce",
            "on_failure": {
                "command": "echo",
                "args": ["Fix {task_id}"],
                "timeout": 240,
            },
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Fixed"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run, \
             patch("pinglet.PROJECT_ROOT", tmp_path), \
             patch("lib.heartbeat._update_task_learning"):
            from pinglet import _run_on_failure_callback

            _run_on_failure_callback(
                task_name="uce",
                task_config=task_config,
                exit_code=1,
                error="Timeout",
                consecutive_failures=5,
            )

            # Verify timeout was passed to subprocess.run
            call_kwargs = mock_run.call_args[1] if mock_run.call_args[1] else {}
            assert call_kwargs.get("timeout") == 240

    def test_on_failure_result_logged(self, tmp_path):
        """Output written to logs/{task_id}-on_failure.log."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        task_config = {
            "name": "UCE Link Collector",
            "command": "/usr/bin/python3",
            "args": ["runner.py"],
            "working_dir": "/tmp/uce",
            "on_failure": {
                "command": "echo",
                "args": ["Fix {task_id}"],
                "timeout": 180,
            },
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Applied fix: restarted service"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run, \
             patch("pinglet.PROJECT_ROOT", tmp_path), \
             patch("lib.heartbeat._update_task_learning"):
            from pinglet import _run_on_failure_callback

            result = _run_on_failure_callback(
                task_name="uce",
                task_config=task_config,
                exit_code=1,
                error="Connection refused",
                consecutive_failures=5,
            )

            assert result["invoked"] is True
            assert result["log_file"] is not None

            # Verify a log file was created in the logs dir
            log_path = Path(result["log_file"])
            assert log_path.exists()
            assert "uce" in log_path.name
            assert "on_failure" in log_path.name

            log_content = log_path.read_text()
            assert "Applied fix" in log_content


# =============================================================================
# Notification tests (7-9)
# =============================================================================


class TestNotificationLLMStatus:
    """Tests for LLM status in notifications."""

    def test_notification_includes_llm_status(self):
        """Slack message includes 'LLM Troubleshooter: Invoked' section."""
        with patch("lib.alerts.send_slack_message") as mock_slack, \
             patch("subprocess.run") as mock_run:
            mock_slack.return_value = True
            mock_run.return_value = MagicMock(returncode=0)

            from lib.alerts import send_critical

            llm_result = {
                "invoked": True,
                "exit_code": 0,
                "output": "Fixed by restarting",
            }

            send_critical(
                task_name="UCE",
                error="Task failed",
                details={"Exit code": 1},
                log_file="/path/to/log",
                task_id="uce",
                llm_result=llm_result,
            )

            mock_slack.assert_called_once()
            message = mock_slack.call_args[0][0]
            assert "LLM Troubleshooter" in message
            assert "Invoked" in message

    def test_notification_says_human_required_when_no_callback(self):
        """Message says 'Not configured -- human intervention required'."""
        with patch("lib.alerts.send_slack_message") as mock_slack, \
             patch("subprocess.run") as mock_run:
            mock_slack.return_value = True
            mock_run.return_value = MagicMock(returncode=0)

            from lib.alerts import send_critical

            # llm_result=None means no on_failure configured
            send_critical(
                task_name="UCE",
                error="Task failed",
                details={"Exit code": 1},
                log_file="/path/to/log",
                task_id="uce",
                llm_result=None,
            )

            mock_slack.assert_called_once()
            message = mock_slack.call_args[0][0]
            assert "human intervention required" in message.lower() or "Not configured" in message

    def test_notification_says_llm_investigating(self):
        """Message says 'Invoked (exit 0 -- may be fixed)'."""
        with patch("lib.alerts.send_slack_message") as mock_slack, \
             patch("subprocess.run") as mock_run:
            mock_slack.return_value = True
            mock_run.return_value = MagicMock(returncode=0)

            from lib.alerts import send_critical

            llm_result = {
                "invoked": True,
                "exit_code": 0,
                "output": "Restarted the service",
            }

            send_critical(
                task_name="UCE",
                error="Task failed",
                details={"Exit code": 1},
                log_file="/path/to/log",
                task_id="uce",
                llm_result=llm_result,
            )

            mock_slack.assert_called_once()
            message = mock_slack.call_args[0][0]
            # Should indicate success and possible fix
            assert "exit 0" in message or "may be fixed" in message.lower()


# =============================================================================
# Self-diagnosis tests (10-12)
# =============================================================================


class TestSelfDiagnosis:
    """Tests for LLM self-diagnosis of infrastructure."""

    def test_self_diagnosis_invoked_after_recovery_fails(self, sample_config):
        """_attempt_llm_self_diagnosis called when auto_recovery returns False.

        run_heartbeat flow: detect disabled -> auto-recovery fails -> LLM diagnosis.
        """
        agent_info = {
            "task_id": "heartbeat",
            "label": "com.pinglet.heartbeat",
            "exit_code": 78,
            "status": "disabled",
        }

        recent_time = datetime.now() - timedelta(hours=1)
        mock_state = MockTaskState(
            task_name="any",
            last_run=recent_time.isoformat(),
            last_status="success",
        )

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False), \
             patch("lib.heartbeat.detect_disabled_agents", return_value=[agent_info]), \
             patch("lib.heartbeat._attempt_auto_recovery", return_value=False) as mock_recovery, \
             patch("lib.heartbeat._attempt_llm_self_diagnosis", return_value=False) as mock_llm, \
             patch("lib.heartbeat._load_monitoring_down_state", return_value={}), \
             patch("lib.heartbeat._save_monitoring_down_state"), \
             patch("lib.heartbeat._record_monitoring_detection", return_value={"heartbeat": {"consecutive_detections": 1, "recovery_attempts": 0}}), \
             patch("lib.heartbeat._load_learning_state", return_value={"version": 1, "agents": {}, "tasks": {}}), \
             patch("lib.heartbeat._update_learning_state"), \
             patch("lib.heartbeat._should_alert_monitoring_down", return_value=False), \
             patch("lib.heartbeat._send_missed_task_notification"), \
             patch("lib.heartbeat._send_slack_message"), \
             patch("lib.heartbeat._load_heartbeat_alert_state", return_value={}), \
             patch("lib.heartbeat._save_heartbeat_alert_state"):
            from lib.heartbeat import run_heartbeat

            run_heartbeat(sample_config, wake_delay=0)

            # Auto-recovery should be attempted first
            mock_recovery.assert_called_once_with("heartbeat")
            # Then LLM diagnosis should be attempted after auto-recovery fails
            mock_llm.assert_called_once_with("heartbeat", agent_info)

    def test_self_diagnosis_success_resets_state(self, tmp_path):
        """LLM returns exit 0 + agent healthy -> returns True."""
        agent_id = "heartbeat"
        status = {
            "task_id": "heartbeat",
            "status": "disabled",
            "exit_code": 78,
        }

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Fixed: re-enabled the agent via launchctl bootstrap"
        mock_proc.stderr = ""

        # After LLM fix, agent should be healthy
        healthy_status = {
            "installed": True,
            "running": False,
            "exit_code": 0,
            "disabled": False,
            "status": "idle",
        }

        with patch("lib.heartbeat.PROJECT_ROOT", tmp_path), \
             patch("subprocess.run", return_value=mock_proc) as mock_run, \
             patch("lib.heartbeat._get_launchd_status", return_value=healthy_status), \
             patch("lib.heartbeat.log"):
            from lib.heartbeat import _attempt_llm_self_diagnosis

            result = _attempt_llm_self_diagnosis(agent_id, status)

            assert result is True
            mock_run.assert_called_once()

    def test_self_diagnosis_prompt_references_learning_file(self):
        """Prompt includes reference to state/_learning.json, NOT inlined content."""
        from lib.heartbeat import SELF_DIAGNOSIS_PROMPT

        # The prompt should reference the learning file path, not embed its contents
        assert "_learning.json" in SELF_DIAGNOSIS_PROMPT
        # Should reference it as a path to read
        assert "state/_learning.json" in SELF_DIAGNOSIS_PROMPT


# =============================================================================
# Learning loop tests (13-21)
# =============================================================================


class TestLearningRecords:
    """Tests for learning state recording."""

    def _make_empty_learning_state(self):
        """Create an empty learning state dict."""
        return {
            "version": 1,
            "agents": {},
            "tasks": {},
        }

    def test_learning_records_auto_recovery(self):
        """total_auto_recoveries increments after successful auto-recovery."""
        learning_state = self._make_empty_learning_state()
        learning_state["agents"]["heartbeat"] = {
            "pattern": "unknown",
            "total_detections": 3,
            "total_auto_recoveries": 3,
            "total_llm_recoveries": 0,
            "total_human_alerts": 0,
            "consecutive_auto_recoveries": 3,
            "effective_threshold": 3,
            "last_detection": "2026-03-17T06:00:00",
            "known_issues": [],
            "suppressed": False,
            "suppressed_reason": None,
        }

        with patch("lib.heartbeat._load_learning_state", return_value=learning_state), \
             patch("lib.heartbeat._save_learning_state") as mock_save:
            from lib.heartbeat import _update_learning_state

            _update_learning_state("heartbeat", "auto_recovery", {"method": "disable+enable"})

            mock_save.assert_called_once()
            saved_state = mock_save.call_args[0][0]
            agent = saved_state["agents"]["heartbeat"]
            assert agent["total_auto_recoveries"] == 4
            assert agent["total_detections"] == 4
            assert agent["consecutive_auto_recoveries"] == 4

    def test_learning_detects_chronic_pattern(self):
        """10+ auto-recoveries at 100% rate -> pattern set to 'chronic_cycle'."""
        learning_state = self._make_empty_learning_state()
        learning_state["agents"]["heartbeat"] = {
            "pattern": "unknown",
            "total_detections": 9,
            "total_auto_recoveries": 9,
            "total_llm_recoveries": 0,
            "total_human_alerts": 0,
            "consecutive_auto_recoveries": 9,
            "effective_threshold": 3,
            "last_detection": "2026-03-17T06:00:00",
            "known_issues": [],
            "suppressed": False,
            "suppressed_reason": None,
        }

        with patch("lib.heartbeat._load_learning_state", return_value=learning_state), \
             patch("lib.heartbeat._save_learning_state") as mock_save:
            from lib.heartbeat import _update_learning_state

            # This is the 10th auto-recovery, making total_detections=10, total_auto_recoveries=10
            _update_learning_state("heartbeat", "auto_recovery", {"method": "disable+enable"})

            mock_save.assert_called_once()
            saved_state = mock_save.call_args[0][0]
            agent = saved_state["agents"]["heartbeat"]
            assert agent["pattern"] == "chronic_cycle"
            assert agent["consecutive_auto_recoveries"] == 10

    def test_chronic_pattern_raises_threshold(self):
        """effective_threshold increases (to >= 10) after chronic detection."""
        learning_state = self._make_empty_learning_state()
        learning_state["agents"]["heartbeat"] = {
            "pattern": "unknown",
            "total_detections": 9,
            "total_auto_recoveries": 9,
            "total_llm_recoveries": 0,
            "total_human_alerts": 0,
            "consecutive_auto_recoveries": 9,
            "effective_threshold": 3,
            "last_detection": "2026-03-17T06:00:00",
            "known_issues": [],
            "suppressed": False,
            "suppressed_reason": None,
        }

        with patch("lib.heartbeat._load_learning_state", return_value=learning_state), \
             patch("lib.heartbeat._save_learning_state") as mock_save:
            from lib.heartbeat import _update_learning_state

            _update_learning_state("heartbeat", "auto_recovery", {"method": "disable+enable"})

            mock_save.assert_called_once()
            saved_state = mock_save.call_args[0][0]
            agent = saved_state["agents"]["heartbeat"]
            # Threshold should increase for chronic cyclers
            assert agent["effective_threshold"] > 3
            assert agent["effective_threshold"] >= 10

    def test_chronic_pattern_suppresses_alert(self):
        """suppressed=True after chronic pattern detected."""
        learning_state = self._make_empty_learning_state()
        learning_state["agents"]["heartbeat"] = {
            "pattern": "unknown",
            "total_detections": 9,
            "total_auto_recoveries": 9,
            "total_llm_recoveries": 0,
            "total_human_alerts": 0,
            "consecutive_auto_recoveries": 9,
            "effective_threshold": 3,
            "last_detection": "2026-03-17T06:00:00",
            "known_issues": [],
            "suppressed": False,
            "suppressed_reason": None,
        }

        with patch("lib.heartbeat._load_learning_state", return_value=learning_state), \
             patch("lib.heartbeat._save_learning_state") as mock_save:
            from lib.heartbeat import _update_learning_state

            _update_learning_state("heartbeat", "auto_recovery", {"method": "disable+enable"})

            mock_save.assert_called_once()
            saved_state = mock_save.call_args[0][0]
            agent = saved_state["agents"]["heartbeat"]
            assert agent["suppressed"] is True
            assert agent["suppressed_reason"] is not None

    def test_learning_records_llm_fix(self):
        """total_llm_recoveries increments after LLM recovery."""
        learning_state = self._make_empty_learning_state()
        learning_state["agents"]["heartbeat"] = {
            "pattern": "unknown",
            "total_detections": 5,
            "total_auto_recoveries": 4,
            "total_llm_recoveries": 0,
            "total_human_alerts": 0,
            "consecutive_auto_recoveries": 0,
            "effective_threshold": 3,
            "last_detection": "2026-03-17T06:00:00",
            "known_issues": [],
            "suppressed": False,
            "suppressed_reason": None,
        }

        with patch("lib.heartbeat._load_learning_state", return_value=learning_state), \
             patch("lib.heartbeat._save_learning_state") as mock_save:
            from lib.heartbeat import _update_learning_state

            _update_learning_state(
                "heartbeat",
                "llm_recovery",
                {"summary": "Re-enabled agent via launchctl bootstrap"},
            )

            mock_save.assert_called_once()
            saved_state = mock_save.call_args[0][0]
            agent = saved_state["agents"]["heartbeat"]
            assert agent["total_llm_recoveries"] == 1
            assert agent["total_detections"] == 6
            # LLM recovery should reset the consecutive auto-recovery counter
            assert agent["consecutive_auto_recoveries"] == 0

    def test_known_issue_added_after_llm_success(self):
        """known_issues[] gets entry after LLM fix via summary field."""
        learning_state = self._make_empty_learning_state()
        learning_state["agents"]["heartbeat"] = {
            "pattern": "unknown",
            "total_detections": 5,
            "total_auto_recoveries": 4,
            "total_llm_recoveries": 0,
            "total_human_alerts": 0,
            "consecutive_auto_recoveries": 0,
            "effective_threshold": 3,
            "last_detection": "2026-03-17T06:00:00",
            "known_issues": [],
            "suppressed": False,
            "suppressed_reason": None,
        }

        with patch("lib.heartbeat._load_learning_state", return_value=learning_state), \
             patch("lib.heartbeat._save_learning_state") as mock_save:
            from lib.heartbeat import _update_learning_state

            _update_learning_state(
                "heartbeat",
                "llm_recovery",
                {"summary": "Fixed: plist had KeepAlive with StartCalendarInterval"},
            )

            mock_save.assert_called_once()
            saved_state = mock_save.call_args[0][0]
            agent = saved_state["agents"]["heartbeat"]
            assert len(agent["known_issues"]) > 0
            # The known issue should contain something from the LLM summary
            assert any("KeepAlive" in issue or "plist" in issue for issue in agent["known_issues"])


class TestLearningPrompt:
    """Tests for learning file reference in prompts."""

    def test_learning_file_referenced_in_prompt(self):
        """SELF_DIAGNOSIS_PROMPT contains '_learning.json' reference."""
        from lib.heartbeat import SELF_DIAGNOSIS_PROMPT

        assert "_learning.json" in SELF_DIAGNOSIS_PROMPT


class TestLearningThresholds:
    """Tests for threshold adjustment based on patterns."""

    def _make_empty_learning_state(self):
        return {
            "version": 1,
            "agents": {},
            "tasks": {},
        }

    def test_persistent_pattern_lowers_threshold(self):
        """Agent with failing recovery -> pattern='persistent' + lower effective_threshold.

        _detect_and_adapt: total>=5, human>0, auto_rate<0.5 -> persistent,
        threshold = max(2, MONITORING_ALERT_THRESHOLD - 1) = 2 (when threshold was 3).
        """
        learning_state = self._make_empty_learning_state()
        learning_state["agents"]["heartbeat"] = {
            "pattern": "unknown",
            "total_detections": 8,
            "total_auto_recoveries": 2,
            "total_llm_recoveries": 0,
            "total_human_alerts": 5,
            "consecutive_auto_recoveries": 0,
            "effective_threshold": 5,
            "last_detection": "2026-03-17T06:00:00",
            "known_issues": [],
            "suppressed": False,
            "suppressed_reason": None,
        }

        with patch("lib.heartbeat._load_learning_state", return_value=learning_state), \
             patch("lib.heartbeat._save_learning_state") as mock_save:
            from lib.heartbeat import _update_learning_state

            # Another human alert -- recovery keeps failing
            _update_learning_state(
                "heartbeat",
                "human_alert",
                {"reason": "Auto-recovery and LLM both failed"},
            )

            mock_save.assert_called_once()
            saved_state = mock_save.call_args[0][0]
            agent = saved_state["agents"]["heartbeat"]
            assert agent["pattern"] == "persistent"
            # Persistent pattern should lower the threshold to alert sooner
            assert agent["effective_threshold"] < 5


class TestLearningPersistence:
    """Tests for learning state save/load roundtrip."""

    def test_learning_state_persistence(self, tmp_path):
        """Save/load roundtrip verifies all fields survive serialization."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        learning_file = state_dir / "_learning.json"

        full_state = {
            "version": 1,
            "agents": {
                "heartbeat": {
                    "pattern": "chronic_cycle",
                    "total_detections": 47,
                    "total_auto_recoveries": 47,
                    "total_llm_recoveries": 0,
                    "total_human_alerts": 0,
                    "consecutive_auto_recoveries": 47,
                    "effective_threshold": 10,
                    "last_detection": "2026-03-17T07:49:31",
                    "known_issues": ["KeepAlive conflict with StartCalendarInterval"],
                    "suppressed": True,
                    "suppressed_reason": "chronic_cycle: 47 consecutive auto-recoveries at 100% rate",
                },
            },
            "tasks": {
                "uce": {
                    "total_failures": 5,
                    "total_on_failure_invocations": 2,
                    "total_on_failure_fixes": 1,
                    "failure_patterns": ["ConnectionError at runner.py:42"],
                    "prompt_improvements": ["Added retry logic hint"],
                },
            },
        }

        with patch("lib.heartbeat.LEARNING_FILE", learning_file), \
             patch("lib.heartbeat.state_module") as mock_state_module:
            mock_state_module.STATE_DIR = state_dir
            from lib.heartbeat import _save_learning_state, _load_learning_state

            _save_learning_state(full_state)

            # Verify file was created
            assert learning_file.exists()

            # Load it back
            loaded = _load_learning_state()

            # Verify all fields survived the roundtrip
            assert loaded["version"] == 1

            # Agent fields
            agent = loaded["agents"]["heartbeat"]
            assert agent["pattern"] == "chronic_cycle"
            assert agent["total_detections"] == 47
            assert agent["total_auto_recoveries"] == 47
            assert agent["total_llm_recoveries"] == 0
            assert agent["total_human_alerts"] == 0
            assert agent["consecutive_auto_recoveries"] == 47
            assert agent["effective_threshold"] == 10
            assert agent["last_detection"] == "2026-03-17T07:49:31"
            assert len(agent["known_issues"]) == 1
            assert "KeepAlive" in agent["known_issues"][0]
            assert agent["suppressed"] is True
            assert "chronic_cycle" in agent["suppressed_reason"]

            # Task fields
            task = loaded["tasks"]["uce"]
            assert task["total_failures"] == 5
            assert task["total_on_failure_invocations"] == 2
            assert task["total_on_failure_fixes"] == 1
            assert len(task["failure_patterns"]) == 1
            assert len(task["prompt_improvements"]) == 1


# =============================================================================
# on_diagnose / task-level diagnosis tests (22-33)
# =============================================================================


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
        mock_proc.stdout = '{"result": "Fixed broken git repo"}'
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
            # Should use claude with default prompt
            assert cmd[0] == "claude"
            assert "-p" in cmd

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
        mock_proc.stdout = "Fixed"
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
        mock_proc.stdout = '{"result": "Switched to StartInterval"}'
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
            prompt = cmd[cmd.index("-p") + 1]
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
        mock_proc.stdout = '{"result": "Fixed"}'
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
            prompt = cmd[cmd.index("-p") + 1]
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
