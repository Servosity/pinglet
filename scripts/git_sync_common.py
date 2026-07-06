#!/usr/bin/env python3
"""Shared git-sync wrapper helpers."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from lib.agent_runners import run_agent_prompt

GIT_SYNC_DIR = Path(os.environ.get("GIT_SYNC_DIR", "../git-sync")).expanduser()
FALLBACK_COMMIT_MESSAGE = "Update Obsidian vault"


def load_sync_module():
    sync_path = GIT_SYNC_DIR / "sync.py"
    sys.path.insert(0, str(GIT_SYNC_DIR))
    spec = importlib.util.spec_from_file_location("git_sync_sync", sync_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {sync_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def generate_commit_message(diff_summary: str, task_id: str) -> str:
    prompt = f"""Analyze this Obsidian vault git diff and write a useful git commit message.

{diff_summary}

Return ONLY the commit message text. Keep it concise and specific."""

    result = run_agent_prompt(
        state_key=f"commit_message:{task_id}",
        prompt=prompt,
        callback_config={},
        log_file=PROJECT_ROOT / "logs" / f"{task_id}-commit-message.log",
        cwd=os.getcwd(),
        timeout=60,
        allowed_tools="",
        max_turns=1,
        max_budget_usd=0.50,
        runner_config=_load_runner_config(),
    )
    message = (result.get("output") or "").strip()
    if result.get("exit_code") == 0 and message:
        return message
    if message:
        print(f"Commit message agent failed: {message}", file=sys.stderr)
    return FALLBACK_COMMIT_MESSAGE


def run_sync(task_id: str, default_config: str) -> int:
    sync_module = load_sync_module()

    class VaultSyncWithAgent(sync_module.VaultSync):
        def verify_setup(self):
            return verify_setup_without_claude(self)

        def generate_commit_message(self, diff_summary: str):
            return generate_commit_message(diff_summary, task_id)

    config_path = sys.argv[1] if len(sys.argv) > 1 else default_config
    syncer = VaultSyncWithAgent(config_path)
    success, _message = syncer.sync()
    return 0 if success else 1


def _load_runner_config() -> dict:
    with open(PROJECT_ROOT / "config.yaml", "r") as f:
        return (yaml.safe_load(f) or {}).get("agent_runners", {})


def verify_setup_without_claude(syncer) -> tuple[bool, str]:
    if not syncer.vault_path.exists():
        return False, f"Vault path does not exist: {syncer.vault_path}"

    git_file = syncer.vault_path / ".git"
    if not git_file.exists():
        return False, f"No .git file found in vault: {git_file}"

    result = syncer._run_git_command(["status"])
    if result.returncode != 0:
        return False, f"Git command failed: {result.stderr}"

    result = syncer._run_git_command(["remote", "get-url", "origin"])
    if result.returncode != 0:
        return False, "No git remote 'origin' configured"

    return True, "Setup verified successfully"
