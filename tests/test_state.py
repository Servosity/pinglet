"""Tests for state management module."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.state import load_all_states, STATE_DIR


class TestLoadAllStates:
    """Tests for load_all_states() file filtering."""

    def test_skips_underscore_prefixed_files(self, tmp_path):
        """Internal state files (_heartbeat_alerts.json) should be excluded."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Regular task state
        (state_dir / "uce.json").write_text(json.dumps({
            "task_name": "uce", "last_run": None, "last_status": "never_run",
            "last_error": None, "last_duration_seconds": 0.0,
            "consecutive_failures": 0, "total_runs": 0, "total_failures": 0,
            "runs_today": 0, "runs_today_date": None,
        }))

        # Internal file (should be skipped)
        (state_dir / "_heartbeat_alerts.json").write_text(json.dumps({
            "uce": {"last_alert": "2026-03-13T10:00:00", "escalation": "warning"}
        }))

        with patch("lib.state.STATE_DIR", state_dir):
            states = load_all_states()

        task_names = [s.task_name for s in states]
        assert "uce" in task_names
        assert "_heartbeat_alerts" not in task_names

    def test_loads_all_regular_files(self, tmp_path):
        """All non-underscore state files should be loaded."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        for name in ["uce", "git-sync", "daily-pulse"]:
            (state_dir / f"{name}.json").write_text(json.dumps({
                "task_name": name, "last_run": None, "last_status": "never_run",
                "last_error": None, "last_duration_seconds": 0.0,
                "consecutive_failures": 0, "total_runs": 0, "total_failures": 0,
                "runs_today": 0, "runs_today_date": None,
            }))

        with patch("lib.state.STATE_DIR", state_dir):
            states = load_all_states()

        task_names = [s.task_name for s in states]
        assert len(task_names) == 3
        assert set(task_names) == {"uce", "git-sync", "daily-pulse"}
