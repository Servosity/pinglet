from lib.heartbeat_core import *
from lib.heartbeat_monitoring import *

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
                            detected_problem: str, diagnosis_state: dict,
                            runner_config: dict = None) -> bool:
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
        "python_path": "uv run python",
        "recurring_fix_failures": str(entry.get("recurring_fix_failures", 0)),
        "last_fix_summary": entry.get("last_fix_summary", ""),
        "total_diagnose_invocations": str(
            _load_learning_state().get("tasks", {}).get(task_id, {}).get(
                "total_diagnose_invocations", 0)),
    }

    from lib.agent_runners import render_args, prompt_from_args, run_agent_prompt

    # Build prompt — use on_diagnose config or default
    if on_diagnose.get("command"):
        callback_config = dict(on_diagnose)
        callback_config["args"] = render_args(list(on_diagnose.get("args", [])), template_vars)
        prompt = prompt_from_args(callback_config["args"])
        callback_timeout = on_diagnose.get("timeout", 300)
        callback_max_turns = on_diagnose.get("max_turns", 10)
        callback_max_budget = on_diagnose.get("max_budget_usd", 3.00)
        callback_allowed_tools = on_diagnose.get("allowed_tools",
                                                  "Read,Bash,Edit,Grep,Glob")
        callback_working_dir = on_diagnose.get("working_dir",
                                                task_config.get("working_dir", str(PROJECT_ROOT)))
    else:
        # Default: use TASK_DIAGNOSE_PROMPT with configured runner order.
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
        callback_config = {}
        callback_allowed_tools = "Read,Bash,Edit,Grep,Glob"
        callback_max_turns = 10
        callback_max_budget = 3.00
        callback_working_dir = task_config.get("working_dir", str(PROJECT_ROOT))

    log(f"Invoking task diagnosis for {task_id} ({detected_problem})", "heartbeat")

    # Record attempt
    entry["diagnosis_attempted"] = True
    entry["last_diagnosis"] = datetime.now().isoformat()

    result = run_agent_prompt(
        state_key=f"on_diagnose:{task_id}",
        prompt=prompt,
        callback_config=callback_config,
        log_file=log_dir / f"{task_id}-on_diagnose.log",
        cwd=callback_working_dir,
        timeout=callback_timeout,
        allowed_tools=callback_allowed_tools,
        max_turns=callback_max_turns,
        max_budget_usd=callback_max_budget,
        runner_config=runner_config,
        run_cmd=subprocess.run,
    )

    if result.get("exit_code") == 0:
        # Verify task is actually healthy now
        new_status = _get_launchd_status(task_id)

        # Consider it fixed if: not disabled AND (just ran OR was stale and no longer failing)
        if not new_status.get("disabled"):
            log(f"Task diagnosis fixed {task_id}", "heartbeat")
            summary = str(result.get("output", ""))[:200]
            # Store summary in diagnosis state for recurring failure tracking
            entry["last_fix_summary"] = summary
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

    log(f"Task diagnosis failed for {task_id} (exit {result.get('exit_code')})", "heartbeat")
    _update_task_learning(task_id, "diagnosed_failed", {
        "problem": detected_problem,
        "error_pattern": f"LLM exit {result.get('exit_code')}",
    })
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



__all__ = [name for name in globals() if not name.startswith("__")]
