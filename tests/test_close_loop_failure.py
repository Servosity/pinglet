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
        mock_result.stdout = '{"result": "Fixed the issue by restarting the service"}'
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
