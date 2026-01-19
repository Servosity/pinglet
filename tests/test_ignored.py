"""Tests for ignored tasks management."""
import json
import pytest
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestIgnoredTasksManager:
    """Tests for the ignored tasks manager."""

    def test_load_ignored_empty(self, tmp_path):
        """Test loading ignored tasks when file doesn't exist."""
        with patch("lib.ignored.IGNORED_FILE", tmp_path / "ignored.json"):
            from lib.ignored import load_ignored
            result = load_ignored()
            assert result == {}

    def test_load_ignored_with_data(self, tmp_path):
        """Test loading ignored tasks from file."""
        ignored_file = tmp_path / "ignored.json"
        ignored_file.write_text(json.dumps({
            "uce": {
                "ignored_at": "2026-01-18T14:30:00",
                "reason": "Manual skip via notification",
                "last_run_at_ignore": "2026-01-17T22:00:00",
                "threshold_hours": 14,
                "hours_overdue": 16
            }
        }))

        with patch("lib.ignored.IGNORED_FILE", ignored_file):
            from lib.ignored import load_ignored
            result = load_ignored()
            assert "uce" in result
            assert result["uce"]["threshold_hours"] == 14

    def test_save_ignored(self, tmp_path):
        """Test saving ignored tasks."""
        ignored_file = tmp_path / "ignored.json"

        with patch("lib.ignored.IGNORED_FILE", ignored_file):
            from lib.ignored import save_ignored
            save_ignored({
                "test-task": {
                    "ignored_at": "2026-01-18T10:00:00",
                    "reason": "Test",
                }
            })

            assert ignored_file.exists()
            data = json.loads(ignored_file.read_text())
            assert "test-task" in data

    def test_ignore_task(self, tmp_path):
        """Test marking a task as ignored."""
        ignored_file = tmp_path / "ignored.json"

        with patch("lib.ignored.IGNORED_FILE", ignored_file):
            from lib.ignored import ignore_task, load_ignored

            ignore_task(
                task_name="uce",
                last_run="2026-01-17T22:00:00",
                threshold=14,
                hours_overdue=16.5
            )

            result = load_ignored()
            assert "uce" in result
            assert result["uce"]["threshold_hours"] == 14
            assert result["uce"]["hours_overdue"] == 16.5
            assert result["uce"]["reason"] == "Manual skip via notification"

    def test_clear_ignored(self, tmp_path):
        """Test clearing ignored status for a task."""
        ignored_file = tmp_path / "ignored.json"
        ignored_file.write_text(json.dumps({
            "uce": {"ignored_at": "2026-01-18T14:30:00"},
            "other-task": {"ignored_at": "2026-01-18T14:30:00"}
        }))

        with patch("lib.ignored.IGNORED_FILE", ignored_file):
            from lib.ignored import clear_ignored, load_ignored

            clear_ignored("uce")

            result = load_ignored()
            assert "uce" not in result
            assert "other-task" in result

    def test_clear_ignored_nonexistent(self, tmp_path):
        """Test clearing a task that isn't ignored."""
        ignored_file = tmp_path / "ignored.json"
        ignored_file.write_text(json.dumps({}))

        with patch("lib.ignored.IGNORED_FILE", ignored_file):
            from lib.ignored import clear_ignored, load_ignored

            # Should not raise an error
            clear_ignored("nonexistent")

            result = load_ignored()
            assert result == {}

    def test_is_ignored_true(self, tmp_path):
        """Test checking if a task is ignored - true case."""
        ignored_file = tmp_path / "ignored.json"
        ignored_file.write_text(json.dumps({
            "uce": {"ignored_at": "2026-01-18T14:30:00"}
        }))

        with patch("lib.ignored.IGNORED_FILE", ignored_file):
            from lib.ignored import is_ignored

            assert is_ignored("uce") is True

    def test_is_ignored_false(self, tmp_path):
        """Test checking if a task is ignored - false case."""
        ignored_file = tmp_path / "ignored.json"
        ignored_file.write_text(json.dumps({}))

        with patch("lib.ignored.IGNORED_FILE", ignored_file):
            from lib.ignored import is_ignored

            assert is_ignored("uce") is False

    def test_is_ignored_null_value(self, tmp_path):
        """Test checking if a task is ignored with null value."""
        ignored_file = tmp_path / "ignored.json"
        ignored_file.write_text(json.dumps({
            "uce": None
        }))

        with patch("lib.ignored.IGNORED_FILE", ignored_file):
            from lib.ignored import is_ignored

            assert is_ignored("uce") is False
