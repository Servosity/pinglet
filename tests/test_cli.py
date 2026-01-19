"""Tests for CLI commands."""
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


class TestRunNowCommand:
    """Tests for --run-now CLI command."""

    def test_run_now_executes_task(self, sample_config):
        """Test --run-now executes the specified task."""
        old_time = datetime.now() - timedelta(hours=20)

        mock_state = MockTaskState(
            task_name="uce",
            last_run=old_time.isoformat(),
            last_status="success",
        )

        with patch("pinglet.load_config", return_value=sample_config), \
             patch("pinglet.should_run_task", return_value=(True, "missed")), \
             patch("pinglet.clear_ignored"), \
             patch("pinglet.run_task", return_value=0) as mock_run, \
             patch("pinglet.send_manual_complete_notification"):
            from pinglet import run_now

            result = run_now("uce", sample_config)

            mock_run.assert_called_once_with("uce")
            assert result == 0

    def test_run_now_skips_if_already_ran(self, sample_config):
        """Test --run-now skips if task already ran recently."""
        with patch("pinglet.load_config", return_value=sample_config), \
             patch("pinglet.should_run_task", return_value=(False, "already ran at 7:00pm")), \
             patch("pinglet.run_task") as mock_run, \
             patch("pinglet.send_macos_notification") as mock_notify:
            from pinglet import run_now

            result = run_now("uce", sample_config)

            mock_run.assert_not_called()
            mock_notify.assert_called()
            assert result == 0

    def test_run_now_clears_ignored(self, sample_config):
        """Test --run-now clears ignored status before execution."""
        with patch("pinglet.load_config", return_value=sample_config), \
             patch("pinglet.should_run_task", return_value=(True, "missed")), \
             patch("pinglet.clear_ignored") as mock_clear, \
             patch("pinglet.run_task", return_value=0), \
             patch("pinglet.send_manual_complete_notification"):
            from pinglet import run_now

            run_now("uce", sample_config)

            mock_clear.assert_called_once_with("uce")


class TestIgnoreCommand:
    """Tests for --ignore CLI command."""

    def test_ignore_marks_task(self, sample_config):
        """Test --ignore adds task to ignored list."""
        old_time = datetime.now() - timedelta(hours=20)

        mock_state = MockTaskState(
            task_name="uce",
            last_run=old_time.isoformat(),
        )

        with patch("pinglet.load_config", return_value=sample_config), \
             patch("pinglet.load_state", return_value=mock_state), \
             patch("pinglet.mark_ignored") as mock_ignore:
            from pinglet import ignore_task

            result = ignore_task("uce", sample_config)

            mock_ignore.assert_called_once()
            assert result == 0

    def test_ignore_unknown_task(self, sample_config):
        """Test --ignore returns error for unknown task."""
        with patch("pinglet.load_config", return_value=sample_config):
            from pinglet import ignore_task

            result = ignore_task("nonexistent-task", sample_config)

            assert result == 1


class TestHeartbeatCommand:
    """Tests for --heartbeat CLI command."""

    def test_heartbeat_runs_detection(self, sample_config):
        """Test --heartbeat runs missed task detection."""
        with patch("pinglet.load_config", return_value=sample_config), \
             patch("pinglet.heartbeat_run") as mock_heartbeat:
            mock_heartbeat.return_value = {"missed_count": 0, "tasks": []}

            from pinglet import run_heartbeat_command

            run_heartbeat_command(sample_config)

            mock_heartbeat.assert_called_once_with(sample_config)

    def test_heartbeat_returns_1_for_missed(self, sample_config):
        """Test --heartbeat returns 1 when tasks are missed."""
        with patch("pinglet.load_config", return_value=sample_config), \
             patch("pinglet.heartbeat_run") as mock_heartbeat:
            mock_heartbeat.return_value = {"missed_count": 2, "tasks": []}

            from pinglet import run_heartbeat_command

            result = run_heartbeat_command(sample_config)

            assert result == 1


class TestInstallHeartbeatCommand:
    """Tests for --install-heartbeat CLI command."""

    def test_install_generates_plist(self, tmp_path):
        """Test --install-heartbeat generates valid plist."""
        launchagents_dir = tmp_path / "launchagents"
        launchagents_dir.mkdir()
        user_launchagents = tmp_path / "Library" / "LaunchAgents"
        user_launchagents.mkdir(parents=True)

        with patch("pinglet.LAUNCHAGENTS_DIR", launchagents_dir), \
             patch("pinglet.USER_LAUNCHAGENTS_DIR", user_launchagents), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            from pinglet import install_heartbeat

            install_heartbeat()

            plist_file = launchagents_dir / "com.pinglet.heartbeat.plist"
            assert plist_file.exists()

            content = plist_file.read_text()
            assert "com.pinglet.heartbeat" in content
            assert "--heartbeat" in content
            assert "StartInterval" in content

    def test_install_copies_to_launch_agents(self, tmp_path):
        """Test --install-heartbeat copies plist to ~/Library/LaunchAgents."""
        launchagents_dir = tmp_path / "launchagents"
        launchagents_dir.mkdir()
        user_launchagents = tmp_path / "Library" / "LaunchAgents"
        user_launchagents.mkdir(parents=True)

        with patch("pinglet.LAUNCHAGENTS_DIR", launchagents_dir), \
             patch("pinglet.USER_LAUNCHAGENTS_DIR", user_launchagents), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            from pinglet import install_heartbeat

            install_heartbeat()

            target_plist = user_launchagents / "com.pinglet.heartbeat.plist"
            assert target_plist.exists()

    def test_install_loads_launchctl(self, tmp_path):
        """Test --install-heartbeat runs launchctl load."""
        launchagents_dir = tmp_path / "launchagents"
        launchagents_dir.mkdir()
        user_launchagents = tmp_path / "Library" / "LaunchAgents"
        user_launchagents.mkdir(parents=True)

        with patch("pinglet.LAUNCHAGENTS_DIR", launchagents_dir), \
             patch("pinglet.USER_LAUNCHAGENTS_DIR", user_launchagents), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            from pinglet import install_heartbeat

            install_heartbeat()

            # Check launchctl load was called
            calls = [str(c) for c in mock_run.call_args_list]
            assert any("launchctl" in str(c) and "load" in str(c) for c in calls)


class TestUninstallHeartbeatCommand:
    """Tests for --uninstall-heartbeat CLI command."""

    def test_uninstall_removes_plist(self, tmp_path):
        """Test --uninstall-heartbeat removes plist and unloads."""
        user_launchagents = tmp_path / "Library" / "LaunchAgents"
        user_launchagents.mkdir(parents=True)

        plist_file = user_launchagents / "com.pinglet.heartbeat.plist"
        plist_file.write_text("<plist></plist>")

        with patch("pinglet.USER_LAUNCHAGENTS_DIR", user_launchagents), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            from pinglet import uninstall_heartbeat

            uninstall_heartbeat()

            assert not plist_file.exists()

            # Check launchctl unload was called
            calls = [str(c) for c in mock_run.call_args_list]
            assert any("launchctl" in str(c) and "unload" in str(c) for c in calls)

    def test_uninstall_not_installed(self, tmp_path):
        """Test --uninstall-heartbeat handles not installed case."""
        user_launchagents = tmp_path / "Library" / "LaunchAgents"
        user_launchagents.mkdir(parents=True)
        # No plist file exists

        with patch("pinglet.USER_LAUNCHAGENTS_DIR", user_launchagents):
            from pinglet import uninstall_heartbeat

            result = uninstall_heartbeat()

            assert result == 0
