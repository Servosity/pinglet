"""Tests for output formatting (Feature 3)."""
import json
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.output_formatter import (
    format_task_output,
    OutputConfig,
)


class TestOutputConfig:
    """Tests for OutputConfig class."""

    def test_default_config(self):
        """Test default output configuration."""
        config = OutputConfig()
        assert config.format == "text"
        assert config.summary_template is None

    def test_from_dict_text_format(self):
        """Test creating config from dict with text format."""
        config = OutputConfig.from_dict({"format": "text"})
        assert config.format == "text"
        assert config.summary_template is None

    def test_from_dict_json_format(self):
        """Test creating config from dict with JSON format."""
        config = OutputConfig.from_dict({
            "format": "json",
            "summary_template": "Archived {tabs_archived} tabs"
        })
        assert config.format == "json"
        assert config.summary_template == "Archived {tabs_archived} tabs"

    def test_from_dict_empty(self):
        """Test creating config from empty dict uses defaults."""
        config = OutputConfig.from_dict({})
        assert config.format == "text"
        assert config.summary_template is None

    def test_from_dict_none(self):
        """Test creating config from None uses defaults."""
        config = OutputConfig.from_dict(None)
        assert config.format == "text"
        assert config.summary_template is None


class TestTextFormatter:
    """Tests for text output formatting."""

    def test_text_format_short_output(self):
        """Test text format with short output."""
        config = OutputConfig(format="text")
        stdout = "Line 1\nLine 2\nLine 3"
        result = format_task_output(stdout, "", config)
        assert "Line 1" in result
        assert "Line 2" in result
        assert "Line 3" in result

    def test_text_format_long_output_last_5_lines(self):
        """Test text format shows last 5 lines."""
        config = OutputConfig(format="text")
        lines = [f"Line {i}" for i in range(10)]
        stdout = "\n".join(lines)
        result = format_task_output(stdout, "", config)
        # Should only contain last 5 lines
        assert "Line 5" in result
        assert "Line 6" in result
        assert "Line 9" in result
        assert "Line 0" not in result
        assert "Line 4" not in result

    def test_text_format_truncates_to_200_chars(self):
        """Test text format truncates to 200 characters."""
        config = OutputConfig(format="text")
        stdout = "A" * 500
        result = format_task_output(stdout, "", config)
        assert len(result) <= 203  # 200 + "..."

    def test_text_format_empty_output(self):
        """Test text format with empty output."""
        config = OutputConfig(format="text")
        result = format_task_output("", "", config)
        assert result == ""

    def test_text_format_whitespace_only(self):
        """Test text format with whitespace only."""
        config = OutputConfig(format="text")
        result = format_task_output("   \n\n   ", "", config)
        assert result == ""


class TestJsonFormatter:
    """Tests for JSON output formatting."""

    def test_json_format_with_template(self):
        """Test JSON format with template string substitution."""
        config = OutputConfig(
            format="json",
            summary_template="Archived {tabs_archived} tabs, kept {tabs_kept}"
        )
        stdout = json.dumps({
            "success": True,
            "tabs_archived": 12,
            "tabs_kept": 8
        })
        result = format_task_output(stdout, "", config)
        assert result == "Archived 12 tabs, kept 8"

    def test_json_format_complex_template(self):
        """Test JSON format with complex template."""
        config = OutputConfig(
            format="json",
            summary_template="Archived {tabs_archived} tabs, kept {tabs_kept} ({tabs_kept_pinned} pinned, {tabs_kept_recent} recent)"
        )
        stdout = json.dumps({
            "success": True,
            "tabs_archived": 12,
            "tabs_kept": 8,
            "tabs_kept_pinned": 3,
            "tabs_kept_recent": 5
        })
        result = format_task_output(stdout, "", config)
        assert result == "Archived 12 tabs, kept 8 (3 pinned, 5 recent)"

    def test_json_format_without_template(self):
        """Test JSON format without template shows key fields."""
        config = OutputConfig(format="json")
        stdout = json.dumps({"success": True, "count": 5})
        result = format_task_output(stdout, "", config)
        # Should show JSON formatted, truncated to 200 chars
        assert "success" in result or "count" in result

    def test_json_format_invalid_json_fallback(self):
        """Test JSON format falls back to text when JSON is invalid."""
        config = OutputConfig(
            format="json",
            summary_template="Count: {count}"
        )
        stdout = "This is not JSON"
        result = format_task_output(stdout, "", config)
        # Should fall back to text format
        assert result == "This is not JSON"

    def test_json_format_missing_template_key(self):
        """Test JSON format with missing template key falls back gracefully."""
        config = OutputConfig(
            format="json",
            summary_template="Archived {tabs_archived} tabs, skipped {tabs_skipped}"
        )
        stdout = json.dumps({
            "tabs_archived": 12,
            # tabs_skipped is missing
        })
        result = format_task_output(stdout, "", config)
        # Should fall back to text or show partial
        assert "12" in result or "tabs_archived" in result

    def test_json_format_nested_fields(self):
        """Test JSON format handles nested fields."""
        config = OutputConfig(format="json")
        stdout = json.dumps({
            "success": True,
            "stats": {"archived": 5, "kept": 3}
        })
        result = format_task_output(stdout, "", config)
        assert "success" in result or "stats" in result


class TestErrorSourcePriority:
    """Tests for error source priority in failure notifications."""

    def test_stderr_prioritized_over_stdout(self):
        """Test that stderr is used for error message when available."""
        config = OutputConfig(format="text")
        stdout = "Some stdout output"
        stderr = "Error: Something went wrong"
        result = format_task_output(stdout, stderr, config, is_error=True)
        assert "Something went wrong" in result

    def test_stdout_used_when_stderr_empty(self):
        """Test stdout is used when stderr is empty."""
        config = OutputConfig(format="text")
        stdout = "Error occurred in processing"
        stderr = ""
        result = format_task_output(stdout, stderr, config, is_error=True)
        assert "Error occurred in processing" in result

    def test_json_error_key_appended(self):
        """Test JSON error key is appended to error output."""
        config = OutputConfig(format="json")
        stdout = json.dumps({
            "success": False,
            "error": "Database connection failed"
        })
        stderr = "Connection timeout"
        result = format_task_output(stdout, stderr, config, is_error=True)
        # Should include stderr first
        assert "Connection timeout" in result or "Database connection failed" in result

    def test_error_output_truncated_to_200_chars(self):
        """Test error output is truncated to 200 characters."""
        config = OutputConfig(format="text")
        stdout = ""
        stderr = "E" * 500
        result = format_task_output(stdout, stderr, config, is_error=True)
        assert len(result) <= 203  # 200 + "..."
