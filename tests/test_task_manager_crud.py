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
