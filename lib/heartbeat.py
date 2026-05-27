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
from lib.task_manager import MONITORING_AGENTS, _strip_macl_from_logs

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


def _clear_stale_ignores() -> list:
    """Wrapper for clear_stale_ignores to allow patching."""
    return ignored_module.clear_stale_ignores()


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


def _get_launchd_run_count(task_id: str):
    """Wrapper for get_launchd_run_count to allow patching."""
    from lib.task_manager import get_launchd_run_count
    return get_launchd_run_count(task_id)


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

# Task diagnosis state — tracks per-task LLM diagnosis attempts
TASK_DIAGNOSIS_STATE_FILE = state_module.STATE_DIR / "_task_diagnosis.json"
TASK_DIAGNOSIS_COOLDOWN_HOURS = 6  # Don't re-invoke LLM more than once per 6h per task
TASK_DIAGNOSIS_MIN_DETECTIONS = 2  # Need 2+ heartbeat detections before invoking LLM
TASK_DIAGNOSIS_CHRONIC_THRESHOLD = 5  # After this many recurring fix failures, stop LLM and alert human
TASK_FIX_DURABILITY_HOURS = 24  # If task re-breaks within this window after a "fix", it's recurring

# Task-level diagnosis prompt — for regular tasks (not monitoring agents)
# Unlike SELF_DIAGNOSIS_PROMPT, this allows fixing the working directory and dependencies
TASK_DIAGNOSE_PROMPT = """Pinglet task '{task_id}' anomaly: {detected_problem}.

TASK CONFIG:
- Name: {task_name}
- Command: {command}
- Working dir: {working_dir}
- Schedule: {schedule}
- Last run: {last_run}
- Consecutive failures: {consecutive_failures}

LAUNCHD STATUS: {launchd_status}
Recovery attempts so far: {recovery_attempts}

READ THESE FILES FIRST:
- tail -50 {log_dir}/pinglet.log | grep {task_id}
- tail -50 {log_dir}/{task_id}.log
- tail -50 {log_dir}/{task_id}.err
- cat {state_file}

LEARNING CONTEXT: Read {learning_file} for previous diagnosis history.

DIAGNOSIS STEPS:
1. Read logs to understand what happened
2. cd to working directory and investigate:
   - Does the command exist and is it executable?
   - Are dependencies intact (venv, git repos, config files)?
   - Check for broken git state (submodules, nested repos, dirty index)
   - Check for permission issues
   - Check for missing environment variables
3. Fix the root cause in the working directory
4. If launchd disabled (exit 78): fix plist, then bootout+bootstrap
5. If stale trigger: disable+enable the task via pinglet CLI
6. Verify the fix by running: {python_path} {project_root}/pinglet.py --run-now {task_id}

RULES:
- Fix the ROOT CAUSE, not just symptoms
- You MAY modify files in the working directory if that's where the bug is
- Do NOT modify other tasks or their working directories
- Do NOT delete data files unless they are clearly corrupt temp files
- Exit 0 if fixed (task runs successfully). Exit 1 if human intervention needed."""

# Prepended to TASK_DIAGNOSE_PROMPT when a previous fix didn't stick
RECURRING_FAILURE_CONTEXT = """
WARNING - RECURRING FAILURE: This task has been "fixed" {recurring_fix_failures} time(s) but keeps breaking.
Previous fix attempt: {last_fix_summary}
Total LLM diagnosis invocations for this task: {total_diagnose_invocations}

The previous approach did NOT persist. The standard re-bootstrap approach is NOT working.

You MUST try a DIFFERENT approach than re-bootstrapping. Consider:
- If this is a stale StartCalendarInterval: switch the plist to use StartInterval (seconds-based)
- If this is a permission or environment issue: check for machine-level changes
- If the task script itself is broken: fix the script, not just the schedule
- If the working directory has issues (git state, missing deps): fix those
- If you cannot fix the root cause: exit 1 to escalate to human intervention
"""

# Self-diagnosis prompt (monitoring agents only — restricted to plist fixes)
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


def _should_recover_stale_trigger(task_id: str, task_diagnosis_state: dict,
                                    expected_interval: float) -> bool:
    """Check if stale trigger recovery should be attempted for a task.

    After a stale trigger recovery, applies a cooldown equal to the task's
    expected_interval before attempting another recovery. This prevents
    infinite re-bootstrap loops for long-interval tasks (e.g., weekly)
    where runs=0 is normal between scheduled fire times.

    Args:
        task_id: Task identifier
        task_diagnosis_state: Per-task diagnosis tracking state
        expected_interval: Task's expected interval in hours (from healthcheck config)

    Returns:
        True if recovery should be attempted
    """
    entry = task_diagnosis_state.get(task_id, {})
    last_recovery = entry.get("last_stale_recovery")

    if not last_recovery:
        return True

    try:
        last_dt = datetime.fromisoformat(last_recovery)
        hours_since = (datetime.now() - last_dt).total_seconds() / 3600
        # Cooldown = expected_interval, minimum 2 hours
        cooldown = max(expected_interval, 2.0)
        return hours_since >= cooldown
    except (ValueError, TypeError):
        return True


def _record_stale_recovery(task_id: str, task_diagnosis_state: dict) -> None:
    """Record that a stale trigger recovery was attempted for a task.

    Args:
        task_id: Task identifier
        task_diagnosis_state: Per-task diagnosis tracking state (modified in-place)
    """
    now = datetime.now().isoformat()
    task_diagnosis_state.setdefault(task_id, _new_diagnosis_entry(now, "stale_recovery"))
    task_diagnosis_state[task_id]["last_stale_recovery"] = now


def detect_stale_triggers(missed_tasks: List[Dict]) -> List[Dict]:
    """Detect missed tasks caused by stale launchd calendar triggers.

    When launchd bootstraps an agent but the calendar event trigger gets stuck,
    the agent shows runs=0 and never fires. This function identifies such tasks.

    Args:
        missed_tasks: List of missed task dicts from detect_missed_tasks()

    Returns:
        Subset of missed_tasks that have stale triggers (runs=0, agent loaded)
    """
    stale = []
    for task in missed_tasks:
        task_name = task["task_name"]
        status = _get_launchd_status(task_name)

        # Only check tasks that are loaded and not disabled
        if not status.get("installed") or status.get("disabled"):
            continue
        if status.get("status") in ("not_loaded", "not_installed"):
            continue

        run_count = _get_launchd_run_count(task_name)
        if run_count is not None and run_count == 0:
            log(f"Stale trigger detected for {task_name}: loaded but runs=0", "heartbeat")
            stale.append(task)

    return stale


def recover_stale_trigger(task_id: str) -> str:
    """Recover a stale launchd calendar trigger via bootout+bootstrap.

    Args:
        task_id: Task identifier

    Returns:
        "recovered" if bootstrap succeeded, "failed" otherwise
    """
    label = f"com.pinglet.{task_id}"
    user_plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"

    if not user_plist.exists():
        log(f"Cannot recover stale trigger for {task_id}: plist not found", "heartbeat")
        return "failed"

    try:
        uid = subprocess.check_output(["id", "-u"]).decode().strip()

        # Bootout (ignore errors — agent may not be loaded)
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{label}"],
            capture_output=True, timeout=10,
        )

        # Strip MACL xattrs from log files (prevents launchd exit 78)
        _strip_macl_from_logs(task_id)

        # Bootstrap
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(user_plist)],
            capture_output=True, text=True, timeout=10,
        )

        if result.returncode != 0:
            log(f"Stale trigger recovery failed for {task_id}: {result.stderr.strip()}", "heartbeat")
            return "failed"

        # Verify — run count should reset (may still be 0 since trigger hasn't fired yet)
        new_count = _get_launchd_run_count(task_id)
        if new_count is not None and new_count >= 0:
            log(f"Stale trigger recovery succeeded for {task_id} (re-bootstrapped)", "heartbeat")
            return "recovered"

        log(f"Stale trigger recovery uncertain for {task_id}", "heartbeat")
        return "recovered"

    except Exception as e:
        log(f"Stale trigger recovery error for {task_id}: {e}", "heartbeat")
        return "failed"


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

        # Strip MACL xattrs from log files (prevents launchd exit 78)
        _strip_macl_from_logs(agent_id)

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


def _load_task_diagnosis_state() -> dict:
    """Load per-task diagnosis tracking state from disk."""
    if TASK_DIAGNOSIS_STATE_FILE.exists():
        try:
            with open(TASK_DIAGNOSIS_STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _save_task_diagnosis_state(state: dict) -> None:
    """Save per-task diagnosis tracking state to disk."""
    state_module.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(TASK_DIAGNOSIS_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _new_diagnosis_entry(now: str, problem: str = "") -> dict:
    """Canonical shape for a per-task diagnosis state entry.

    Any code path that creates a new entry in _task_diagnosis.json must use
    this — partial init has crashed run_heartbeat in the past when a later
    function indexed a key that was never set.
    """
    return {
        "consecutive_detections": 0,
        "first_detected": now,
        "last_detected": now,
        "detected_problem": problem,
        "diagnosis_attempted": False,
        "last_diagnosis": None,
        "recovery_attempts": 0,
        "fixed_at": None,
        "recurring_fix_failures": 0,
        "last_fix_summary": "",
    }


def _record_task_detection(task_id: str, state: dict, problem: str) -> dict:
    """Record a heartbeat detection of a task anomaly (stale/disabled/stuck).

    If the task was recently "fixed" (within TASK_FIX_DURABILITY_HOURS),
    increments recurring_fix_failures to track that the fix didn't stick.
    """
    now = datetime.now().isoformat()
    entry = state.setdefault(task_id, _new_diagnosis_entry(now, problem))
    for k, v in _new_diagnosis_entry(now, problem).items():
        entry.setdefault(k, v)

    # Check if this is a recurrence after a "fix"
    fixed_at = entry.get("fixed_at")
    if fixed_at:
        try:
            fixed_dt = datetime.fromisoformat(fixed_at)
            now_dt = datetime.fromisoformat(now)
            hours_since_fix = (now_dt - fixed_dt).total_seconds() / 3600
            if hours_since_fix <= TASK_FIX_DURABILITY_HOURS:
                entry["recurring_fix_failures"] = entry.get("recurring_fix_failures", 0) + 1
                log(f"Recurring fix failure for {task_id} "
                    f"(broke again {hours_since_fix:.1f}h after fix, "
                    f"count={entry['recurring_fix_failures']})", "heartbeat")
        except (ValueError, TypeError):
            pass
        entry["fixed_at"] = None  # Clear so we don't double-count

    entry["consecutive_detections"] += 1
    entry["last_detected"] = now
    entry["detected_problem"] = problem
    entry["recovery_attempts"] += 1
    return state


def _should_attempt_task_diagnosis(task_id: str, state: dict) -> bool:
    """Check if we should invoke LLM diagnosis for a task.

    Requires TASK_DIAGNOSIS_MIN_DETECTIONS consecutive detections
    and respects cooldown between attempts. Uses exponential backoff
    when recurring_fix_failures indicates previous fixes didn't stick.
    Stops LLM entirely at TASK_DIAGNOSIS_CHRONIC_THRESHOLD.
    """
    entry = state.get(task_id, {})

    if entry.get("consecutive_detections", 0) < TASK_DIAGNOSIS_MIN_DETECTIONS:
        return False

    # Chronic failure — stop wasting LLM calls, needs human
    recurring = entry.get("recurring_fix_failures", 0)
    if recurring >= TASK_DIAGNOSIS_CHRONIC_THRESHOLD:
        log(f"Chronic failure for {task_id} ({recurring} recurring fixes failed), "
            f"skipping LLM diagnosis — needs human", "heartbeat")
        return False

    # Exponential backoff: 6h → 12h → 24h → 48h → 96h (cap at 168h/1wk)
    cooldown = min(TASK_DIAGNOSIS_COOLDOWN_HOURS * (2 ** recurring), 168)

    last_diagnosis = entry.get("last_diagnosis")
    if last_diagnosis:
        try:
            last_dt = datetime.fromisoformat(last_diagnosis)
            hours_since = (datetime.now() - last_dt).total_seconds() / 3600
            if hours_since < cooldown:
                return False
        except (ValueError, TypeError):
            pass

    return True


def _clear_task_diagnosis(task_id: str, state: dict) -> dict:
    """Soft-clear diagnosis state for a task after recovery.

    Preserves recurring_fix_failures and last_fix_summary so that if the
    task breaks again, we know it's a recurring failure. Sets fixed_at so
    _record_task_detection can detect re-breakage.
    """
    if task_id in state:
        entry = state[task_id]
        entry["fixed_at"] = datetime.now().isoformat()
        entry["last_fix_summary"] = entry.get("detected_problem", "")
        entry["consecutive_detections"] = 0
        entry["diagnosis_attempted"] = False
        entry["last_diagnosis"] = None
    return state


def _attempt_task_diagnosis(task_id: str, task_config: dict,
                            detected_problem: str, diagnosis_state: dict) -> bool:
    """Invoke LLM to diagnose and fix a task-level issue.

    Uses the task's on_diagnose config if present, otherwise falls back
    to the default TASK_DIAGNOSE_PROMPT.

    Returns True if the task appears healthy after diagnosis.
    """
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    entry = diagnosis_state.get(task_id, {})
    on_diagnose = task_config.get("on_diagnose", {})

    # Load task state for context
    task_state = _load_state(task_id)
    launchd_status = _get_launchd_status(task_id)

    # Build template variables
    template_vars = {
        "task_id": task_id,
        "task_name": task_config.get("name", task_id),
        "detected_problem": detected_problem,
        "command": f"{task_config.get('command', '')} {' '.join(str(a) for a in task_config.get('args', []))}".strip(),
        "working_dir": task_config.get("working_dir", str(PROJECT_ROOT)),
        "schedule": task_config.get("schedule", "unknown"),
        "last_run": task_state.last_run or "never",
        "consecutive_failures": str(task_state.consecutive_failures),
        "launchd_status": json.dumps(launchd_status, indent=2),
        "recovery_attempts": str(entry.get("recovery_attempts", 0)),
        "log_dir": str(log_dir),
        "log_file": str(log_dir / "pinglet.log"),
        "stderr_file": str(log_dir / f"{task_id}.err"),
        "stdout_file": str(log_dir / f"{task_id}.log"),
        "state_file": str(PROJECT_ROOT / "state" / f"{task_id}.json"),
        "learning_file": str(LEARNING_FILE),
        "project_root": str(PROJECT_ROOT),
        "python_path": str(PROJECT_ROOT / "venv" / "bin" / "python"),
        "recurring_fix_failures": str(entry.get("recurring_fix_failures", 0)),
        "last_fix_summary": entry.get("last_fix_summary", ""),
        "total_diagnose_invocations": str(
            _load_learning_state().get("tasks", {}).get(task_id, {}).get(
                "total_diagnose_invocations", 0)),
    }

    # Build prompt — use on_diagnose config or default
    if on_diagnose.get("command"):
        # Custom on_diagnose callback
        callback_cmd = on_diagnose["command"]
        callback_args = list(on_diagnose.get("args", []))
        callback_timeout = on_diagnose.get("timeout", 300)
        callback_max_turns = on_diagnose.get("max_turns", 10)
        callback_max_budget = on_diagnose.get("max_budget_usd", 3.00)
        callback_allowed_tools = on_diagnose.get("allowed_tools",
                                                  "Read,Bash,Edit,Grep,Glob")
        callback_working_dir = on_diagnose.get("working_dir",
                                                task_config.get("working_dir", str(PROJECT_ROOT)))

        # Substitute template variables in args
        resolved_args = []
        for arg in callback_args:
            resolved = str(arg)
            for key, value in template_vars.items():
                resolved = resolved.replace("{" + key + "}", value)
            resolved_args.append(resolved)

        cmd = [callback_cmd] + resolved_args

        # Add claude best-practice flags
        if callback_cmd in ("claude", "/opt/homebrew/bin/claude"):
            cmd.extend([
                "--allowedTools", callback_allowed_tools,
                "--output-format", "json",
                "--max-turns", str(callback_max_turns),
                "--max-budget-usd", str(callback_max_budget),
                "--no-session-persistence",
            ])
    else:
        # Default: use TASK_DIAGNOSE_PROMPT with claude
        prompt = TASK_DIAGNOSE_PROMPT
        for key, value in template_vars.items():
            prompt = prompt.replace("{" + key + "}", value)

        # Prepend recurring failure context if previous fixes didn't stick
        recurring = entry.get("recurring_fix_failures", 0)
        if recurring > 0:
            context = RECURRING_FAILURE_CONTEXT
            for key, value in template_vars.items():
                context = context.replace("{" + key + "}", value)
            prompt = context + "\n" + prompt

        callback_timeout = 300
        callback_working_dir = task_config.get("working_dir", str(PROJECT_ROOT))

        cmd = [
            "claude",
            "-p", prompt,
            "--allowedTools", "Read,Bash,Edit,Grep,Glob",
            "--output-format", "json",
            "--max-turns", "10",
            "--max-budget-usd", "3.00",
            "--no-session-persistence",
        ]

    log(f"Invoking task diagnosis for {task_id} ({detected_problem})", "heartbeat")

    # Record attempt
    entry["diagnosis_attempted"] = True
    entry["last_diagnosis"] = datetime.now().isoformat()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=callback_timeout,
            cwd=callback_working_dir,
        )

        # Log output
        log_file = log_dir / f"{task_id}-on_diagnose.log"
        with open(log_file, "w") as f:
            f.write(f"Timestamp: {datetime.now().isoformat()}\n")
            f.write(f"Problem: {detected_problem}\n")
            f.write(f"Exit code: {result.returncode}\n")
            f.write(f"Command: {' '.join(cmd[:6])}...\n")
            f.write(f"\n--- STDOUT ---\n{result.stdout}\n")
            f.write(f"\n--- STDERR ---\n{result.stderr}\n")

        if result.returncode == 0:
            # Verify task is actually healthy now
            new_status = _get_launchd_status(task_id)

            # Consider it fixed if: not disabled AND (just ran OR was stale and no longer failing)
            if not new_status.get("disabled"):
                log(f"Task diagnosis fixed {task_id}", "heartbeat")
                summary = _extract_llm_summary(result.stdout)
                # Store summary in diagnosis state for recurring failure tracking
                entry["last_fix_summary"] = summary[:200]
                _update_task_learning(task_id, "diagnosed_fixed", {
                    "problem": detected_problem,
                    "summary": summary,
                })
                return True

            log(f"Task diagnosis claimed success but {task_id} still has issues", "heartbeat")
            _update_task_learning(task_id, "diagnosed_failed", {
                "problem": detected_problem,
                "error_pattern": "LLM exit 0 but task still unhealthy",
            })
            return False
        else:
            log(f"Task diagnosis failed for {task_id} (exit {result.returncode})", "heartbeat")
            _update_task_learning(task_id, "diagnosed_failed", {
                "problem": detected_problem,
                "error_pattern": f"LLM exit {result.returncode}",
            })
            return False

    except subprocess.TimeoutExpired:
        log(f"Task diagnosis timed out for {task_id}", "heartbeat")
        return False
    except FileNotFoundError:
        log(f"claude CLI not found — cannot run task diagnosis", "heartbeat")
        return False
    except Exception as e:
        log(f"Task diagnosis error for {task_id}: {e}", "heartbeat")
        return False


def _extract_llm_summary(stdout: str) -> str:
    """Extract a summary from LLM JSON output, or first 200 chars of text."""
    try:
        parsed = json.loads(stdout)
        return str(parsed.get("result", ""))[:200]
    except (json.JSONDecodeError, TypeError):
        return (stdout or "")[:200]


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
    """Update learning state after an on_failure or on_diagnose callback.

    Args:
        task_id: Task identifier
        outcome: One of "invoked", "fixed", "failed", "diagnosed_fixed", "diagnosed_failed"
        details: Optional details (error patterns, diagnoses, etc.)
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
    elif outcome == "diagnosed_fixed":
        entry.setdefault("total_diagnose_invocations", 0)
        entry["total_diagnose_invocations"] += 1
        entry.setdefault("total_diagnose_fixes", 0)
        entry["total_diagnose_fixes"] += 1
        # Record diagnosis details
        if details:
            diagnosis_record = {
                "ts": datetime.now().isoformat(),
                "problem": details.get("problem", "unknown"),
                "summary": details.get("summary", "")[:200],
                "exit_code": 0,
            }
            entry.setdefault("diagnoses", [])
            entry["diagnoses"].append(diagnosis_record)
            # Keep only last 10 diagnoses
            entry["diagnoses"] = entry["diagnoses"][-10:]
    elif outcome == "diagnosed_failed":
        entry.setdefault("total_diagnose_invocations", 0)
        entry["total_diagnose_invocations"] += 1
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
        # Load task state
        state = _load_state(task_name)
        display_name = tasks_config.get(task_name, {}).get("name", task_name)

        ignored = _is_ignored(task_name)

        if not state.last_run:
            # Task has never run
            missed.append({
                "task_name": task_name,
                "display_name": display_name,
                "hours_overdue": float(threshold),  # Treat as fully overdue
                "threshold": threshold,
                "last_run": None,
                "never_run": True,
                "ignored": ignored,
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
                    "ignored": ignored,
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

    # Auto-clear stale ignore entries (older than IGNORE_EXPIRY_HOURS)
    cleared = _clear_stale_ignores()
    if cleared:
        log(f"Auto-cleared {len(cleared)} stale ignore entries: {', '.join(cleared)}", "heartbeat")

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

            # Tier 2: LLM diagnosis (after auto-recovery fails, attempt once)
            if not entry.get("llm_diagnosis_attempted", False):
                entry["llm_diagnosis_attempted"] = True

                # Use task-level diagnosis for regular tasks, monitoring-level for agents
                is_monitoring = agent_id in MONITORING_AGENTS
                tasks_config = config.get("tasks", {})

                if is_monitoring:
                    llm_success = _attempt_llm_self_diagnosis(agent_id, agent_info)
                elif agent_id in tasks_config:
                    llm_success = _attempt_task_diagnosis(
                        agent_id, tasks_config[agent_id],
                        f"disabled (exit {agent_info.get('exit_code', '?')})",
                        monitoring_down_state,
                    )
                else:
                    llm_success = _attempt_llm_self_diagnosis(agent_id, agent_info)

                if llm_success:
                    log(f"LLM diagnosis fixed {agent_id}", "heartbeat")
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
        return {"missed_count": 0, "tasks": [], "disabled_agents": [], "task_diagnoses": 0}

    # Detect and recover stale launchd calendar triggers
    stale_triggers_recovered = 0
    task_diagnoses_attempted = 0
    task_diagnoses_fixed = 0
    tasks_config = config.get("tasks", {})

    # Load task diagnosis tracking state
    task_diagnosis_state = _load_task_diagnosis_state()

    if missed_tasks:
        log(f"Found {len(missed_tasks)} missed task(s)", "heartbeat")

        # Tier 1: Stale trigger recovery (bounce launchd)
        # Apply cooldown: don't re-recover the same task within its expected_interval.
        # This prevents infinite re-bootstrap loops for long-interval tasks (e.g., weekly)
        # where runs=0 is normal between scheduled fire times.
        expected_intervals = config.get("healthcheck", {}).get("expected_intervals", {})
        stale = detect_stale_triggers(missed_tasks)
        for task in stale:
            task_name = task["task_name"]
            interval = expected_intervals.get(task_name, 24)
            if not _should_recover_stale_trigger(task_name, task_diagnosis_state, interval):
                log(f"Skipping stale recovery for {task_name} (cooldown active, interval={interval}h)", "heartbeat")
                continue
            result = recover_stale_trigger(task_name)
            if result == "recovered":
                stale_triggers_recovered += 1
                _record_stale_recovery(task_name, task_diagnosis_state)
                log(f"Recovered stale trigger for {task_name}", "heartbeat")

        # Tier 2: Task-level LLM diagnosis for persistently missed tasks
        tasks_to_remove = []
        for task in missed_tasks:
            task_name = task["task_name"]

            # Track detection
            task_diagnosis_state = _record_task_detection(
                task_name, task_diagnosis_state,
                f"stale ({task['hours_overdue']:.0f}h overdue)",
            )

            # Check if we should attempt LLM diagnosis
            if task_name in tasks_config and _should_attempt_task_diagnosis(task_name, task_diagnosis_state):
                task_diagnoses_attempted += 1
                llm_success = _attempt_task_diagnosis(
                    task_name, tasks_config[task_name],
                    f"stale ({task['hours_overdue']:.0f}h overdue, schedule: {tasks_config[task_name].get('schedule', '?')})",
                    task_diagnosis_state,
                )
                if llm_success:
                    task_diagnoses_fixed += 1
                    task_diagnosis_state = _clear_task_diagnosis(task_name, task_diagnosis_state)
                    tasks_to_remove.append(task_name)
                    log(f"Task diagnosis fixed {task_name}", "heartbeat")

        # Remove fixed tasks from missed list (no need to alert)
        missed_tasks = [t for t in missed_tasks if t["task_name"] not in tasks_to_remove]

    # Soft-clear diagnosis state for tasks that are no longer missed.
    # Preserve recurring failure history for tasks that had diagnosis attempts.
    missed_ids = {t["task_name"] for t in missed_tasks}
    for task_id in list(task_diagnosis_state.keys()):
        if task_id not in missed_ids:
            entry = task_diagnosis_state[task_id]
            if entry.get("diagnosis_attempted") or entry.get("recurring_fix_failures", 0) > 0:
                # Soft clear: preserve recurring failure history
                task_diagnosis_state = _clear_task_diagnosis(task_id, task_diagnosis_state)
            else:
                # Hard clear: no diagnosis history worth preserving
                del task_diagnosis_state[task_id]

    _save_task_diagnosis_state(task_diagnosis_state)

    # Wait for wake delay (allows system to stabilize after wake)
    if wake_delay is not None and wake_delay > 0 and missed_tasks:
        log(f"Waiting {wake_delay}s wake delay before notifying", "heartbeat")
        time.sleep(wake_delay)

    # Load per-task alert cooldown state
    alert_state = _load_heartbeat_alert_state()

    # Check for chronic LLM failures that need human escalation
    for task in missed_tasks:
        task_name = task["task_name"]
        diag_entry = task_diagnosis_state.get(task_name, {})
        recurring = diag_entry.get("recurring_fix_failures", 0)
        if recurring >= TASK_DIAGNOSIS_CHRONIC_THRESHOLD:
            chronic_msg = (
                f"*Pinglet: CHRONIC FAILURE*\n"
                f"`{task_name}` has been \"fixed\" {recurring} times but keeps breaking.\n"
                f"Last fix: {diag_entry.get('last_fix_summary', 'unknown')}\n"
                f"LLM diagnosis suspended — *human intervention required*."
            )
            # Send regardless of ignore status — chronic failures always alert
            if _should_alert_for_task(task_name, "critical", alert_state):
                _send_slack_message(chronic_msg)
                alert_state[task_name] = {
                    "last_alert": datetime.now().isoformat(),
                    "escalation": "critical",
                }

    # Tier 3: Send notifications for remaining missed tasks
    for task in missed_tasks:
        task_name = task["task_name"]
        display_name = task["display_name"]
        hours_overdue = task["hours_overdue"]
        threshold = task["threshold"]
        last_run = task.get("last_run")

        # Determine escalation level
        level = get_escalation_level(hours_overdue, threshold)
        task["escalation"] = level

        # Suppress notifications for ignored tasks (they still get Tier 1+2 recovery)
        if task.get("ignored", False):
            log(f"Suppressing notification for ignored task: {task_name}", "heartbeat")
            continue

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
        "stale_triggers_recovered": stale_triggers_recovered,
        "task_diagnoses_attempted": task_diagnoses_attempted,
        "task_diagnoses_fixed": task_diagnoses_fixed,
    }
