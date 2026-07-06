from lib.heartbeat_core import *


def _load_learning_state() -> dict:
    from lib.heartbeat_task_diagnosis import _load_learning_state as load_learning_state
    return load_learning_state()


def _new_diagnosis_entry(now: str, detected_problem: str) -> dict:
    from lib.heartbeat_task_diagnosis import _new_diagnosis_entry as new_diagnosis_entry
    return new_diagnosis_entry(now, detected_problem)


def _update_learning_state(agent_id: str, action: str, details: dict) -> None:
    from lib.heartbeat_task_diagnosis import _update_learning_state as update_learning_state
    return update_learning_state(agent_id, action, details)


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


def _attempt_llm_self_diagnosis(agent_id: str, status: dict, runner_config: dict = None) -> bool:
    """Invoke a CLI agent to diagnose and fix a disabled monitoring agent.

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

    log(f"Invoking LLM self-diagnosis for {agent_id}", "heartbeat")

    from lib.agent_runners import run_agent_prompt
    result = run_agent_prompt(
        state_key=f"self_diagnosis:{agent_id}",
        prompt=prompt,
        callback_config={},
        log_file=log_dir / f"{agent_id}-self-diagnosis.log",
        cwd=str(PROJECT_ROOT),
        timeout=120,
        allowed_tools="Read,Bash(launchctl *),Bash(cat *),Bash(ls *),Bash(grep *),Edit",
        max_turns=5,
        max_budget_usd=1.00,
        runner_config=runner_config,
        run_cmd=subprocess.run,
    )

    if result.get("exit_code") != 0:
        log(f"LLM self-diagnosis failed for {agent_id} (exit {result.get('exit_code')})", "heartbeat")
        return False

    new_status = _get_launchd_status(agent_id)
    if not new_status["disabled"]:
        log(f"LLM self-diagnosis fixed {agent_id}", "heartbeat")
        _update_learning_state(agent_id, "llm_recovery", {
            "summary": f"LLM fixed {agent_id} after auto-recovery failed",
        })
        return True

    log(f"LLM claimed success but {agent_id} still disabled", "heartbeat")
    return False


__all__ = [name for name in globals() if not name.startswith("__")]
