import importlib.util
import types
from pathlib import Path


def load_common():
    path = Path(__file__).resolve().parents[1] / "scripts" / "git_sync_common.py"
    spec = importlib.util.spec_from_file_location("git_sync_common_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generate_commit_message_uses_pinglet_runner(monkeypatch):
    common = load_common()
    calls = []

    monkeypatch.setattr(
        common,
        "_load_runner_config",
        lambda: {"primary": {"provider": "codex", "model": "gpt-5.5"}},
    )

    def fake_run_agent_prompt(**kwargs):
        calls.append(kwargs)
        return {"exit_code": 0, "output": "Update vault notes"}

    monkeypatch.setattr(common, "run_agent_prompt", fake_run_agent_prompt)

    assert common.generate_commit_message("Status:\n M note.md", "git-sync") == "Update vault notes"
    assert calls[0]["state_key"] == "commit_message:git-sync"
    assert calls[0]["runner_config"]["primary"]["model"] == "gpt-5.5"
    assert calls[0]["callback_config"] == {}


def test_generate_commit_message_falls_back(monkeypatch):
    common = load_common()

    monkeypatch.setattr(common, "_load_runner_config", lambda: {})
    monkeypatch.setattr(
        common,
        "run_agent_prompt",
        lambda **_kwargs: {"exit_code": -1, "output": "Timed out"},
    )

    assert common.generate_commit_message("diff", "hermes-sync") == "Update Obsidian vault"


def test_run_sync_keeps_git_checks_but_skips_claude_preflight(monkeypatch, tmp_path):
    common = load_common()
    (tmp_path / ".git").write_text("gitdir: real-git-dir\n")
    calls = []

    class Proc:
        returncode = 0
        stderr = ""

    class VaultSync:
        def __init__(self, config_path):
            self.config_path = config_path
            self.vault_path = tmp_path

        def _run_git_command(self, args):
            calls.append(args)
            return Proc()

        def sync(self):
            ok, _message = self.verify_setup()
            return ok and self.generate_commit_message("diff") == "Agent commit", "done"

    monkeypatch.setattr(common, "load_sync_module", lambda: types.SimpleNamespace(VaultSync=VaultSync))
    monkeypatch.setattr(common, "generate_commit_message", lambda _diff, _task_id: "Agent commit")
    monkeypatch.setattr(common.sys, "argv", ["git_sync.py"])

    assert common.run_sync("git-sync", "config.yaml") == 0
    assert calls == [["status"], ["remote", "get-url", "origin"]]
