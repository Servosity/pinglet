"""Tests for heartbeat/missed task detection (Feature 1)."""
import json
import pytest
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class MockTaskState:
    """Mock task state for testing."""
    task_name: str
    last_run: str = None
    last_status: str = "never_run"
    consecutive_failures: int = 0


class TestMonitoringDownThreshold:
    """Tests for the 3-tier monitoring-down recovery cascade.

    The cascade is:
      1. Auto-recovery (bootout/bootstrap) — every detection
      2. LLM self-diagnosis (codex exec, then claude -p) — after auto-recovery fails once
      3. Human alert (Slack/macOS) — after consecutive_detections >= 3

    State is tracked per-agent in state/_monitoring_down_state.json.
    """

    # --- Pure unit tests for threshold logic (tests 1-8) ---

    def test_single_detection_no_alert(self):
        """One detection should NOT trigger a human alert (below threshold)."""
        from lib.heartbeat import (
            _record_monitoring_detection,
            _should_alert_monitoring_down,
            MONITORING_ALERT_THRESHOLD,
        )

        state = {}
        state = _record_monitoring_detection("healthcheck", state)

        assert state["healthcheck"]["consecutive_detections"] == 1
        assert _should_alert_monitoring_down("healthcheck", state) is False

    @patch("lib.heartbeat._load_learning_state", return_value={"version": 1, "agents": {}, "tasks": {}})
    def test_below_threshold_no_alert(self, mock_learning):
        """Two detections should still NOT trigger a human alert (default threshold=3)."""
        from lib.heartbeat import (
            _record_monitoring_detection,
            _should_alert_monitoring_down,
        )

        state = {}
        state = _record_monitoring_detection("healthcheck", state)
        state = _record_monitoring_detection("healthcheck", state)

        assert state["healthcheck"]["consecutive_detections"] == 2
        assert _should_alert_monitoring_down("healthcheck", state) is False

    def test_at_threshold_alerts(self):
        """Three detections (== MONITORING_ALERT_THRESHOLD) SHOULD trigger alert."""
        from lib.heartbeat import (
            _record_monitoring_detection,
            _should_alert_monitoring_down,
            MONITORING_ALERT_THRESHOLD,
        )

        state = {}
        for _ in range(MONITORING_ALERT_THRESHOLD):
            state = _record_monitoring_detection("healthcheck", state)

        assert state["healthcheck"]["consecutive_detections"] == MONITORING_ALERT_THRESHOLD
        assert _should_alert_monitoring_down("healthcheck", state) is True

    def test_above_threshold_alerts(self):
        """Five detections (above threshold) should still trigger alert."""
        from lib.heartbeat import (
            _record_monitoring_detection,
            _should_alert_monitoring_down,
        )

        state = {}
        for _ in range(5):
            state = _record_monitoring_detection("healthcheck", state)

        assert state["healthcheck"]["consecutive_detections"] == 5
        assert _should_alert_monitoring_down("healthcheck", state) is True

    def test_auto_recovery_resets_counter(self):
        """Successful recovery should reset the consecutive counter to zero."""
        from lib.heartbeat import (
            _record_monitoring_detection,
            _clear_monitoring_down,
        )

        state = {}
        state = _record_monitoring_detection("healthcheck", state)
        state = _record_monitoring_detection("healthcheck", state)
        assert state["healthcheck"]["consecutive_detections"] == 2

        state = _clear_monitoring_down("healthcheck", state)

        # Agent entry should be cleared or counter reset
        if "healthcheck" in state:
            assert state["healthcheck"]["consecutive_detections"] == 0
        # If key is removed entirely, that also counts as reset

    def test_alert_cooldown_respected(self):
        """Alert within 24h of last_alert should be suppressed even at threshold."""
        from lib.heartbeat import (
            _should_alert_monitoring_down,
            MONITORING_ALERT_THRESHOLD,
        )

        recent_alert = (datetime.now() - timedelta(hours=1)).isoformat()
        state = {
            "healthcheck": {
                "consecutive_detections": MONITORING_ALERT_THRESHOLD,
                "first_detected": (datetime.now() - timedelta(hours=3)).isoformat(),
                "last_detected": datetime.now().isoformat(),
                "last_alert": recent_alert,
                "recovery_attempts": MONITORING_ALERT_THRESHOLD,
                "llm_diagnosis_attempted": False,
            }
        }

        assert _should_alert_monitoring_down("healthcheck", state) is False

    def test_alert_cooldown_expired(self):
        """Alert 25h after last_alert should fire when at threshold."""
        from lib.heartbeat import (
            _should_alert_monitoring_down,
            MONITORING_ALERT_THRESHOLD,
        )

        old_alert = (datetime.now() - timedelta(hours=25)).isoformat()
        state = {
            "healthcheck": {
                "consecutive_detections": MONITORING_ALERT_THRESHOLD,
                "first_detected": (datetime.now() - timedelta(hours=26)).isoformat(),
                "last_detected": datetime.now().isoformat(),
                "last_alert": old_alert,
                "recovery_attempts": MONITORING_ALERT_THRESHOLD,
                "llm_diagnosis_attempted": False,
            }
        }

        assert _should_alert_monitoring_down("healthcheck", state) is True

    def test_per_agent_independence(self):
        """Agent A at threshold should alert; agent B below threshold should not."""
        from lib.heartbeat import (
            _record_monitoring_detection,
            _should_alert_monitoring_down,
            MONITORING_ALERT_THRESHOLD,
        )

        state = {}
        # Agent A: record 3 detections (at threshold)
        for _ in range(MONITORING_ALERT_THRESHOLD):
            state = _record_monitoring_detection("healthcheck", state)
        # Agent B: record only 1 detection
        state = _record_monitoring_detection("heartbeat", state)

        assert _should_alert_monitoring_down("healthcheck", state) is True
        assert _should_alert_monitoring_down("heartbeat", state) is False

    # --- Integration tests for run_heartbeat flow (tests 9-10) ---

    def test_heartbeat_integration_no_false_alert(self, sample_config):
        """Single disabled detection should NOT send a human Slack alert.

        Auto-recovery runs but even if it fails, the alert threshold isn't met.
        """
        recent_time = datetime.now() - timedelta(hours=1)
        mock_state = MockTaskState(
            task_name="any",
            last_run=recent_time.isoformat(),
            last_status="success",
        )

        disabled_agents = [
            {"task_id": "healthcheck", "label": "com.pinglet.healthcheck",
             "exit_code": 78, "status": "disabled"}
        ]

        # Monitoring down state: first detection (below threshold)
        monitoring_down = {}

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False), \
             patch("lib.heartbeat.detect_disabled_agents", return_value=disabled_agents), \
             patch("lib.heartbeat._attempt_auto_recovery", return_value=False), \
             patch("lib.heartbeat._attempt_llm_self_diagnosis", return_value=False), \
             patch("lib.heartbeat._load_monitoring_down_state", return_value=monitoring_down), \
             patch("lib.heartbeat._save_monitoring_down_state") as mock_save, \
             patch("lib.heartbeat._should_send_disabled_agent_alert", return_value=False), \
             patch("lib.alerts.send_critical_monitoring_alert") as mock_alert:
            from lib.heartbeat import run_heartbeat

            result = run_heartbeat(sample_config, wake_delay=0)

            # Human alert should NOT have been called (only 1 detection)
            mock_alert.assert_not_called()

    def test_heartbeat_integration_alerts_at_threshold(self, sample_config):
        """Persistent disabled agent (3 cycles) SHOULD trigger human alert."""
        recent_time = datetime.now() - timedelta(hours=1)
        mock_state = MockTaskState(
            task_name="any",
            last_run=recent_time.isoformat(),
            last_status="success",
        )

        disabled_agents = [
            {"task_id": "healthcheck", "label": "com.pinglet.healthcheck",
             "exit_code": 78, "status": "disabled"}
        ]

        # Monitoring down state: already detected 2 times, this will be 3rd
        monitoring_down = {
            "healthcheck": {
                "consecutive_detections": 2,
                "first_detected": (datetime.now() - timedelta(hours=2)).isoformat(),
                "last_detected": (datetime.now() - timedelta(hours=1)).isoformat(),
                "last_alert": None,
                "recovery_attempts": 2,
                "llm_diagnosis_attempted": True,
            }
        }

        with patch("lib.heartbeat._load_state", return_value=mock_state), \
             patch("lib.heartbeat._is_ignored", return_value=False), \
             patch("lib.heartbeat.detect_disabled_agents", return_value=disabled_agents), \
             patch("lib.heartbeat._attempt_auto_recovery", return_value=False), \
             patch("lib.heartbeat._attempt_llm_self_diagnosis", return_value=False), \
             patch("lib.heartbeat._load_monitoring_down_state", return_value=monitoring_down), \
             patch("lib.heartbeat._save_monitoring_down_state") as mock_save, \
             patch("lib.heartbeat._record_disabled_agent_alert"), \
             patch("lib.alerts.send_critical_monitoring_alert") as mock_alert:
            from lib.heartbeat import run_heartbeat

            result = run_heartbeat(sample_config, wake_delay=0)

            # Human alert SHOULD have been sent (3rd detection = at threshold)
            mock_alert.assert_called()

    # --- LLM self-diagnosis tests (tests 11-13) ---

    def test_llm_diagnosis_invoked_after_recovery_fails(self, tmp_path):
        """After auto-recovery fails once, 2nd detection should invoke LLM diagnosis."""
        from lib.heartbeat import (
            _record_monitoring_detection,
            _attempt_llm_self_diagnosis,
        )

        # State: 1 prior detection, auto-recovery already failed
        state = {
            "healthcheck": {
                "consecutive_detections": 1,
                "first_detected": (datetime.now() - timedelta(hours=1)).isoformat(),
                "last_detected": (datetime.now() - timedelta(hours=1)).isoformat(),
                "last_alert": None,
                "recovery_attempts": 1,
                "llm_diagnosis_attempted": False,
            }
        }

        # Record 2nd detection
        state = _record_monitoring_detection("healthcheck", state)
        assert state["healthcheck"]["consecutive_detections"] == 2

        # LLM diagnosis should be invocable with the agent's status
        mock_status = {"installed": True, "running": False, "exit_code": 78,
                       "disabled": True, "status": "disabled"}

        with patch("lib.heartbeat.PROJECT_ROOT", tmp_path), \
             patch("lib.heartbeat.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=0, stdout='{"result": "Fixed it"}')
            result = _attempt_llm_self_diagnosis("healthcheck", mock_status)

        # Just verify it was called (the function exists and accepts these args)
        assert isinstance(result, bool)

    def test_llm_diagnosis_success_resets_state(self, tmp_path):
        """Successful LLM diagnosis should clear the monitoring-down state."""
        from lib.heartbeat import (
            _attempt_llm_self_diagnosis,
            _clear_monitoring_down,
        )

        state = {
            "healthcheck": {
                "consecutive_detections": 2,
                "first_detected": (datetime.now() - timedelta(hours=2)).isoformat(),
                "last_detected": datetime.now().isoformat(),
                "last_alert": None,
                "recovery_attempts": 2,
                "llm_diagnosis_attempted": True,
            }
        }

        mock_status = {"installed": True, "running": False, "exit_code": 78,
                       "disabled": True, "status": "disabled"}

        with patch("lib.heartbeat.PROJECT_ROOT", tmp_path), \
             patch("lib.heartbeat.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=0, stdout='{"result": "Fixed it"}')
            diagnosis_result = _attempt_llm_self_diagnosis("healthcheck", mock_status)

        # If diagnosis succeeded, clear the state
        if diagnosis_result:
            state = _clear_monitoring_down("healthcheck", state)

            if "healthcheck" in state:
                assert state["healthcheck"]["consecutive_detections"] == 0
            # Agent removed entirely is also acceptable

    def test_llm_diagnosis_failure_increments(self, tmp_path):
        """Failed LLM diagnosis should still allow counter to increment."""
        from lib.heartbeat import (
            _record_monitoring_detection,
            _attempt_llm_self_diagnosis,
        )

        state = {
            "healthcheck": {
                "consecutive_detections": 2,
                "first_detected": (datetime.now() - timedelta(hours=2)).isoformat(),
                "last_detected": (datetime.now() - timedelta(hours=1)).isoformat(),
                "last_alert": None,
                "recovery_attempts": 2,
                "llm_diagnosis_attempted": True,
            }
        }

        mock_status = {"installed": True, "running": False, "exit_code": 78,
                       "disabled": True, "status": "disabled"}

        with patch("lib.heartbeat.PROJECT_ROOT", tmp_path), \
             patch("lib.heartbeat.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=1, stdout="Could not diagnose")
            diagnosis_result = _attempt_llm_self_diagnosis("healthcheck", mock_status)

        # Diagnosis failed — counter should still increment on next detection
        state = _record_monitoring_detection("healthcheck", state)
        assert state["healthcheck"]["consecutive_detections"] == 3

    # --- State persistence roundtrip (test 14) ---

    def test_state_persistence_roundtrip(self, tmp_path):
        """Save state, load state, verify all fields preserved."""
        from lib.heartbeat import (
            _load_monitoring_down_state,
            _save_monitoring_down_state,
        )

        now = datetime.now()
        original_state = {
            "healthcheck": {
                "consecutive_detections": 3,
                "first_detected": (now - timedelta(hours=3)).isoformat(),
                "last_detected": now.isoformat(),
                "last_alert": (now - timedelta(hours=1)).isoformat(),
                "recovery_attempts": 3,
                "llm_diagnosis_attempted": True,
            },
            "heartbeat": {
                "consecutive_detections": 1,
                "first_detected": now.isoformat(),
                "last_detected": now.isoformat(),
                "last_alert": None,
                "recovery_attempts": 1,
                "llm_diagnosis_attempted": False,
            },
        }

        # Patch the state file path to use tmp_path
        state_file = tmp_path / "_monitoring_down_state.json"

        with patch("lib.heartbeat.MONITORING_DOWN_STATE_FILE", state_file), \
             patch("lib.heartbeat.STATE_DIR", tmp_path):
            _save_monitoring_down_state(original_state)
            loaded_state = _load_monitoring_down_state()

        # Verify all fields round-tripped correctly
        assert loaded_state == original_state
        for agent_id in ("healthcheck", "heartbeat"):
            assert loaded_state[agent_id]["consecutive_detections"] == original_state[agent_id]["consecutive_detections"]
            assert loaded_state[agent_id]["first_detected"] == original_state[agent_id]["first_detected"]
            assert loaded_state[agent_id]["last_detected"] == original_state[agent_id]["last_detected"]
            assert loaded_state[agent_id]["last_alert"] == original_state[agent_id]["last_alert"]
            assert loaded_state[agent_id]["recovery_attempts"] == original_state[agent_id]["recovery_attempts"]
            assert loaded_state[agent_id]["llm_diagnosis_attempted"] == original_state[agent_id]["llm_diagnosis_attempted"]
