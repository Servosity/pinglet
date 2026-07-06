import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

# Add lib to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lib.alerts import (
    send_critical,
    send_recovery,
    send_health_summary,
    send_macos_notification,
    send_manual_complete_notification,
    test_alerts,
)
from lib.logging import log, log_run_start, log_run_end, get_log_file_path
from lib.reliability import ReliabilityConfig, ReliabilityManager
from lib.state import (
    load_state,
    update_state_success,
    update_state_failure,
    load_all_states,
)
from lib.heartbeat import run_heartbeat as heartbeat_run, should_run_task
from lib.ignored import ignore_task as mark_ignored, clear_ignored, is_ignored
def _run_on_failure_callback(task_name: str, task_config: dict, exit_code: int,
                              error: str, consecutive_failures: int,
                              runner_config: dict = None) -> dict:
    """Run an on_failure callback if configured for this task.

    Substitutes template variables into the callback args and runs the
    configured CLI runner.

    Returns:
        {"invoked": bool, "exit_code": int|None, "output": str, "log_file": str|None}
    """
    on_failure = task_config.get("on_failure")
    if not on_failure:
        return {"invoked": False, "exit_code": None, "output": "", "log_file": None}

    callback_args = on_failure.get("args", [])
    callback_timeout = on_failure.get("timeout", 180)
    callback_max_turns = on_failure.get("max_turns", 5)
    callback_max_budget = on_failure.get("max_budget_usd", 2.00)
    callback_allowed_tools = on_failure.get("allowed_tools", "Read,Bash,Edit")
    callback_working_dir = on_failure.get("working_dir", str(PROJECT_ROOT))

    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    learning_file = PROJECT_ROOT / "state" / "_learning.json"

    # Template variables
    template_vars = {
        "task_id": task_name,
        "task_name": task_config.get("name", task_name),
        "exit_code": str(exit_code),
        "error": (error or "")[:500],
        "log_file": str(PROJECT_ROOT / "logs" / "pinglet.log"),
        "stderr_file": str(logs_dir / f"{task_name}.err"),
        "stdout_file": str(logs_dir / f"{task_name}.log"),
        "working_dir": task_config.get("working_dir", str(PROJECT_ROOT)),
        "consecutive_failures": str(consecutive_failures),
        "state_file": str(PROJECT_ROOT / "state" / f"{task_name}.json"),
        "project_root": str(PROJECT_ROOT),
        "learning_file": str(learning_file),
    }

    from lib.agent_runners import render_args, prompt_from_args, run_agent_prompt
    callback_config = dict(on_failure)
    callback_config["args"] = render_args(callback_args, template_vars)
    prompt = prompt_from_args(callback_config["args"])
    log_file_path = logs_dir / f"{task_name}-on_failure.log"

    log(f"Running on_failure callback for {task_name}", task_name)
    result = run_agent_prompt(
        state_key=f"on_failure:{task_name}",
        prompt=prompt,
        callback_config=callback_config,
        log_file=log_file_path,
        cwd=callback_working_dir,
        timeout=callback_timeout,
        allowed_tools=callback_allowed_tools,
        max_turns=callback_max_turns,
        max_budget_usd=callback_max_budget,
        runner_config=runner_config,
        run_cmd=subprocess.run,
    )

    from lib.heartbeat import _update_task_learning
    if result.get("invoked"):
        if result.get("exit_code") == 0:
            _update_task_learning(task_name, "fixed")
        else:
            _update_task_learning(task_name, "failed", {"error_pattern": (error or "")[:200]})
    log(f"on_failure callback completed (exit {result.get('exit_code')})", task_name)
    return result
