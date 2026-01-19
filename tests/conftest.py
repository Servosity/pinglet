"""Pytest configuration and shared fixtures for Pinglet tests."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def temp_state_dir(tmp_path):
    """Create a temporary state directory for tests."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture
def temp_logs_dir(tmp_path):
    """Create a temporary logs directory for tests."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    return logs_dir


@pytest.fixture
def mock_project_root(tmp_path, temp_state_dir, temp_logs_dir):
    """Create a temporary project root with state and logs directories."""
    return tmp_path


@pytest.fixture
def sample_config():
    """Return a sample configuration dict."""
    return {
        "slack": {
            "token_env": "SLACK_USER_TOKEN",
            "default_channel": "DXXXXXXXXXX",
        },
        "notifications": {
            "on_success": False,
            "on_failure": True,
            "slack_enabled": True,
            "macos_enabled": True,
            "success_silent": True,
            "manual_complete_silent": True,
        },
        "reliability": {
            "retry": {
                "max_attempts": 3,
                "delays_seconds": [10, 60, 300],
                "jitter": 0.25,
            },
            "alert": {
                "consecutive_failures": 3,
                "cooldown_minutes": 30,
            },
            "notify_on_recovery": True,
        },
        "heartbeat": {
            "enabled": True,
            "interval_minutes": 60,
            "wake_delay_seconds": 30,
        },
        "healthcheck": {
            "enabled": True,
            "schedule_hour": 20,
            "expected_intervals": {
                "uce": 14,
                "git-sync": 2,
                "claude-backup": 2,
            },
        },
        "tasks": {
            "uce": {
                "name": "UCE Link Collector",
                "command": "/usr/bin/python3",
                "args": ["runner.py"],
                "working_dir": "/tmp/uce",
                "timeout": 600,
                "env": ["HOME"],
                "output": {
                    "format": "text",
                },
                "reliability": {
                    "alert": {
                        "consecutive_failures": 3,
                    },
                },
            },
            "obsidian-tab-archiver": {
                "name": "Obsidian Tab Archiver",
                "command": "/usr/bin/python3",
                "args": ["archive_tabs.py"],
                "working_dir": "/tmp/archiver",
                "timeout": 300,
                "output": {
                    "format": "json",
                    "summary_template": "Archived {tabs_archived} tabs, kept {tabs_kept}",
                },
                "reliability": {
                    "alert": {
                        "consecutive_failures": 2,
                    },
                },
            },
        },
    }


@pytest.fixture
def sample_task_state():
    """Return a sample task state dict."""
    return {
        "task_name": "uce",
        "last_run": "2026-01-18T07:00:06.852060",
        "last_status": "success",
        "last_error": None,
        "last_duration_seconds": 1.5,
        "consecutive_failures": 0,
        "total_runs": 41,
        "total_failures": 1,
        "runs_today": 1,
        "runs_today_date": "2026-01-18",
        "last_alert_time": None,
        "alerts_sent_this_streak": 0,
        "was_failing": False,
    }


@pytest.fixture
def mock_slack_client():
    """Mock Slack client for testing."""
    with patch("lib.alerts.get_client") as mock:
        client = MagicMock()
        client.chat_postMessage.return_value = {"ok": True}
        mock.return_value = client
        yield client


@pytest.fixture
def mock_subprocess():
    """Mock subprocess for testing notifications."""
    with patch("subprocess.run") as mock:
        mock.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
        yield mock


@pytest.fixture
def mock_terminal_notifier(mock_subprocess):
    """Mock terminal-notifier for testing macOS notifications."""
    return mock_subprocess
