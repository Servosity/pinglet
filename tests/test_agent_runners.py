import json

from lib import agent_runners


class Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def call_runner(tmp_path, callback_config, run_cmd, state_key="cb:task", runner_config=None, initial_state=None):
    agent_runners.STATE_FILE = tmp_path / "_agent_runners.json"
    if initial_state is not None:
        agent_runners.STATE_FILE.write_text(json.dumps(initial_state))
    return agent_runners.run_agent_prompt(
        state_key=state_key,
        prompt="Fix it",
        callback_config=callback_config,
        log_file=tmp_path / "runner.log",
        cwd=str(tmp_path),
        timeout=30,
        allowed_tools="Read,Bash,Edit",
        max_turns=5,
        max_budget_usd=1.0,
        runner_config=runner_config,
        run_cmd=run_cmd,
    )


def test_default_provider_is_codex(tmp_path):
    calls = []
    stdout = json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "fixed"},
    })

    def run(cmd, **kwargs):
        calls.append(cmd)
        return Proc(stdout=stdout)

    result = call_runner(tmp_path, {}, run)

    assert result["provider"] == "codex"
    assert calls[0][0] == "codex"


def test_claude_provider_uses_print_json(tmp_path):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return Proc(stdout='{"result": "fixed"}')

    result = call_runner(tmp_path, {"command": "claude"}, run)

    assert result["exit_code"] == 0
    assert result["provider"] == "claude"
    assert calls[0][:3] == ["claude", "-p", "Fix it"]
    assert "--output-format" in calls[0]


def test_codex_provider_uses_exec_json(tmp_path):
    calls = []
    stdout = json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "fixed"},
    })

    def run(cmd, **kwargs):
        calls.append(cmd)
        return Proc(stdout=stdout)

    result = call_runner(tmp_path, {"providers": ["codex"]}, run)

    assert result["exit_code"] == 0
    assert result["provider"] == "codex"
    assert calls[0] == ["codex", "exec", "--json", "--sandbox", "workspace-write", "Fix it"]


def test_named_slots_pass_models_and_claude_fallback_model(tmp_path):
    calls = []
    runner_config = {
        "primary": {"provider": "codex", "model": "gpt-5.5"},
        "secondary": {
            "provider": "claude",
            "model": "sonnet-4.6",
            "fallback_model": "sonnet-5",
        },
    }

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "codex":
            return Proc(returncode=1, stderr="subscription unavailable")
        return Proc(stdout='{"result": "claude fixed it"}')

    result = call_runner(
        tmp_path, {}, run, runner_config=runner_config,
        initial_state={"cb:task": {"codex": 2}},
    )

    assert result["provider"] == "claude"
    assert calls[0] == [
        "codex", "exec", "--json", "--sandbox", "workspace-write",
        "--model", "gpt-5.5", "Fix it",
    ]
    assert "--model" in calls[1]
    assert calls[1][calls[1].index("--model") + 1] == "sonnet-4.6"
    assert "--fallback-model" in calls[1]
    assert calls[1][calls[1].index("--fallback-model") + 1] == "sonnet-5"


def test_named_slots_skip_disabled_tertiary(tmp_path):
    calls = []
    stdout = json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "fixed"},
    })
    runner_config = {
        "primary": {"provider": "codex"},
        "tertiary": {"enabled": False, "provider": "openrouter", "model": "any/model"},
    }

    def run(cmd, **kwargs):
        calls.append(cmd)
        return Proc(stdout=stdout)

    result = call_runner(tmp_path, {}, run, runner_config=runner_config)

    assert result["provider"] == "codex"
    assert [cmd[0] for cmd in calls] == ["codex"]


def test_openrouter_sends_any_model_and_returns_diagnosis(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "diagnose this"}}],
            }).encode()

    def urlopen(request, timeout):
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode())
        captured["auth"] = request.get_header("Authorization")
        return Response()

    monkeypatch.setattr(agent_runners.urllib.request, "urlopen", urlopen)

    result = call_runner(
        tmp_path,
        {"providers": [{"name": "openrouter", "model": "vendor/anything"}]},
        lambda cmd, **kwargs: Proc(),
    )

    assert result["exit_code"] == 1
    assert result["output"] == "diagnose this"
    assert captured["payload"]["model"] == "vendor/anything"
    assert captured["payload"]["messages"][0]["content"] == "Fix it"
    assert captured["auth"] == "Bearer secret"


def test_openrouter_missing_key_counts_as_provider_failure(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    result = call_runner(
        tmp_path,
        {"providers": [{"name": "openrouter", "model": "vendor/anything"}]},
        lambda cmd, **kwargs: Proc(),
    )

    assert result["exit_code"] == 127
    assert "Missing OPENROUTER_API_KEY" in result["output"]
    state = json.loads(agent_runners.STATE_FILE.read_text())
    assert state["cb:task"]["openrouter"] == 1


def test_fallback_after_third_provider_failure(tmp_path):
    agent_runners.STATE_FILE = tmp_path / "_agent_runners.json"
    agent_runners.STATE_FILE.write_text(json.dumps({"cb:task": {"claude": 2}}))
    calls = []
    codex_stdout = json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "codex fixed it"},
    })

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "claude":
            return Proc(returncode=1, stderr="auth failed")
        return Proc(stdout=codex_stdout)

    result = call_runner(tmp_path, {"providers": ["claude", "codex"]}, run)

    assert result["exit_code"] == 0
    assert result["providers_attempted"] == ["claude", "codex"]
    assert [cmd[0] for cmd in calls] == ["claude", "codex"]
    state = json.loads(agent_runners.STATE_FILE.read_text())
    assert state["cb:task"]["claude"] == 3
    assert state["cb:task"]["codex"] == 0


def test_single_provider_config_waits_until_threshold(tmp_path):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return Proc(returncode=1, stderr="still broken")

    result = call_runner(tmp_path, {"providers": ["claude"]}, run)

    assert result["exit_code"] == 1
    assert result["providers_attempted"] == ["claude"]
    assert len(calls) == 1


def test_parse_failure_counts_as_provider_failure(tmp_path):
    def run(cmd, **kwargs):
        return Proc(stdout="plain text")

    result = call_runner(tmp_path, {"providers": ["claude"]}, run)

    assert result["exit_code"] == -2
    state = json.loads(agent_runners.STATE_FILE.read_text())
    assert state["cb:task"]["claude"] == 1
