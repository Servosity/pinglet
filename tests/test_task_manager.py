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


# =============================================================================
# Schedule Parsing
# =============================================================================


class TestParseSchedule:
    def test_every_hours(self):
        assert parse_schedule("every 1h") == {"StartInterval": 3600}

    def test_every_minutes(self):
        assert parse_schedule("every 30m") == {"StartInterval": 1800}

    def test_every_seconds(self):
        assert parse_schedule("every 3600s") == {"StartInterval": 3600}

    def test_daily_single(self):
        assert parse_schedule("daily 7:00") == {"StartCalendarInterval": {"Hour": 7, "Minute": 0}}

    def test_daily_multiple(self):
        result = parse_schedule("daily 7:00,19:00")
        assert result == {"StartCalendarInterval": [
            {"Hour": 7, "Minute": 0},
            {"Hour": 19, "Minute": 0},
        ]}

    def test_weekly(self):
        assert parse_schedule("weekly mon 7:33") == {
            "StartCalendarInterval": {"Weekday": 1, "Hour": 7, "Minute": 33}
        }

    def test_weekly_full_name(self):
        assert parse_schedule("weekly monday 7:33") == {
            "StartCalendarInterval": {"Weekday": 1, "Hour": 7, "Minute": 33}
        }

    def test_weekly_sunday(self):
        assert parse_schedule("weekly sun 8:00") == {
            "StartCalendarInterval": {"Weekday": 0, "Hour": 8, "Minute": 0}
        }

    def test_case_insensitive(self):
        assert parse_schedule("Every 1H") == {"StartInterval": 3600}
        assert parse_schedule("DAILY 7:00") == {"StartCalendarInterval": {"Hour": 7, "Minute": 0}}

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Cannot parse schedule"):
            parse_schedule("at 7:00")

    def test_invalid_weekday(self):
        with pytest.raises(ValueError, match="Unknown weekday"):
            parse_schedule("weekly xyz 7:00")

    def test_invalid_time(self):
        with pytest.raises(ValueError, match="Hour must be"):
            parse_schedule("daily 25:00")

    def test_invalid_minute(self):
        with pytest.raises(ValueError, match="Minute must be"):
            parse_schedule("daily 7:99")


class TestParseTime:
    def test_valid(self):
        assert _parse_time("7:00") == {"Hour": 7, "Minute": 0}
        assert _parse_time("19:30") == {"Hour": 19, "Minute": 30}
        assert _parse_time("0:00") == {"Hour": 0, "Minute": 0}
        assert _parse_time("23:59") == {"Hour": 23, "Minute": 59}

    def test_invalid_format(self):
        with pytest.raises(ValueError):
            _parse_time("7")
        with pytest.raises(ValueError):
            _parse_time("abc")


class TestScheduleToHuman:
    def test_interval_hours(self):
        assert schedule_to_human({"StartInterval": 3600}) == "every 1h"

    def test_interval_minutes(self):
        assert schedule_to_human({"StartInterval": 1800}) == "every 30m"

    def test_interval_seconds(self):
        assert schedule_to_human({"StartInterval": 45}) == "every 45s"

    def test_daily_single(self):
        assert schedule_to_human({"StartCalendarInterval": {"Hour": 7, "Minute": 0}}) == "daily 7:00"

    def test_daily_multiple(self):
        result = schedule_to_human({"StartCalendarInterval": [
            {"Hour": 7, "Minute": 0},
            {"Hour": 19, "Minute": 0},
        ]})
        assert result == "daily 7:00,19:00"

    def test_weekly(self):
        result = schedule_to_human({"StartCalendarInterval": {"Weekday": 1, "Hour": 7, "Minute": 33}})
        assert result == "weekly mon 7:33"

    def test_roundtrip(self):
        """Parse then convert back should produce equivalent string."""
        for spec in ["every 1h", "every 30m", "daily 7:00", "daily 7:00,19:00", "weekly mon 7:33"]:
            parsed = parse_schedule(spec)
            assert schedule_to_human(parsed) == spec


class TestEstimateExpectedInterval:
    def test_start_interval(self):
        assert estimate_expected_interval({"StartInterval": 3600}) == 1.5

    def test_daily_single(self):
        assert estimate_expected_interval({"StartCalendarInterval": {"Hour": 7, "Minute": 0}}) == 26

    def test_daily_multiple(self):
        result = estimate_expected_interval({"StartCalendarInterval": [
            {"Hour": 7, "Minute": 0},
            {"Hour": 19, "Minute": 0},
        ]})
        assert result == 14.0  # 12h gap + 2h grace

    def test_weekly(self):
        assert estimate_expected_interval(
            {"StartCalendarInterval": {"Weekday": 1, "Hour": 7, "Minute": 33}}
        ) == 180


# =============================================================================
# Validation
# =============================================================================


class TestValidateTaskId:
    def test_valid(self):
        assert validate_task_id("my-task")[0] is True
        assert validate_task_id("ab")[0] is True
        assert validate_task_id("task-123")[0] is True
        assert validate_task_id("a1")[0] is True

    def test_too_short(self):
        assert validate_task_id("a")[0] is False

    def test_uppercase(self):
        assert validate_task_id("My-Task")[0] is False

    def test_leading_hyphen(self):
        assert validate_task_id("-task")[0] is False

    def test_trailing_hyphen(self):
        assert validate_task_id("task-")[0] is False

    def test_underscore(self):
        assert validate_task_id("my_task")[0] is False

    def test_spaces(self):
        assert validate_task_id("my task")[0] is False


class TestValidateTaskConfig:
    def test_valid_minimal(self):
        valid, errors = validate_task_config({"command": "/usr/bin/echo"}, "test", {})
        assert valid is True
        assert errors == []

    def test_missing_command(self):
        valid, errors = validate_task_config({"name": "Test"}, "test", {})
        assert valid is False
        assert any("command" in e for e in errors)

    def test_duplicate_task_id(self):
        valid, errors = validate_task_config({"command": "/usr/bin/echo"}, "test", {"test": {}})
        assert valid is False
        assert any("already exists" in e for e in errors)

    def test_invalid_timeout(self):
        valid, errors = validate_task_config({"command": "/usr/bin/echo", "timeout": -1}, "test", {})
        assert valid is False

    def test_invalid_output_format(self):
        valid, errors = validate_task_config(
            {"command": "/usr/bin/echo", "output": {"format": "xml"}}, "test", {}
        )
        assert valid is False


# =============================================================================
# Plist Generation
# =============================================================================


class TestGeneratePlist:
    def test_start_interval(self):
        plist = generate_plist("my-task", {"StartInterval": 3600})
        assert "com.pinglet.my-task" in plist
        assert "<integer>3600</integer>" in plist
        assert "StartInterval" in plist
        assert "--task" in plist
        assert "my-task" in plist
        assert "<?xml" in plist

    def test_calendar_single(self):
        plist = generate_plist("my-task", {"StartCalendarInterval": {"Hour": 7, "Minute": 0}})
        assert "StartCalendarInterval" in plist
        assert "<integer>7</integer>" in plist
        assert "<integer>0</integer>" in plist

    def test_calendar_array(self):
        plist = generate_plist("my-task", {"StartCalendarInterval": [
            {"Hour": 7, "Minute": 0},
            {"Hour": 19, "Minute": 0},
        ]})
        assert "<array>" in plist
        assert "<integer>7</integer>" in plist
        assert "<integer>19</integer>" in plist

    def test_weekly(self):
        plist = generate_plist("my-task", {"StartCalendarInterval": {
            "Weekday": 1, "Hour": 7, "Minute": 33,
        }})
        assert "Weekday" in plist
        assert "<integer>1</integer>" in plist

    def test_log_paths(self):
        plist = generate_plist("my-task", {"StartInterval": 60})
        assert "my-task.log" in plist
        assert "my-task.err" in plist

    def test_env_path(self):
        plist = generate_plist("my-task", {"StartInterval": 60})
        assert "/opt/homebrew/bin" in plist


# =============================================================================
# CRUD Operations
# =============================================================================


class TestAddTask:
    def test_add_writes_config(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config))

        with patch("lib.task_manager.CONFIG_PATH", config_path):
            from lib.task_manager import add_task
            result = add_task("new-task", {"command": "/usr/bin/echo", "name": "New Task"})

        assert result["ok"] is True
        assert result["task_id"] == "new-task"
        updated = yaml.safe_load(config_path.read_text())
        assert "new-task" in updated["tasks"]
        assert updated["tasks"]["new-task"]["name"] == "New Task"

    def test_add_applies_defaults(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config))

        with patch("lib.task_manager.CONFIG_PATH", config_path):
            from lib.task_manager import add_task
            result = add_task("my-test", {"command": "/usr/bin/echo"})

        assert result["ok"] is True
        assert result["config"]["name"] == "My Test"  # Auto title-cased
        assert result["config"]["timeout"] == 300

    def test_add_duplicate_fails(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config))

        with patch("lib.task_manager.CONFIG_PATH", config_path):
            from lib.task_manager import add_task
            result = add_task("uce", {"command": "/usr/bin/echo"})

        assert result["ok"] is False
        assert "already exists" in result["error"]

    def test_add_missing_command_fails(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config))

        with patch("lib.task_manager.CONFIG_PATH", config_path):
            from lib.task_manager import add_task
            result = add_task("new-task", {"name": "No Command"})

        assert result["ok"] is False
        assert "command" in result["error"]

    def test_add_with_schedule_sets_healthcheck(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config))

        with patch("lib.task_manager.CONFIG_PATH", config_path):
            from lib.task_manager import add_task
            result = add_task("scheduled-task", {"command": "/usr/bin/echo", "schedule": "every 1h"})

        assert result["ok"] is True
        updated = yaml.safe_load(config_path.read_text())
        assert "scheduled-task" in updated["healthcheck"]["expected_intervals"]
        assert updated["healthcheck"]["expected_intervals"]["scheduled-task"] == 1.5


class TestEditTask:
    def test_edit_updates_field(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config))

        with patch("lib.task_manager.CONFIG_PATH", config_path):
            from lib.task_manager import edit_task
            result = edit_task("uce", {"timeout": 900})

        assert result["ok"] is True
        assert result["before"]["timeout"] == 600
        assert result["after"]["timeout"] == 900
        updated = yaml.safe_load(config_path.read_text())
        assert updated["tasks"]["uce"]["timeout"] == 900

    def test_edit_deep_merge(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config))

        with patch("lib.task_manager.CONFIG_PATH", config_path):
            from lib.task_manager import edit_task
            result = edit_task("uce", {"reliability": {"alert": {"consecutive_failures": 5}}})

        assert result["ok"] is True
        assert result["after"]["reliability"]["alert"]["consecutive_failures"] == 5

    def test_edit_nonexistent_fails(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config))

        with patch("lib.task_manager.CONFIG_PATH", config_path):
            from lib.task_manager import edit_task
            result = edit_task("nonexistent", {"timeout": 900})

        assert result["ok"] is False
        assert "not found" in result["error"]


class TestRemoveTask:
    def test_remove_deletes_from_config(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config_path.write_text(yaml.dump(sample_config))

        with patch("lib.task_manager.CONFIG_PATH", config_path), \
             patch("lib.task_manager.STATE_DIR", state_dir), \
             patch("lib.task_manager.is_task_enabled", return_value=False):
            from lib.task_manager import remove_task
            result = remove_task("uce")

        assert result["ok"] is True
        updated = yaml.safe_load(config_path.read_text())
        assert "uce" not in updated["tasks"]

    def test_remove_cleans_healthcheck(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config_path.write_text(yaml.dump(sample_config))

        with patch("lib.task_manager.CONFIG_PATH", config_path), \
             patch("lib.task_manager.STATE_DIR", state_dir), \
             patch("lib.task_manager.is_task_enabled", return_value=False):
            from lib.task_manager import remove_task
            result = remove_task("uce")

        updated = yaml.safe_load(config_path.read_text())
        assert "uce" not in updated["healthcheck"]["expected_intervals"]

    def test_remove_disables_if_enabled(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config_path.write_text(yaml.dump(sample_config))

        with patch("lib.task_manager.CONFIG_PATH", config_path), \
             patch("lib.task_manager.STATE_DIR", state_dir), \
             patch("lib.task_manager.is_task_enabled", return_value=True), \
             patch("lib.task_manager.disable_task") as mock_disable:
            mock_disable.return_value = {"ok": True}
            from lib.task_manager import remove_task
            result = remove_task("uce")

        mock_disable.assert_called_once_with("uce")
        assert "disabled LaunchAgent" in result["cleaned"]

    def test_remove_nonexistent_fails(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config))

        with patch("lib.task_manager.CONFIG_PATH", config_path):
            from lib.task_manager import remove_task
            result = remove_task("nonexistent")

        assert result["ok"] is False


class TestShowTask:
    def test_show_returns_config_and_state(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        config_path.write_text(yaml.dump(sample_config))

        mock_state = MagicMock()
        mock_state.last_run = "2026-01-18T07:00:00"
        mock_state.last_status = "success"
        mock_state.last_error = None
        mock_state.last_duration_seconds = 1.5
        mock_state.consecutive_failures = 0
        mock_state.total_runs = 10
        mock_state.total_failures = 0

        with patch("lib.task_manager.CONFIG_PATH", config_path), \
             patch("lib.task_manager.LOGS_DIR", logs_dir), \
             patch("lib.task_manager.load_state", return_value=mock_state), \
             patch("lib.task_manager.is_task_enabled", return_value=False):
            from lib.task_manager import show_task
            result = show_task("uce")

        assert result["ok"] is True
        assert result["config"]["name"] == "UCE Link Collector"
        assert result["state"]["last_status"] == "success"
        assert result["enabled"] is False

    def test_show_nonexistent_fails(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config))

        with patch("lib.task_manager.CONFIG_PATH", config_path):
            from lib.task_manager import show_task
            result = show_task("nonexistent")

        assert result["ok"] is False


class TestGetTaskLogs:
    def test_reads_matching_log_files(self, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "my-task.log").write_text("line1\nline2\nline3\n")
        (logs_dir / "my-task.err").write_text("error1\n")

        with patch("lib.task_manager.LOGS_DIR", logs_dir):
            from lib.task_manager import get_task_logs
            result = get_task_logs("my-task", lines=10)

        assert result["ok"] is True
        assert len(result["log_files"]) >= 2

    def test_handles_missing_logs(self, tmp_path):
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        with patch("lib.task_manager.LOGS_DIR", logs_dir):
            from lib.task_manager import get_task_logs
            result = get_task_logs("no-such-task")

        assert result["ok"] is True
        assert result["log_files"] == {}


class TestListTasksJson:
    def test_returns_all_tasks(self, tmp_path, sample_config):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(sample_config))

        mock_state = MagicMock()
        mock_state.last_run = None
        mock_state.last_status = "never_run"
        mock_state.consecutive_failures = 0
        mock_state.total_runs = 0

        with patch("lib.task_manager.CONFIG_PATH", config_path), \
             patch("lib.task_manager.load_state", return_value=mock_state), \
             patch("lib.task_manager.is_task_enabled", return_value=False):
            from lib.task_manager import list_tasks_json
            result = list_tasks_json()

        assert len(result) == 2  # uce + obsidian-tab-archiver
        ids = [t["task_id"] for t in result]
        assert "uce" in ids


# =============================================================================
# Enable / Disable
# =============================================================================


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
