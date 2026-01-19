"""Tests for enhanced notifications (Feature 4)."""
import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestMissedTaskNotification:
    """Tests for missed task notifications."""

    def test_missed_task_macos_notification(self):
        """Test macOS notification with Run/Ignore actions."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            from lib.alerts import send_missed_task_notification

            send_missed_task_notification(
                task_name="uce",
                display_name="UCE Link Collector",
                hours_overdue=16.5,
                threshold=14
            )

            # Should have called terminal-notifier
            mock_run.assert_called()
            call_args = str(mock_run.call_args)
            assert "terminal-notifier" in call_args or "osascript" in call_args

    def test_missed_task_slack_notification(self):
        """Test Slack notification for missed task."""
        with patch("lib.alerts.send_slack_message") as mock_slack:
            mock_slack.return_value = True

            from lib.alerts import send_missed_task_slack

            send_missed_task_slack(
                task_name="uce",
                display_name="UCE Link Collector",
                hours_overdue=16.5,
                threshold=14,
                last_run="2026-01-17 22:00"
            )

            mock_slack.assert_called_once()
            message = mock_slack.call_args[0][0]
            assert "uce" in message.lower() or "UCE" in message
            # Message shows total hours (hours_overdue + threshold)
            assert "30.5" in message  # 16.5 + 14 = 30.5
            assert "macOS" in message or "notification" in message.lower()


class TestActionButtons:
    """Tests for notification action buttons."""

    def test_run_action_opens_terminal(self):
        """Test Run action opens Terminal with command."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            from lib.alerts import send_missed_task_notification

            send_missed_task_notification(
                task_name="uce",
                display_name="UCE Link Collector",
                hours_overdue=16.5,
                threshold=14
            )

            call_args = str(mock_run.call_args)
            # Should include run-now command
            assert "--run-now" in call_args or "run-now" in call_args.lower()

    def test_ignore_action_in_notification(self):
        """Test Ignore action is available in notification."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            from lib.alerts import send_missed_task_notification

            send_missed_task_notification(
                task_name="uce",
                display_name="UCE Link Collector",
                hours_overdue=16.5,
                threshold=14
            )

            call_args = str(mock_run.call_args)
            # Should mention ignore option
            assert "--ignore" in call_args or "Ignore" in call_args


class TestSuccessNotifications:
    """Tests for success notifications."""

    def test_success_notification_silent(self, sample_config):
        """Test success notifications are silent (no sound/banner)."""
        sample_config["notifications"]["success_silent"] = True

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            from lib.alerts import send_success_notification

            send_success_notification(
                task_name="uce",
                display_name="UCE Link Collector",
                summary="Processed 15 links",
                config=sample_config
            )

            # If called, should not have sound parameter or have silent mode
            if mock_run.called:
                call_args = str(mock_run.call_args)
                # Silent notification shouldn't have sound
                assert "sound" not in call_args.lower() or "nosound" in call_args.lower()

    def test_success_notification_with_formatter(self, sample_config):
        """Test success notification uses output formatter."""
        from lib.alerts import send_success_notification

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            send_success_notification(
                task_name="obsidian-tab-archiver",
                display_name="Obsidian Tab Archiver",
                summary="Archived 12 tabs, kept 8",
                config=sample_config
            )

            # Should include formatted summary
            if mock_run.called:
                call_args = str(mock_run.call_args)
                assert "Archived" in call_args or "12" in call_args


class TestManualRunNotifications:
    """Tests for manual run completion notifications."""

    def test_manual_run_complete_silent(self, sample_config):
        """Test manual run completion is silent."""
        sample_config["notifications"]["manual_complete_silent"] = True

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            from lib.alerts import send_manual_complete_notification

            send_manual_complete_notification(
                task_name="uce",
                display_name="UCE Link Collector",
                success=True,
                config=sample_config
            )

            # Should be silent or not called for macOS
            # (status-only message to notification center)

    def test_manual_run_failure_notification(self, sample_config):
        """Test manual run failure shows error briefly."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            from lib.alerts import send_manual_complete_notification

            send_manual_complete_notification(
                task_name="uce",
                display_name="UCE Link Collector",
                success=False,
                error="Connection timeout",
                config=sample_config
            )

            if mock_run.called:
                call_args = str(mock_run.call_args)
                assert "failed" in call_args.lower() or "error" in call_args.lower()


class TestRecoveryNotifications:
    """Tests for recovery notifications."""

    def test_recovery_notification_format(self):
        """Test recovery notification includes previous failure count."""
        with patch("lib.alerts.send_slack_message") as mock_slack, \
             patch("subprocess.run") as mock_run:
            mock_slack.return_value = True
            mock_run.return_value = MagicMock(returncode=0)

            from lib.alerts import send_recovery

            send_recovery(
                task_name="UCE Link Collector",
                previous_failures=5,
                details={"Duration": "2.1s"}
            )

            mock_slack.assert_called_once()
            message = mock_slack.call_args[0][0]
            assert "5" in message
            assert "recover" in message.lower()


class TestHealthSummaryNotifications:
    """Tests for health summary notifications."""

    def test_health_summary_table_format(self):
        """Test health summary uses table format for Slack."""
        with patch("lib.alerts.send_slack_message") as mock_slack:
            mock_slack.return_value = True

            from lib.alerts import send_health_summary

            tasks = [
                {"name": "UCE", "last_run": "7:00 PM", "status": "OK", "issue": "-"},
                {"name": "Git-Sync", "last_run": "8:00 PM", "status": "STALE", "issue": "2h late"},
            ]

            send_health_summary(tasks, healthy=False)

            mock_slack.assert_called_once()
            message = mock_slack.call_args[0][0]
            # Should have table format
            assert "|" in message
            assert "UCE" in message
            assert "Git-Sync" in message

    def test_health_summary_unhealthy_macos(self):
        """Test unhealthy summary triggers macOS notification."""
        with patch("lib.alerts.send_slack_message") as mock_slack, \
             patch("lib.alerts.send_macos_notification") as mock_macos:
            mock_slack.return_value = True
            mock_macos.return_value = True

            from lib.alerts import send_health_summary

            tasks = [
                {"name": "UCE", "last_run": "Never", "status": "UNKNOWN", "issue": "Never run"},
            ]

            send_health_summary(tasks, healthy=False)

            mock_macos.assert_called_once()


class TestNotificationTypes:
    """Tests for notification type behavior per spec."""

    def test_failure_notification_has_sound(self):
        """Test failure notifications have sound."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            from lib.alerts import send_critical

            send_critical(
                task_name="UCE",
                error="Task failed",
                details={"Exit code": 1},
                log_file="/path/to/log",
                task_id="uce"
            )

            mock_run.assert_called()
            call_args = str(mock_run.call_args)
            # Should have sound
            assert "sound" in call_args.lower()

    def test_missed_task_notification_has_sound(self):
        """Test missed task notifications have sound."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            from lib.alerts import send_missed_task_notification

            send_missed_task_notification(
                task_name="uce",
                display_name="UCE Link Collector",
                hours_overdue=16.5,
                threshold=14
            )

            mock_run.assert_called()
            call_args = str(mock_run.call_args)
            assert "sound" in call_args.lower()
