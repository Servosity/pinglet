"""Facade for Pinglet task management modules."""
from pathlib import Path
from unittest.mock import Mock

from lib import task_manager_config as _config
from lib import task_manager_launchd as _launchd
from lib import task_manager_logs as _logs
from lib import task_manager_schedule as _schedule
from lib import task_manager_status as _status
from lib.state import load_state

MONITORING_AGENTS = ["healthcheck", "heartbeat"]
PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
LAUNCHAGENTS_DIR = PROJECT_ROOT / "launchagents"
USER_LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LOGS_DIR = PROJECT_ROOT / "logs"
STATE_DIR = PROJECT_ROOT / "state"
_PATCHABLE = ("disable_task", "get_launchd_status", "is_task_enabled", "_get_uid", "load_state")
_ORIGINALS = {
    (module, name): getattr(module, name)
    for module in (_config, _launchd, _logs, _status)
    for name in _PATCHABLE
    if hasattr(module, name)
}


def _sync():
    for module in (_config, _launchd, _logs, _schedule, _status):
        for name in (
            "MONITORING_AGENTS",
            "PROJECT_ROOT",
            "CONFIG_PATH",
            "LAUNCHAGENTS_DIR",
            "USER_LAUNCHAGENTS_DIR",
            "LOGS_DIR",
            "STATE_DIR",
        ):
            if hasattr(module, name):
                setattr(module, name, globals()[name])
    for module in (_config, _launchd, _logs, _status):
        for name in _PATCHABLE:
            value = globals().get(name)
            if isinstance(value, Mock) and hasattr(module, name):
                setattr(module, name, value)
            elif (module, name) in _ORIGINALS:
                setattr(module, name, _ORIGINALS[(module, name)])


def parse_schedule(schedule_spec: str) -> dict:
    return _schedule.parse_schedule(schedule_spec)


def _parse_time(time_str: str) -> dict:
    return _schedule._parse_time(time_str)


def schedule_to_human(schedule_dict: dict) -> str:
    return _schedule.schedule_to_human(schedule_dict)


def estimate_expected_interval(schedule_dict: dict):
    return _schedule.estimate_expected_interval(schedule_dict)


def validate_task_id(task_id: str):
    return _schedule.validate_task_id(task_id)


def validate_task_config(task_config: dict, task_id: str, existing_tasks: dict):
    return _schedule.validate_task_config(task_config, task_id, existing_tasks)


def generate_plist(task_id: str, schedule_dict: dict) -> str:
    _sync()
    return _schedule.generate_plist(task_id, schedule_dict)


def _render_schedule_xml(schedule_dict: dict) -> str:
    return _schedule._render_schedule_xml(schedule_dict)


def _render_calendar_interval_xml(interval: dict, indent: int = 2) -> str:
    return _schedule._render_calendar_interval_xml(interval, indent)


def load_config() -> dict:
    _sync()
    return _config.load_config()


def save_config(config: dict) -> None:
    _sync()
    return _config.save_config(config)


def add_task(task_id: str, task_config: dict) -> dict:
    _sync()
    return _config.add_task(task_id, task_config)


def edit_task(task_id: str, updates: dict) -> dict:
    _sync()
    return _config.edit_task(task_id, updates)


def remove_task(task_id: str) -> dict:
    _sync()
    return _config.remove_task(task_id)


def _deep_merge(base: dict, updates: dict) -> None:
    return _config._deep_merge(base, updates)


def show_task(task_id: str) -> dict:
    _sync()
    return _logs.show_task(task_id)


def list_tasks_json() -> list:
    _sync()
    return _logs.list_tasks_json()


def get_task_logs(task_id: str, lines: int = 50) -> dict:
    _sync()
    return _logs.get_task_logs(task_id, lines)


def _tail_file(path: Path, lines: int) -> str:
    return _logs._tail_file(path, lines)


def _grep_file(path: Path, pattern: str, max_lines: int) -> str:
    return _logs._grep_file(path, pattern, max_lines)


def enable_task(task_id: str, schedule_spec: str = None) -> dict:
    _sync()
    return _launchd.enable_task(task_id, schedule_spec)


def disable_task(task_id: str) -> dict:
    _sync()
    return _launchd.disable_task(task_id)


def is_task_enabled(task_id: str) -> bool:
    _sync()
    return _launchd.is_task_enabled(task_id)


def get_launchd_status(task_id: str) -> dict:
    _sync()
    return _launchd.get_launchd_status(task_id)


def get_launchd_run_count(task_id: str):
    _sync()
    return _launchd.get_launchd_run_count(task_id)


def get_all_launchd_statuses() -> dict:
    _sync()
    return _launchd.get_all_launchd_statuses()


def set_schedule(task_id: str, schedule_spec: str) -> dict:
    _sync()
    return _launchd.set_schedule(task_id, schedule_spec)


def _get_uid() -> str:
    return _launchd._get_uid()


def _strip_macl_from_logs(task_id: str) -> None:
    _sync()
    return _launchd._strip_macl_from_logs(task_id)


def _write_and_install_plist(task_id: str, schedule_dict: dict) -> Path:
    _sync()
    return _launchd._write_and_install_plist(task_id, schedule_dict)


def get_system_status() -> dict:
    _sync()
    return _status.get_system_status()


def _read_schedule_from_plist(plist_path: Path):
    return _logs._read_schedule_from_plist(plist_path)
