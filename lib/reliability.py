"""
Reliability management for Pinglet.

Provides:
- Automatic retry with exponential backoff + jitter
- Consecutive failure threshold for alert gating
- Alert cooldown/throttling
- Recovery notification coordination
"""
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Tuple, Optional, List

from lib.state import TaskState, load_state, save_state
from lib.logging import log


@dataclass
class ReliabilityConfig:
    """Complete reliability configuration for a task."""
    # Retry settings
    max_retry_attempts: int = 3
    retry_delays: List[float] = None  # [10, 60, 300] seconds
    retry_jitter: float = 0.25  # ±25% randomization

    # Alert gating settings
    alert_threshold: int = 3  # Consecutive failures before alerting
    alert_cooldown_minutes: int = 30  # Minimum time between alerts
    alert_grace_minutes: int = 15  # Wall-clock minutes a streak must persist before alerting

    # Recovery notification
    notify_on_recovery: bool = True

    def __post_init__(self):
        if self.retry_delays is None:
            self.retry_delays = [10, 60, 300]  # 10s, 1min, 5min

    @classmethod
    def from_config(cls, global_config: dict, task_config: dict) -> "ReliabilityConfig":
        """
        Build config from global defaults + task-specific overrides.

        Args:
            global_config: Global reliability config from config.yaml
            task_config: Task-specific reliability overrides

        Returns:
            Merged ReliabilityConfig
        """
        # Start with defaults
        config = cls()

        # Apply global settings
        if global_config:
            retry = global_config.get("retry", {})
            if "max_attempts" in retry:
                config.max_retry_attempts = retry["max_attempts"]
            if "delays_seconds" in retry:
                config.retry_delays = retry["delays_seconds"]
            if "jitter" in retry:
                config.retry_jitter = retry["jitter"]

            alert = global_config.get("alert", {})
            if "consecutive_failures" in alert:
                config.alert_threshold = alert["consecutive_failures"]
            if "cooldown_minutes" in alert:
                config.alert_cooldown_minutes = alert["cooldown_minutes"]
            if "grace_minutes" in alert:
                config.alert_grace_minutes = alert["grace_minutes"]

            if "notify_on_recovery" in global_config:
                config.notify_on_recovery = global_config["notify_on_recovery"]

        # Apply task-specific overrides
        if task_config:
            retry = task_config.get("retry", {})
            if "max_attempts" in retry:
                config.max_retry_attempts = retry["max_attempts"]
            if "delays_seconds" in retry:
                config.retry_delays = retry["delays_seconds"]
            if "jitter" in retry:
                config.retry_jitter = retry["jitter"]

            alert = task_config.get("alert", {})
            if "consecutive_failures" in alert:
                config.alert_threshold = alert["consecutive_failures"]
            if "cooldown_minutes" in alert:
                config.alert_cooldown_minutes = alert["cooldown_minutes"]
            if "grace_minutes" in alert:
                config.alert_grace_minutes = alert["grace_minutes"]

            if "notify_on_recovery" in task_config:
                config.notify_on_recovery = task_config["notify_on_recovery"]

        return config


class ReliabilityManager:
    """
    Coordinates all reliability features for task execution.

    Usage:
        mgr = ReliabilityManager(task_name, config)
        success, exit_code, stdout, stderr = mgr.execute_with_retry(execute_fn)
        # After updating state...
        should_alert, reason = mgr.should_alert()
        if mgr.is_recovery():
            send_recovery(...)
    """

    def __init__(self, task_name: str, config: ReliabilityConfig):
        self.task_name = task_name
        self.config = config
        self.state = load_state(task_name)
        self._previous_consecutive_failures = self.state.consecutive_failures

    def execute_with_retry(
        self,
        execute_fn: Callable[[], Tuple[int, str, str]],
    ) -> Tuple[bool, int, str, str]:
        """
        Execute task with automatic retry on failure.

        Args:
            execute_fn: Function that executes task, returns (exit_code, stdout, stderr)

        Returns:
            (success, final_exit_code, stdout, stderr)
        """
        last_exit_code = 1
        last_stdout = ""
        last_stderr = ""

        for attempt in range(1, self.config.max_retry_attempts + 1):
            if attempt > 1:
                log(f"Retry attempt {attempt}/{self.config.max_retry_attempts}", self.task_name)
            else:
                log(f"Attempt {attempt}/{self.config.max_retry_attempts}", self.task_name)

            exit_code, stdout, stderr = execute_fn()
            last_exit_code = exit_code
            last_stdout = stdout
            last_stderr = stderr

            if exit_code == 0:
                if attempt > 1:
                    log(f"Success on retry attempt {attempt}", self.task_name)
                return True, exit_code, stdout, stderr

            # Don't sleep after last attempt
            if attempt < self.config.max_retry_attempts:
                delay = self._calculate_backoff(attempt)
                log(f"Attempt {attempt} failed (exit={exit_code}), retrying in {delay:.1f}s", self.task_name)
                time.sleep(delay)

        log(f"All {self.config.max_retry_attempts} attempts failed", self.task_name)
        return False, last_exit_code, last_stdout, last_stderr

    def should_alert(self) -> Tuple[bool, str]:
        """
        Determine if an alert should be sent based on:
        - Consecutive failure threshold
        - Alert cooldown period

        Call this AFTER updating state with update_state_failure().

        Returns:
            (should_alert, reason)
        """
        # Reload state to get updated consecutive_failures
        self.state = load_state(self.task_name)

        # Check consecutive failures threshold
        if self.state.consecutive_failures < self.config.alert_threshold:
            return False, f"below_threshold ({self.state.consecutive_failures}/{self.config.alert_threshold})"

        # Check wall-clock grace period — suppresses noise from brief blips that
        # rack up consecutive failures in seconds (e.g. minute-cadence tasks).
        if self.config.alert_grace_minutes > 0 and self.state.first_failure_at:
            try:
                first_fail = datetime.fromisoformat(self.state.first_failure_at)
                grace_end = first_fail + timedelta(minutes=self.config.alert_grace_minutes)
                if datetime.now() < grace_end:
                    remaining = (grace_end - datetime.now()).total_seconds() / 60
                    return False, f"grace_period_active ({remaining:.1f}m remaining)"
            except ValueError:
                pass  # Invalid timestamp, allow alert

        # Check cooldown
        if self.state.last_alert_time:
            try:
                last_alert = datetime.fromisoformat(self.state.last_alert_time)
                cooldown_end = last_alert + timedelta(minutes=self.config.alert_cooldown_minutes)
                if datetime.now() < cooldown_end:
                    remaining = (cooldown_end - datetime.now()).total_seconds() / 60
                    return False, f"cooldown_active ({remaining:.0f}m remaining)"
            except ValueError:
                pass  # Invalid timestamp, allow alert

        return True, "threshold_exceeded_and_no_cooldown"

    def record_alert_sent(self) -> None:
        """Record that an alert was sent. Call after successfully sending alert."""
        self.state = load_state(self.task_name)
        self.state.last_alert_time = datetime.now().isoformat()
        self.state.alerts_sent_this_streak += 1
        save_state(self.state)

    def is_recovery(self) -> bool:
        """
        Check if this is a recovery event (success after failures).

        Call this AFTER updating state with update_state_success().

        Returns:
            True if task recovered from failures
        """
        # Reload state to check was_failing flag. Only treat this as a recovery
        # if a failure alert was actually sent during the streak — otherwise the
        # streak was silent (suppressed by grace/threshold) and recovery would
        # be noise without a corresponding failure message.
        self.state = load_state(self.task_name)
        return (
            self.state.was_failing
            and self.state.alerted_during_streak
            and self.config.notify_on_recovery
        )

    def get_previous_failures(self) -> int:
        """Get the number of consecutive failures before this run."""
        return self._previous_consecutive_failures

    def _calculate_backoff(self, attempt: int) -> float:
        """
        Calculate delay for retry attempt using configured delays + jitter.

        Args:
            attempt: Current attempt number (1-indexed)

        Returns:
            Delay in seconds with jitter applied
        """
        # Get base delay from config (0-indexed into delays list)
        delay_index = min(attempt - 1, len(self.config.retry_delays) - 1)
        base_delay = self.config.retry_delays[delay_index]

        # Add jitter
        jitter_range = base_delay * self.config.retry_jitter
        jitter = random.uniform(-jitter_range, jitter_range)

        return max(0.1, base_delay + jitter)
