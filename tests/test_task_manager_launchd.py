"""Tests for task management module."""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.task_manager import (
    parse_schedule,
    schedule_to_human,
    validate_task_id,
    validate_task_config,
    generate_plist,
    estimate_expected_interval,
    get_launchd_status,
    enable_task,
    _parse_time,
    _deep_merge,
)
class TestEnableDisable:
    def test_enable_generates_plist(self, tmp_path, sample_config):
        sample_config["tasks"]["uce"]["schedule"] = "daily 7:00,19:00"
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config))
        la_dir = tmp_path / "launchagents"
        la_dir.mkdir()
        user_la_dir = tmp_path / "Library" / "LaunchAgents"
        user_la_dir.mkdir(parents=True)

        with patch("lib.task_manager.CONFIG_PATH", config_path), \
             patch("lib.task_manager.LAUNCHAGENTS_DIR", la_dir), \
             patch("lib.task_manager.USER_LAUNCHAGENTS_DIR", user_la_dir), \
             patch("subprocess.run") as mock_run, \
             patch("lib.task_manager._get_uid", return_value="501"), \
             patch("lib.task_manager.get_launchd_status", return_value={
                 "installed": True, "running": False, "exit_code": 0,
                 "disabled": False, "status": "idle",
             }):
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            result = enable_task("uce")

        assert result["ok"] is True
        assert (la_dir / "com.pinglet.uce.plist").exists()
        assert (user_la_dir / "com.pinglet.uce.plist").exists()
        # Verify plist content
        plist_content = (la_dir / "com.pinglet.uce.plist").read_text()
        assert "com.pinglet.uce" in plist_content
        assert "StartCalendarInterval" in plist_content

    def test_enable_no_schedule_fails(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config))

        with patch("lib.task_manager.CONFIG_PATH", config_path), \
             patch("lib.task_manager.is_task_enabled", return_value=False):
            from lib.task_manager import enable_task
            result = enable_task("uce")

        assert result["ok"] is False
        assert "No schedule" in result["error"]

    def test_disable_removes_plist(self, tmp_path):
        user_la_dir = tmp_path / "Library" / "LaunchAgents"
        user_la_dir.mkdir(parents=True)
        plist = user_la_dir / "com.pinglet.my-task.plist"
        plist.write_text("<plist></plist>")

        with patch("lib.task_manager.USER_LAUNCHAGENTS_DIR", user_la_dir), \
             patch("subprocess.run") as mock_run, \
             patch("lib.task_manager._get_uid", return_value="501"):
            mock_run.return_value = MagicMock(returncode=0)
            from lib.task_manager import disable_task
            result = disable_task("my-task")

        assert result["ok"] is True
        assert not plist.exists()

    def test_disable_already_disabled(self, tmp_path):
        user_la_dir = tmp_path / "Library" / "LaunchAgents"
        user_la_dir.mkdir(parents=True)

        with patch("lib.task_manager.USER_LAUNCHAGENTS_DIR", user_la_dir):
            from lib.task_manager import disable_task
            result = disable_task("nonexistent")

        assert result["ok"] is True
        assert "Already disabled" in result["note"]


class TestEnableTaskVerification:
    """Tests for post-install verification in enable_task()."""

    def _make_config(self, tmp_path, sample_config):
        sample_config["tasks"]["uce"]["schedule"] = "daily 7:00,19:00"
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config))
        la_dir = tmp_path / "launchagents"
        la_dir.mkdir()
        user_la_dir = tmp_path / "Library" / "LaunchAgents"
        user_la_dir.mkdir(parents=True)
        return config_path, la_dir, user_la_dir

    def test_enable_returns_launchd_status(self, tmp_path, sample_config):
        """enable_task() should include launchd_status in its return dict."""
        config_path, la_dir, user_la_dir = self._make_config(tmp_path, sample_config)
        launchd_status = {
            "installed": True, "running": False, "exit_code": 0,
            "disabled": False, "status": "idle",
        }

        with patch("lib.task_manager.CONFIG_PATH", config_path), \
             patch("lib.task_manager.LAUNCHAGENTS_DIR", la_dir), \
             patch("lib.task_manager.USER_LAUNCHAGENTS_DIR", user_la_dir), \
             patch("subprocess.run") as mock_run, \
             patch("lib.task_manager._get_uid", return_value="501"), \
             patch("lib.task_manager.get_launchd_status", return_value=launchd_status):
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            result = enable_task("uce")

        assert result["ok"] is True
        assert "launchd_status" in result
        assert result["launchd_status"]["status"] == "idle"

    def test_enable_fails_on_exit_78(self, tmp_path, sample_config):
        """enable_task() should return ok=False when launchd rejects with exit 78."""
        config_path, la_dir, user_la_dir = self._make_config(tmp_path, sample_config)
        launchd_status = {
            "installed": True, "running": False, "exit_code": 78,
            "disabled": True, "status": "disabled",
        }

        with patch("lib.task_manager.CONFIG_PATH", config_path), \
             patch("lib.task_manager.LAUNCHAGENTS_DIR", la_dir), \
             patch("lib.task_manager.USER_LAUNCHAGENTS_DIR", user_la_dir), \
             patch("subprocess.run") as mock_run, \
             patch("lib.task_manager._get_uid", return_value="501"), \
             patch("lib.task_manager.get_launchd_status", return_value=launchd_status):
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            result = enable_task("uce")

        assert result["ok"] is False
        assert "rejected" in result["error"].lower() or "disabled" in result["error"].lower()
        assert result["launchd_status"]["disabled"] is True

    def test_enable_ok_on_failed_status(self, tmp_path, sample_config):
        """enable_task() should return ok=True when status is 'failed' (not 'disabled')."""
        config_path, la_dir, user_la_dir = self._make_config(tmp_path, sample_config)
        launchd_status = {
            "installed": True, "running": False, "exit_code": 1,
            "disabled": False, "status": "failed",
        }

        with patch("lib.task_manager.CONFIG_PATH", config_path), \
             patch("lib.task_manager.LAUNCHAGENTS_DIR", la_dir), \
             patch("lib.task_manager.USER_LAUNCHAGENTS_DIR", user_la_dir), \
             patch("subprocess.run") as mock_run, \
             patch("lib.task_manager._get_uid", return_value="501"), \
             patch("lib.task_manager.get_launchd_status", return_value=launchd_status):
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            result = enable_task("uce")

        assert result["ok"] is True
        assert result["launchd_status"]["status"] == "failed"


class TestSetSchedule:
    def test_sets_schedule_in_config(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config))

        with patch("lib.task_manager.CONFIG_PATH", config_path):
            from lib.task_manager import set_schedule
            result = set_schedule("uce", "daily 7:00,19:00")

        assert result["ok"] is True
        assert result["expected_interval_hours"] == 14.0
        updated = yaml.safe_load(config_path.read_text())
        assert updated["tasks"]["uce"]["schedule"] == "daily 7:00,19:00"

    def test_invalid_schedule_fails(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config))

        with patch("lib.task_manager.CONFIG_PATH", config_path):
            from lib.task_manager import set_schedule
            result = set_schedule("uce", "bad schedule")

        assert result["ok"] is False


# =============================================================================
# Helpers
# =============================================================================


class TestDeepMerge:
    def test_simple_merge(self):
        base = {"a": 1, "b": 2}
        _deep_merge(base, {"b": 3, "c": 4})
        assert base == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        base = {"a": {"x": 1, "y": 2}}
        _deep_merge(base, {"a": {"y": 3, "z": 4}})
        assert base == {"a": {"x": 1, "y": 3, "z": 4}}

    def test_overwrite_non_dict(self):
        base = {"a": "string"}
        _deep_merge(base, {"a": {"nested": True}})
        assert base == {"a": {"nested": True}}


# =============================================================================
# LaunchAgent Status Detection
# =============================================================================


class TestGetLaunchdStatus:
    """Tests for get_launchd_status() with mocked launchctl output."""

    def test_not_installed(self, tmp_path):
        """Agent plist doesn't exist -> not_installed."""
        user_la_dir = tmp_path / "Library" / "LaunchAgents"
        user_la_dir.mkdir(parents=True)

        with patch("lib.task_manager.USER_LAUNCHAGENTS_DIR", user_la_dir):
            result = get_launchd_status("nonexistent")

        assert result["installed"] is False
        assert result["status"] == "not_installed"
        assert result["disabled"] is False

    def test_disabled_exit_78(self, tmp_path):
        """Agent with exit code 78 -> disabled."""
        user_la_dir = tmp_path / "Library" / "LaunchAgents"
        user_la_dir.mkdir(parents=True)
        (user_la_dir / "com.pinglet.my-task.plist").write_text("<plist/>")

        launchctl_output = '''{
\t"LimitLoadToSessionType" = "Aqua";
\t"Label" = "com.pinglet.my-task";
\t"LastExitStatus" = 78;
\t"PID" = 0;
};
'''
        mock_result = MagicMock(returncode=0, stdout=launchctl_output, stderr="")

        with patch("lib.task_manager.USER_LAUNCHAGENTS_DIR", user_la_dir), \
             patch("subprocess.run", return_value=mock_result):
            result = get_launchd_status("my-task")

        assert result["installed"] is True
        assert result["disabled"] is True
        assert result["exit_code"] == 78
        assert result["status"] == "disabled"

    def test_running_agent(self, tmp_path):
        """Agent with a PID -> running."""
        user_la_dir = tmp_path / "Library" / "LaunchAgents"
        user_la_dir.mkdir(parents=True)
        (user_la_dir / "com.pinglet.my-task.plist").write_text("<plist/>")

        launchctl_output = '''{
\t"Label" = "com.pinglet.my-task";
\t"LastExitStatus" = 0;
\t"PID" = 12345;
};
'''
        mock_result = MagicMock(returncode=0, stdout=launchctl_output, stderr="")

        with patch("lib.task_manager.USER_LAUNCHAGENTS_DIR", user_la_dir), \
             patch("subprocess.run", return_value=mock_result):
            result = get_launchd_status("my-task")

        assert result["installed"] is True
        assert result["running"] is True
        assert result["status"] == "running"
        assert result["disabled"] is False

    def test_failed_agent(self, tmp_path):
        """Agent with non-zero, non-78 exit code -> failed."""
        user_la_dir = tmp_path / "Library" / "LaunchAgents"
        user_la_dir.mkdir(parents=True)
        (user_la_dir / "com.pinglet.my-task.plist").write_text("<plist/>")

        launchctl_output = '''{
\t"Label" = "com.pinglet.my-task";
\t"LastExitStatus" = 1;
};
'''
        mock_result = MagicMock(returncode=0, stdout=launchctl_output, stderr="")

        with patch("lib.task_manager.USER_LAUNCHAGENTS_DIR", user_la_dir), \
             patch("subprocess.run", return_value=mock_result):
            result = get_launchd_status("my-task")

        assert result["installed"] is True
        assert result["running"] is False
        assert result["exit_code"] == 1
        assert result["status"] == "failed"
        assert result["disabled"] is False

    def test_idle_agent(self, tmp_path):
        """Agent loaded with exit code 0, not running -> idle."""
        user_la_dir = tmp_path / "Library" / "LaunchAgents"
        user_la_dir.mkdir(parents=True)
        (user_la_dir / "com.pinglet.my-task.plist").write_text("<plist/>")

        launchctl_output = '''{
\t"Label" = "com.pinglet.my-task";
\t"LastExitStatus" = 0;
};
'''
        mock_result = MagicMock(returncode=0, stdout=launchctl_output, stderr="")

        with patch("lib.task_manager.USER_LAUNCHAGENTS_DIR", user_la_dir), \
             patch("subprocess.run", return_value=mock_result):
            result = get_launchd_status("my-task")

        assert result["installed"] is True
        assert result["running"] is False
        assert result["exit_code"] == 0
        assert result["status"] == "idle"

    def test_not_loaded(self, tmp_path):
        """Agent plist exists but not loaded in launchd -> not_loaded."""
        user_la_dir = tmp_path / "Library" / "LaunchAgents"
        user_la_dir.mkdir(parents=True)
        (user_la_dir / "com.pinglet.my-task.plist").write_text("<plist/>")

        mock_result = MagicMock(returncode=113, stdout="", stderr="Could not find service")

        with patch("lib.task_manager.USER_LAUNCHAGENTS_DIR", user_la_dir), \
             patch("subprocess.run", return_value=mock_result):
            result = get_launchd_status("my-task")

        assert result["installed"] is True
        assert result["status"] == "not_loaded"

    def test_launchctl_timeout(self, tmp_path):
        """launchctl times out -> unknown status with installed=True."""
        user_la_dir = tmp_path / "Library" / "LaunchAgents"
        user_la_dir.mkdir(parents=True)
        (user_la_dir / "com.pinglet.my-task.plist").write_text("<plist/>")

        import subprocess as sp

        with patch("lib.task_manager.USER_LAUNCHAGENTS_DIR", user_la_dir), \
             patch("subprocess.run", side_effect=sp.TimeoutExpired("launchctl", 5)):
            result = get_launchd_status("my-task")

        assert result["installed"] is True
        assert result["status"] == "unknown"


class TestGeneratePlistKeepAlive:
    """Test that generate_plist includes KeepAlive configuration."""

    def test_keepalive_in_plist(self):
        plist = generate_plist("my-task", {"StartInterval": 3600})
        assert "KeepAlive" in plist
        assert "SuccessfulExit" in plist
        assert "<false/>" in plist

    def test_no_keepalive_for_calendar_interval(self):
        """Regression: KeepAlive + StartCalendarInterval causes launchd exit 78."""
        plist = generate_plist("my-task", {"StartCalendarInterval": {"Hour": 7, "Minute": 0}})
        assert "KeepAlive" not in plist

    def test_no_keepalive_for_calendar_interval_array(self):
        """Regression: array form of StartCalendarInterval must also exclude KeepAlive."""
        plist = generate_plist("my-task", {"StartCalendarInterval": [
            {"Hour": 7, "Minute": 0},
            {"Hour": 19, "Minute": 0},
        ]})
        assert "KeepAlive" not in plist
