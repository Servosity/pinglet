"""
Heartbeat/Missed Task Detection for Pinglet.

Provides hourly detection of tasks whose last_run exceeds their expected_interval.
When gaps are found, user receives actionable notifications to run or ignore each missed task.
"""
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Tuple, Optional

from lib import state as state_module
from lib import ignored as ignored_module
from lib import alerts as alerts_module
from lib.logging import log

# Re-export for testing patches
STATE_DIR = state_module.STATE_DIR
IGNORED_FILE = ignored_module.IGNORED_FILE
PROJECT_ROOT = Path(__file__).parent.parent


def _load_state(task_name: str):
    """Wrapper for load_state to allow patching."""
    return state_module.load_state(task_name)


def _is_ignored(task_name: str) -> bool:
    """Wrapper for is_ignored to allow patching."""
    return ignored_module.is_ignored(task_name)


def _load_ignored() -> dict:
    """Wrapper for load_ignored to allow patching."""
    return ignored_module.load_ignored()


def _send_missed_task_notification(task_name, display_name, hours_overdue, threshold):
    """Wrapper for send_missed_task_notification to allow patching."""
    return alerts_module.send_missed_task_notification(task_name, display_name, hours_overdue, threshold)


def _send_slack_message(message):
    """Wrapper for send_slack_message to allow patching."""
    return alerts_module.send_slack_message(message)


def _get_launchd_status(task_id: str) -> dict:
    """Wrapper for get_launchd_status to allow patching."""
    from lib.task_manager import get_launchd_status
    return get_launchd_status(task_id)


HEARTBEAT_ALERT_FILE = state_module.STATE_DIR / "_heartbeat_alerts.json"
ALERT_COOLDOWN_HOURS = 24

# Disabled agent alert tracking — prevents re-alerting every heartbeat cycle
DISABLED_AGENT_ALERT_FILE = state_module.STATE_DIR / "_disabled_agent_alerts.json"
DISABLED_AGENT_COOLDOWN_HOURS = 4

# Monitoring down state — threshold before alerting
MONITORING_DOWN_STATE_FILE = state_module.STATE_DIR / "_monitoring_down_state.json"
MONITORING_ALERT_THRESHOLD = 3  # Consecutive detections before human alert
MONITORING_ALERT_COOLDOWN_HOURS = 24

# Learning state
LEARNING_STATE_FILE = state_module.STATE_DIR / "_learning.json"
LEARNING_FILE = LEARNING_STATE_FILE  # Alias for test patching

# Self-diagnosis prompt
SELF_DIAGNOSIS_PROMPT = """Pinglet monitoring agent '{agent_id}' is DOWN. Fix it.

STATUS: {status} | EXIT: {exit_code} | LABEL: com.pinglet.{agent_id}
Auto-recovery (bootout+bootstrap) FAILED.

READ THESE FILES FIRST:
- tail -50 {log_dir}/launchd-heartbeat.log
- tail -50 {log_dir}/pinglet.log
- cat ~/Library/LaunchAgents/com.pinglet.{agent_id}.plist

LEARNING CONTEXT: Read {project_root}/state/_learning.json for previous diagnosis history and known issues for this agent. Check the agents.{agent_id}.known_issues array.

KNOWN FAILURE MODES:
- Exit 78 = KeepAlive + StartCalendarInterval conflict (remove KeepAlive from plist)
- Exit 1 after bootstrap = plist syntax error or missing binary
- "not_loaded" = plist removed from ~/Library/LaunchAgents/

FIX SEQUENCE:
1. Read logs + plist + learning context to identify root cause
2. Fix the root cause (edit plist, fix permissions, etc.)
3. launchctl bootout gui/$(id -u)/com.pinglet.{agent_id} 2>/dev/null
4. launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pinglet.{agent_id}.plist
5. Verify: launchctl list | grep {agent_id} shows PID or exit 0

RULES:
- Do NOT modify task scripts, only LaunchAgent plists
- Do NOT disable other agents
- Exit 0 if fixed. Exit 1 if you cannot fix it."""


def _load_heartbeat_alert_state() -> dict:
    """Load per-task heartbeat alert state from disk."""
    if HEARTBEAT_ALERT_FILE.exists():
        try:
            with open(HEARTBEAT_ALERT_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _save_heartbeat_alert_state(state: dict) -> None:
    """Save per-task heartbeat alert state to disk."""
    state_module.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(HEARTBEAT_ALERT_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _should_alert_for_task(task_name: str, escalation: str, alert_state: dict) -> bool:
    """Check if we should send a Slack alert for this task.

    Only re-alerts after ALERT_COOLDOWN_HOURS or if escalation level changed.

    Args:
        task_name: Task identifier
        escalation: Current escalation level (warning/urgent/critical)
        alert_state: Current alert state dict

    Returns:
        True if alert should be sent
    """
    prev = alert_state.get(task_name)
    if not prev:
        return True

    try:
        prev_time = datetime.fromisoformat(prev.get("last_alert", "2000-01-01"))
        hours_since = (datetime.now() - prev_time).total_seconds() / 3600

        # Re-alert if cooldown expired or escalation level increased
        if hours_since >= ALERT_COOLDOWN_HOURS:
            return True
        if prev.get("escalation") != escalation:
            return True
        return False
    except (ValueError, TypeError):
        return True


def detect_disabled_agents(config: dict) -> List[Dict]:
    """Detect LaunchAgents that are truly disabled by launchd (exit code 78).

    Only flags agents with exit code 78 (config error / disabled by launchd).
    Does NOT flag agents with status "failed" — a non-zero exit code from a
    task run is handled by the task's own reliability system (retries +
    consecutive failure threshold). Monitoring agents (heartbeat, healthcheck)
    exit 1 when they find issues, which is normal operation, not a dead agent.

    Args:
        config: Full configuration dictionary

    Returns:
        List of dicts with keys: task_id, label, exit_code, status
    """
    from lib.task_manager import MONITORING_AGENTS

    tasks_config = config.get("tasks", {})
    all_ids = list(tasks_config.keys()) + [a for a in MONITORING_AGENTS if a not in tasks_config]

    disabled = []
    for task_id in all_ids:
        status = _get_launchd_status(task_id)
        if status["disabled"]:
            disabled.append({
                "task_id": task_id,
                "label": f"com.pinglet.{task_id}",
                "exit_code": status["exit_code"],
                "status": status["status"],
            })

    return disabled


def _load_disabled_agent_alert_state() -> dict:
    """Load disabled agent alert tracking state."""
    if DISABLED_AGENT_ALERT_FILE.exists():
        try:
            with open(DISABLED_AGENT_ALERT_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _save_disabled_agent_alert_state(state: dict) -> None:
    """Save disabled agent alert tracking state."""
    state_module.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(DISABLED_AGENT_ALERT_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _should_send_disabled_agent_alert(disabled: list) -> bool:
    """Check if we should send a Slack alert for disabled agents.

    Applies cooldown to prevent hourly re-alerting for the same set of
    disabled agents. Re-alerts if the set changes or cooldown expires.

    Args:
        disabled: List of disabled agent dicts from detect_disabled_agents()

    Returns:
        True if alert should be sent
    """
    state = _load_disabled_agent_alert_state()
    now = datetime.now()
    current_agents = sorted(d["task_id"] for d in disabled)

    prev_time_str = state.get("last_alert_time")
    prev_agents = sorted(state.get("agents", []))

    if not prev_time_str:
        return True

    try:
        prev_time = datetime.fromisoformat(prev_time_str)
        hours_since = (now - prev_time).total_seconds() / 3600

        # Re-alert if cooldown expired or set of disabled agents changed
        if hours_since >= DISABLED_AGENT_COOLDOWN_HOURS:
            return True
        if current_agents != prev_agents:
            return True
        return False
    except (ValueError, TypeError):
        return True


def _record_disabled_agent_alert(disabled: list) -> None:
    """Record that a disabled agent alert was sent."""
    state_module.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "last_alert_time": datetime.now().isoformat(),
        "agents": sorted(d["task_id"] for d in disabled),
    }
    _save_disabled_agent_alert_state(state)


def _load_monitoring_down_state() -> dict:
    """Load monitoring down tracking state from disk."""
    if MONITORING_DOWN_STATE_FILE.exists():
        try:
            with open(MONITORING_DOWN_STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _save_monitoring_down_state(state: dict) -> None:
    """Save monitoring down tracking state to disk."""
    state_module.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(MONITORING_DOWN_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _record_monitoring_detection(agent_id: str, state: dict) -> dict:
    """Record a detection of a monitoring agent being down."""
    now = datetime.now().isoformat()
    if agent_id not in state:
        state[agent_id] = {
            "consecutive_detections": 0,
            "first_detected": now,
            "last_detected": now,
            "last_alert": None,
            "recovery_attempts": 0,
            "llm_diagnosis_attempted": False,
        }
    entry = state[agent_id]
    entry["consecutive_detections"] += 1
    entry["last_detected"] = now
    entry["recovery_attempts"] += 1
    return state


def _should_alert_monitoring_down(agent_id: str, state: dict) -> bool:
    """Check if we should send a human alert for this agent.

    Only alerts after MONITORING_ALERT_THRESHOLD consecutive detections.
    Respects MONITORING_ALERT_COOLDOWN_HOURS between alerts.
    Uses learned effective_threshold if available.
    """
    entry = state.get(agent_id, {})
    detections = entry.get("consecutive_detections", 0)

    # Check learned threshold
    learning = _load_learning_state()
    agent_learning = learning.get("agents", {}).get(agent_id, {})
    threshold = agent_learning.get("effective_threshold", MONITORING_ALERT_THRESHOLD)

    if detections < threshold:
        return False

    # Check cooldown
    last_alert = entry.get("last_alert")
    if last_alert:
        try:
            last_alert_dt = datetime.fromisoformat(last_alert)
            hours_since = (datetime.now() - last_alert_dt).total_seconds() / 3600
            if hours_since < MONITORING_ALERT_COOLDOWN_HOURS:
                return False
        except (ValueError, TypeError):
            pass

    return True


def _clear_monitoring_down(agent_id: str, state: dict) -> dict:
    """Clear monitoring down state for an agent (after recovery)."""
    if agent_id in state:
        del state[agent_id]
    return state


def _attempt_auto_recovery(agent_id: str) -> str:
    """Attempt auto-recovery of a disabled monitoring agent.

    Uses bootout+bootstrap sequence.

    Returns:
        "recovered" - bootstrap succeeded AND agent wasn't in a reload cycle
        "reloaded" - bootstrap succeeded but agent keeps cycling (not a real fix)
        "failed" - bootstrap failed or agent still disabled
    """
    label = f"com.pinglet.{agent_id}"
    user_plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"

    if not user_plist.exists():
        log(f"Cannot recover {agent_id}: plist not found", "heartbeat")
        return "failed"

    try:
        uid = subprocess.check_output(["id", "-u"]).decode().strip()

        # Bootout (ignore errors)
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{label}"],
            capture_output=True, timeout=10,
        )

        # Bootstrap
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(user_plist)],
            capture_output=True, text=True, timeout=10,
        )

        if result.returncode != 0:
            log(f"Auto-recovery failed for {agent_id}: {result.stderr.strip()}", "heartbeat")
            return "failed"

        # Verify recovery
        status = _get_launchd_status(agent_id)
        if status["disabled"]:
            log(f"Auto-recovery failed for {agent_id}: still disabled after bootstrap", "heartbeat")
            return "failed"

        # Check learning history — if we keep "recovering" this agent but it
        # keeps appearing disabled, bootstrap is just resetting LastExitStatus
        # without fixing the underlying problem (e.g. the task script itself
        # exits 78 or the task keeps failing).
        learning = _load_learning_state()
        agent_learning = learning.get("agents", {}).get(agent_id, {})
        consecutive = agent_learning.get("consecutive_auto_recoveries", 0)

        if consecutive >= 3:
            log(f"Auto-recovery for {agent_id} is a reload cycle "
                f"({consecutive} consecutive reloads, not real recoveries)", "heartbeat")
            return "reloaded"

        log(f"Auto-recovery succeeded for {agent_id}", "heartbeat")
        return "recovered"
    except Exception as e:
        log(f"Auto-recovery error for {agent_id}: {e}", "heartbeat")
        return "failed"


def _attempt_llm_self_diagnosis(agent_id: str, status: dict) -> bool:
    """Invoke claude -p to diagnose and fix a disabled monitoring agent.

    Returns True if the agent appears healthy after diagnosis.
    """
    log_dir = PROJECT_ROOT / "logs"

    prompt = SELF_DIAGNOSIS_PROMPT.format(
        agent_id=agent_id,
        status=status.get("status", "unknown"),
        exit_code=status.get("exit_code", "unknown"),
        log_dir=str(log_dir),
        project_root=str(PROJECT_ROOT),
    )

    cmd = [
        "claude",
        "-p", prompt,
        "--allowedTools", "Read,Bash(launchctl *),Bash(cat *),Bash(ls *),Bash(grep *),Edit",
        "--output-format", "json",
        "--max-turns", "5",
        "--max-budget-usd", "1.00",
        "--no-session-persistence",
    ]

    log(f"Invoking LLM self-diagnosis for {agent_id}", "heartbeat")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(PROJECT_ROOT),
        )

        # Log output
        log_file = log_dir / f"{agent_id}-self-diagnosis.log"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_file, "w") as f:
            f.write(f"Exit code: {result.returncode}\n")
            f.write(f"Stdout:\n{result.stdout}\n")
            f.write(f"Stderr:\n{result.stderr}\n")

        if result.returncode == 0:
            # Verify agent is actually healthy now
            new_status = _get_launchd_status(agent_id)
            if not new_status["disabled"]:
                log(f"LLM self-diagnosis fixed {agent_id}", "heartbeat")
                _update_learning_state(agent_id, "llm_recovery", {
                    "summary": f"LLM fixed {agent_id} after auto-recovery failed",
                })
                return True
            else:
                log(f"LLM claimed success but {agent_id} still disabled", "heartbeat")
                return False
        else:
            log(f"LLM self-diagnosis failed for {agent_id} (exit {result.returncode})", "heartbeat")
            return False
    except subprocess.TimeoutExpired:
        log(f"LLM self-diagnosis timed out for {agent_id}", "heartbeat")
        return False
    except FileNotFoundError:
        log(f"claude CLI not found — cannot run self-diagnosis", "heartbeat")
        return False
    except Exception as e:
        log(f"LLM self-diagnosis error for {agent_id}: {e}", "heartbeat")
        return False


def _load_learning_state() -> dict:
    """Load learning state from disk."""
    if LEARNING_FILE.exists():
        try:
            with open(LEARNING_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {"version": 1, "agents": {}, "tasks": {}}


def _save_learning_state(state: dict) -> None:
    """Save learning state to disk."""
    state_module.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LEARNING_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _update_learning_state(agent_id: str, outcome: str, details: dict = None) -> None:
    """Update learning state after a heartbeat cycle detection.

    Args:
        agent_id: The monitoring agent ID
        outcome: One of "auto_recovery", "llm_recovery", "human_alert", "llm_failed"
        details: Optional details (e.g., llm output summary)
    """
    state = _load_learning_state()
    agents = state.setdefault("agents", {})

    if agent_id not in agents:
        agents[agent_id] = {
            "pattern": "unknown",
            "total_detections": 0,
            "total_auto_recoveries": 0,
            "total_llm_recoveries": 0,
            "total_human_alerts": 0,
            "consecutive_auto_recoveries": 0,
            "effective_threshold": MONITORING_ALERT_THRESHOLD,
            "last_detection": None,
            "known_issues": [],
            "suppressed": False,
            "suppressed_reason": None,
        }

    entry = agents[agent_id]
    entry["total_detections"] += 1
    entry["last_detection"] = datetime.now().isoformat()

    if outcome == "auto_recovery":
        entry["total_auto_recoveries"] += 1
        entry["consecutive_auto_recoveries"] += 1
    elif outcome == "reload_cycle":
        # Bootstrap succeeded but agent keeps cycling — NOT a real recovery.
        # Don't increment auto_recoveries or consecutive count.
        entry.setdefault("total_reload_cycles", 0)
        entry["total_reload_cycles"] += 1
        entry["consecutive_auto_recoveries"] = 0  # Break the false streak
    elif outcome == "llm_recovery":
        entry["total_llm_recoveries"] += 1
        entry["consecutive_auto_recoveries"] = 0  # Reset consecutive auto
        entry["total_reload_cycles"] = 0  # LLM actually fixed it
        # Extract known issue from details
        if details and details.get("summary"):
            issue = details["summary"][:200]
            if issue not in entry["known_issues"]:
                entry["known_issues"].append(issue)
    elif outcome == "human_alert":
        entry["total_human_alerts"] += 1
        entry["consecutive_auto_recoveries"] = 0
    elif outcome == "llm_failed":
        entry["consecutive_auto_recoveries"] = 0

    # Detect patterns and adapt thresholds
    _detect_and_adapt(entry)

    _save_learning_state(state)


def _detect_and_adapt(entry: dict) -> None:
    """Detect patterns and adapt thresholds for an agent learning entry."""
    total = entry["total_detections"]
    auto = entry["total_auto_recoveries"]
    consecutive = entry["consecutive_auto_recoveries"]
    human = entry["total_human_alerts"]
    reload_cycles = entry.get("total_reload_cycles", 0)

    if reload_cycles >= 3:
        # Reload cycle: bootstrap keeps "fixing" it but it keeps coming back.
        # The underlying task/script is broken, not the launchd config.
        entry["pattern"] = "reload_cycle"
        entry["effective_threshold"] = max(2, MONITORING_ALERT_THRESHOLD - 1)
        entry["suppressed"] = False
        entry["suppressed_reason"] = None
    elif total >= 10 and auto == total and consecutive >= 10:
        # Chronic cycle: always detected, always auto-recovers
        entry["pattern"] = "chronic_cycle"
        entry["effective_threshold"] = max(10, entry.get("effective_threshold", MONITORING_ALERT_THRESHOLD))
        entry["suppressed"] = True
        entry["suppressed_reason"] = f"{consecutive} consecutive auto-recoveries, no human intervention needed"
    elif total >= 5 and human > 0 and (auto / max(total, 1)) < 0.5:
        # Persistent: recovery failing often
        entry["pattern"] = "persistent"
        entry["effective_threshold"] = max(2, MONITORING_ALERT_THRESHOLD - 1)
        entry["suppressed"] = False
        entry["suppressed_reason"] = None
    elif total >= 3:
        # Intermittent: occasional detections, mostly recovers
        if entry["pattern"] == "unknown":
            entry["pattern"] = "intermittent"
    # Leave threshold at default for intermittent/unknown


def _update_task_learning(task_id: str, outcome: str, details: dict = None) -> None:
    """Update learning state after an on_failure callback.

    Args:
        task_id: Task identifier
        outcome: One of "invoked", "fixed", "failed"
        details: Optional details (error patterns, etc.)
    """
    state = _load_learning_state()
    tasks = state.setdefault("tasks", {})

    if task_id not in tasks:
        tasks[task_id] = {
            "total_failures": 0,
            "total_on_failure_invocations": 0,
            "total_on_failure_fixes": 0,
            "failure_patterns": [],
            "prompt_improvements": [],
        }

    entry = tasks[task_id]
    entry["total_failures"] += 1

    if outcome == "invoked":
        entry["total_on_failure_invocations"] += 1
    elif outcome == "fixed":
        entry["total_on_failure_invocations"] += 1
        entry["total_on_failure_fixes"] += 1
    elif outcome == "failed":
        entry["total_on_failure_invocations"] += 1
        # Record failure pattern if provided
        if details and details.get("error_pattern"):
            pattern = details["error_pattern"][:200]
            if pattern not in entry["failure_patterns"]:
                entry["failure_patterns"].append(pattern)

    _save_learning_state(state)


def get_escalation_level(hours_overdue: float, threshold: float) -> str:
    """Determine escalation level based on staleness multiplier.

    Args:
        hours_overdue: Hours past threshold
        threshold: Expected interval in hours

    Returns:
        Escalation level: "warning", "urgent", or "critical"
    """
    total_hours = hours_overdue + threshold
    if threshold <= 0:
        return "warning"

    multiplier = total_hours / threshold
    if multiplier >= 10:
        return "critical"
    elif multiplier >= 5:
        return "urgent"
    return "warning"


def detect_missed_tasks(config: dict) -> List[Dict]:
    """
    Detect tasks that haven't run within their expected interval.

    Uses existing healthcheck.expected_intervals configuration.

    Args:
        config: Full configuration dictionary

    Returns:
        List of missed task info dicts with keys:
        - task_name: Task identifier
        - display_name: Human-readable name
        - hours_overdue: Hours past threshold
        - threshold: Expected interval in hours
        - last_run: ISO timestamp of last run
        - never_run: True if task has never run
    """
    expected_intervals = config.get("healthcheck", {}).get("expected_intervals", {})
    tasks_config = config.get("tasks", {})

    missed = []
    now = datetime.now()

    for task_name, threshold in expected_intervals.items():
        # Skip ignored tasks
        if _is_ignored(task_name):
            log(f"Skipping ignored task: {task_name}", "heartbeat")
            continue

        # Load task state
        state = _load_state(task_name)
        display_name = tasks_config.get(task_name, {}).get("name", task_name)

        if not state.last_run:
            # Task has never run
            missed.append({
                "task_name": task_name,
                "display_name": display_name,
                "hours_overdue": float(threshold),  # Treat as fully overdue
                "threshold": threshold,
                "last_run": None,
                "never_run": True,
            })
            continue

        # Calculate hours since last run
        try:
            last_run_dt = datetime.fromisoformat(state.last_run)
            hours_since = (now - last_run_dt).total_seconds() / 3600

            if hours_since > threshold:
                missed.append({
                    "task_name": task_name,
                    "display_name": display_name,
                    "hours_overdue": round(hours_since - threshold, 1),
                    "threshold": threshold,
                    "last_run": state.last_run,
                    "never_run": False,
                })
        except ValueError:
            log(f"Invalid last_run timestamp for {task_name}", "heartbeat")

    return missed


def should_run_task(task_name: str, config: dict) -> Tuple[bool, str]:
    """
    Check if a task should be run via --run-now.

    Handles stale notification case where task already ran.

    Args:
        task_name: Name of the task to check
        config: Full configuration dictionary

    Returns:
        (should_run, reason) tuple
    """
    expected_intervals = config.get("healthcheck", {}).get("expected_intervals", {})
    threshold = expected_intervals.get(task_name)

    if not threshold:
        # No expected interval defined, always allow run
        return True, "no_threshold_defined"

    state = _load_state(task_name)

    if not state.last_run:
        return True, "never_run"

    try:
        last_run_dt = datetime.fromisoformat(state.last_run)
        hours_since = (datetime.now() - last_run_dt).total_seconds() / 3600

        if hours_since > threshold:
            return True, f"missed_by_{hours_since - threshold:.1f}h"
        else:
            last_run_str = last_run_dt.strftime("%I:%M%p").lower()
            return False, f"already ran at {last_run_str}"
    except ValueError:
        return True, "invalid_timestamp"


def run_heartbeat(config: dict, wake_delay: Optional[int] = None) -> dict:
    """
    Run the heartbeat check.

    Detects missed tasks and sends notifications.

    Args:
        config: Full configuration dictionary
        wake_delay: Seconds to wait before notifying (default from config)

    Returns:
        Result dict with missed_count and tasks
    """
    heartbeat_config = config.get("heartbeat", {})

    if wake_delay is None:
        wake_delay = heartbeat_config.get("wake_delay_seconds", 30)

    log("Running heartbeat check", "heartbeat")

    # Check for disabled agents with 3-tier recovery cascade
    disabled = detect_disabled_agents(config)
    monitoring_down_state = _load_monitoring_down_state()

    if disabled:
        disabled_names = [d["task_id"] for d in disabled]
        log(f"Detected {len(disabled)} disabled agent(s): {', '.join(disabled_names)}", "heartbeat")

        for agent_info in disabled:
            agent_id = agent_info["task_id"]

            # Check if suppressed by learning
            learning = _load_learning_state()
            agent_learning = learning.get("agents", {}).get(agent_id, {})

            # Record detection
            monitoring_down_state = _record_monitoring_detection(agent_id, monitoring_down_state)
            entry = monitoring_down_state[agent_id]

            # Tier 1: Always attempt auto-recovery
            recovery_result = _attempt_auto_recovery(agent_id)

            if recovery_result == "recovered":
                log(f"Auto-recovery succeeded for {agent_id}", "heartbeat")
                monitoring_down_state = _clear_monitoring_down(agent_id, monitoring_down_state)
                _update_learning_state(agent_id, "auto_recovery")
                continue
            elif recovery_result == "reloaded":
                # Bootstrap worked but this agent keeps cycling — don't count
                # as real recovery, escalate to next tier
                log(f"Agent {agent_id} is in a reload cycle, escalating", "heartbeat")
                _update_learning_state(agent_id, "reload_cycle")

            # Tier 2: LLM self-diagnosis (after auto-recovery fails, attempt once)
            if not entry.get("llm_diagnosis_attempted", False):
                entry["llm_diagnosis_attempted"] = True
                llm_success = _attempt_llm_self_diagnosis(agent_id, agent_info)

                if llm_success:
                    log(f"LLM self-diagnosis fixed {agent_id}", "heartbeat")
                    monitoring_down_state = _clear_monitoring_down(agent_id, monitoring_down_state)
                    _update_learning_state(agent_id, "llm_recovery", {"summary": f"LLM fixed {agent_id} after auto-recovery failed"})
                    continue
                else:
                    _update_learning_state(agent_id, "llm_failed")

            # Tier 3: Human alert (after threshold met)
            if agent_learning.get("suppressed", False):
                log(f"Skipping alert for {agent_id}: suppressed by learning ({agent_learning.get('suppressed_reason', 'chronic auto-recovery')})", "heartbeat")
                _update_learning_state(agent_id, "auto_recovery")  # Still count as detection
                continue

            if _should_alert_monitoring_down(agent_id, monitoring_down_state):
                from lib.alerts import send_critical_monitoring_alert
                send_critical_monitoring_alert([agent_info])
                monitoring_down_state[agent_id]["last_alert"] = datetime.now().isoformat()
                _update_learning_state(agent_id, "human_alert")
            else:
                log(f"Alert suppressed for {agent_id} (below threshold or cooldown)", "heartbeat")

    # Clear monitoring down state for healthy agents
    all_disabled_ids = {d["task_id"] for d in disabled}
    for agent_id in list(monitoring_down_state.keys()):
        if agent_id not in all_disabled_ids:
            log(f"Clearing monitoring down state for {agent_id} (now healthy)", "heartbeat")
            monitoring_down_state = _clear_monitoring_down(agent_id, monitoring_down_state)

    _save_monitoring_down_state(monitoring_down_state)

    # Detect missed tasks
    missed_tasks = detect_missed_tasks(config)

    if not missed_tasks and not disabled:
        log("All tasks up to date", "heartbeat")
        return {"missed_count": 0, "tasks": [], "disabled_agents": []}

    if missed_tasks:
        log(f"Found {len(missed_tasks)} missed task(s)", "heartbeat")

    # Wait for wake delay (allows system to stabilize after wake)
    if wake_delay > 0 and missed_tasks:
        log(f"Waiting {wake_delay}s wake delay before notifying", "heartbeat")
        time.sleep(wake_delay)

    # Load per-task alert cooldown state
    alert_state = _load_heartbeat_alert_state()

    # Send notifications for each missed task
    for task in missed_tasks:
        task_name = task["task_name"]
        display_name = task["display_name"]
        hours_overdue = task["hours_overdue"]
        threshold = task["threshold"]
        last_run = task.get("last_run")

        # Determine escalation level
        level = get_escalation_level(hours_overdue, threshold)
        task["escalation"] = level

        level_prefix = ""
        if level == "critical":
            level_prefix = "CRITICAL: "
        elif level == "urgent":
            level_prefix = "URGENT: "

        log(f"Notifying for missed task: {task_name} ({hours_overdue:.1f}h overdue, {level})", "heartbeat")

        # Send macOS notification with actions (always — lightweight, local)
        _send_missed_task_notification(
            task_name=task_name,
            display_name=display_name,
            hours_overdue=hours_overdue,
            threshold=threshold,
        )

        # Send Slack notification only if cooldown allows
        if _should_alert_for_task(task_name, level, alert_state):
            if last_run:
                try:
                    last_run_str = datetime.fromisoformat(last_run).strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    last_run_str = "Unknown"
            else:
                last_run_str = "Never"

            slack_message = f"""*Pinglet: {level_prefix}Missed Task*
`{task_name}` hasn't run in {hours_overdue + threshold:.1f} hours (threshold: {threshold}h)
Last successful run: {last_run_str}
Escalation: {level.upper()}

_Use macOS notification to Run or Ignore_"""

            _send_slack_message(slack_message)

            # Update cooldown state for this task
            alert_state[task_name] = {
                "last_alert": datetime.now().isoformat(),
                "escalation": level,
            }
        else:
            log(f"Slack alert suppressed for {task_name} (cooldown active)", "heartbeat")

    # Save updated alert state
    _save_heartbeat_alert_state(alert_state)

    return {
        "missed_count": len(missed_tasks),
        "tasks": missed_tasks,
        "disabled_agents": disabled,
    }
