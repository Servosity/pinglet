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
