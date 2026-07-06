from lib.heartbeat_core import *
from lib.heartbeat_monitoring import *
from lib.heartbeat_task_diagnosis import *

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
    runner_config = config.get("agent_runners", {})

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
                    llm_success = _attempt_llm_self_diagnosis(agent_id, agent_info, runner_config)
                elif agent_id in tasks_config:
                    llm_success = _attempt_task_diagnosis(
                        agent_id, tasks_config[agent_id],
                        f"disabled (exit {agent_info.get('exit_code', '?')})",
                        monitoring_down_state,
                        runner_config,
                    )
                else:
                    llm_success = _attempt_llm_self_diagnosis(agent_id, agent_info, runner_config)

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
                    runner_config,
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

__all__ = [name for name in globals() if not name.startswith("__")]
