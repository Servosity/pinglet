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

    def test_self_diagnosis_success_resets_state(self):
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

        with patch("subprocess.run", return_value=mock_proc) as mock_run, \
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
