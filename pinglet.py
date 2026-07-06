#!/usr/bin/env python3
"""Pinglet CLI entrypoint and compatibility facade."""
from pathlib import Path
from unittest.mock import Mock

from lib import pinglet_callbacks as _callbacks
from lib import pinglet_cli as _cli
from lib import pinglet_launch as _launch
from lib import pinglet_runtime as _runtime
from lib.alerts import (
    send_macos_notification,
    send_manual_complete_notification,
    test_alerts,
)
from lib.ignored import clear_ignored, ignore_task as mark_ignored
from lib.state import load_state, update_state_failure, update_state_success

PROJECT_ROOT = Path(__file__).parent
LAUNCHAGENTS_DIR = PROJECT_ROOT / "launchagents"
USER_LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
heartbeat_run = _runtime.heartbeat_run
should_run_task = _runtime.should_run_task
log = _runtime.log
log_run_start = _runtime.log_run_start
log_run_end = _runtime.log_run_end

HELP_TEXT = _cli.HELP_TEXT
_PATCHABLE = (
    "_check_monitoring_agents",
    "_maybe_send_monitoring_alert",
    "_run_on_failure_callback",
    "clear_ignored",
    "heartbeat_run",
    "load_config",
    "load_state",
    "log",
    "log_run_end",
    "log_run_start",
    "mark_ignored",
    "run_task",
    "send_macos_notification",
    "send_manual_complete_notification",
    "should_run_task",
    "test_alerts",
    "update_state_failure",
    "update_state_success",
)
_ORIGINALS = {
    (module, name): getattr(module, name)
    for module in (_runtime, _launch, _callbacks, _cli)
    for name in _PATCHABLE
    if hasattr(module, name)
}


def _sync():
    for module in (_runtime, _launch, _callbacks, _cli):
        if hasattr(module, "PROJECT_ROOT"):
            module.PROJECT_ROOT = PROJECT_ROOT
        if hasattr(module, "LAUNCHAGENTS_DIR"):
            module.LAUNCHAGENTS_DIR = LAUNCHAGENTS_DIR
        if hasattr(module, "USER_LAUNCHAGENTS_DIR"):
            module.USER_LAUNCHAGENTS_DIR = USER_LAUNCHAGENTS_DIR
        for name in _PATCHABLE:
            value = globals().get(name)
            if isinstance(value, Mock) and hasattr(module, name):
                setattr(module, name, value)
            elif (module, name) in _ORIGINALS:
                setattr(module, name, _ORIGINALS[(module, name)])


def load_config():
    _sync()
    return _runtime.load_config()


def run_task(task_name: str) -> int:
    _sync()
    return _runtime.run_task(task_name)


def run_healthcheck() -> int:
    _sync()
    return _runtime.run_healthcheck()


def run_now(task_name: str, config: dict = None) -> int:
    _sync()
    return _runtime.run_now(task_name, config)


def ignore_task(task_name: str, config: dict = None) -> int:
    _sync()
    return _runtime.ignore_task(task_name, config)


def run_heartbeat_command(config: dict = None) -> int:
    _sync()
    return _runtime.run_heartbeat_command(config)


def install_heartbeat() -> int:
    _sync()
    return _launch.install_heartbeat()


def uninstall_heartbeat() -> int:
    _sync()
    return _launch.uninstall_heartbeat()


def list_tasks(json_mode: bool = False) -> None:
    _sync()
    return _launch.list_tasks(json_mode)


def _check_monitoring_agents() -> list:
    _sync()
    return _launch._check_monitoring_agents()


def _maybe_send_monitoring_alert(dead_agents: list) -> None:
    _sync()
    return _launch._maybe_send_monitoring_alert(dead_agents)


def _run_on_failure_callback(*args, **kwargs) -> dict:
    _sync()
    return _callbacks._run_on_failure_callback(*args, **kwargs)


def __getattr__(name):
    if hasattr(_cli, name):
        return getattr(_cli, name)
    raise AttributeError(name)


main = _cli.main
run_status = _cli.run_status
handle_schedule = _cli.handle_schedule
handle_task_add = _cli.handle_task_add
handle_task_disable = _cli.handle_task_disable
handle_task_edit = _cli.handle_task_edit
handle_task_enable = _cli.handle_task_enable
handle_task_logs = _cli.handle_task_logs
handle_task_remove = _cli.handle_task_remove
handle_task_show = _cli.handle_task_show
_build_task_config_from_args = _cli._build_task_config_from_args


if __name__ == "__main__":
    main()
