"""Codex/Claude/OpenRouter runner fallback for Pinglet callbacks."""
import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from lib import state as state_module

STATE_FILE = state_module.STATE_DIR / "_agent_runners.json"
FAILOVER_THRESHOLD = 3
DEFAULT_PROVIDERS = ["codex", "claude"]
SUPPORTED_PROVIDERS = {"codex", "claude", "openrouter"}
RUNNER_SLOTS = ("primary", "secondary", "tertiary")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def render_args(args: list, template_vars: dict) -> list:
    rendered = []
    for arg in args:
        value = str(arg)
        for key, replacement in template_vars.items():
            value = value.replace("{" + key + "}", str(replacement))
        rendered.append(value)
    return rendered


def prompt_from_args(args: list) -> str:
    for flag in ("-p", "--print"):
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                return str(args[i + 1])
    return " ".join(str(a) for a in args)


def run_agent_prompt(
    *,
    state_key: str,
    prompt: str,
    callback_config: dict,
    log_file: Path,
    cwd: str,
    timeout: int,
    allowed_tools: str,
    max_turns: int,
    max_budget_usd: float,
    runner_config: Optional[dict] = None,
    run_cmd: Callable = subprocess.run,
) -> dict:
    """Run a callback through configured providers, falling back after failures."""
    runner_config = runner_config or {}
    callback_config = callback_config or {}
    command = callback_config.get("command", "")
    provider_hint = _provider_name(command)

    if command and not provider_hint and not callback_config.get("providers"):
        return _run_legacy_command(command, callback_config, log_file, cwd, timeout, run_cmd)

    providers = _provider_order(callback_config, runner_config, provider_hint)
    if not providers:
        return {"invoked": False, "exit_code": None, "output": "", "log_file": None}

    log_file.parent.mkdir(parents=True, exist_ok=True)
    state = _load_state()
    counts = state.setdefault(state_key, {})
    eligible = [p for p in providers if counts.get(p["name"], 0) < FAILOVER_THRESHOLD] or providers
    attempts = []

    for provider in eligible:
        name = provider["name"]
        exe = provider["command"]
        cmd = _build_command(provider, prompt, allowed_tools, max_turns, max_budget_usd)
        attempts.append(name)

        try:
            if name == "openrouter":
                result = _run_openrouter(provider, prompt, timeout)
            else:
                proc = run_cmd(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
                result = _parse_result(name, proc.returncode, proc.stdout or "", proc.stderr or "")
        except subprocess.TimeoutExpired:
            result = _failure_result(-1, "", f"Timed out after {timeout}s")
        except FileNotFoundError:
            result = _failure_result(127, "", f"Command not found: {exe}")
        except Exception as exc:
            result = _failure_result(1, "", str(exc))

        _append_log(log_file, name, cmd, result)
        if result["ok"]:
            counts[name] = 0
            _save_state(state)
            return {
                "invoked": True,
                "exit_code": 0,
                "output": result["output"],
                "log_file": str(log_file),
                "provider": name,
                "providers_attempted": attempts,
            }

        provider_failed = result.get("provider_failed", True)
        if provider_failed:
            counts[name] = counts.get(name, 0) + 1
        _save_state(state)
        if not provider_failed or counts.get(name, 0) < FAILOVER_THRESHOLD:
            return {
                "invoked": True,
                "exit_code": result["exit_code"],
                "output": result["output"],
                "log_file": str(log_file),
                "provider": name,
                "providers_attempted": attempts,
            }

    return {
        "invoked": True,
        "exit_code": result["exit_code"],
        "output": result["output"],
        "log_file": str(log_file),
        "provider": attempts[-1] if attempts else None,
        "providers_attempted": attempts,
    }


def _run_legacy_command(command, callback_config, log_file, cwd, timeout, run_cmd):
    cmd = [command] + list(callback_config.get("args", []))
    try:
        proc = run_cmd(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        result = {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "output": (proc.stdout or proc.stderr or "")[:500],
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }
    except subprocess.TimeoutExpired:
        result = _failure_result(-1, "", f"Timed out after {timeout}s")
    except FileNotFoundError:
        return {"invoked": False, "exit_code": None, "output": f"Command not found: {command}", "log_file": None}
    except Exception as exc:
        return {"invoked": False, "exit_code": None, "output": str(exc), "log_file": None}

    log_file.parent.mkdir(parents=True, exist_ok=True)
    _append_log(log_file, "legacy", cmd, result)
    return {"invoked": True, "exit_code": result["exit_code"], "output": result["output"], "log_file": str(log_file)}


def _provider_order(callback_config, runner_config, hint):
    raw = callback_config.get("providers")
    slots_configured = any(slot in runner_config for slot in RUNNER_SLOTS)
    if not raw:
        raw = _slot_providers(runner_config)
    if raw is None and not slots_configured:
        raw = runner_config.get("providers")
    if raw is None:
        raw = [hint] + [p for p in DEFAULT_PROVIDERS if p != hint] if hint else DEFAULT_PROVIDERS

    providers = []
    command = callback_config.get("command", "")
    for item in raw:
        provider = _provider_from_config(item, command)
        if provider:
            providers.append(provider)
    return providers


def _slot_providers(runner_config):
    if not any(slot in runner_config for slot in RUNNER_SLOTS):
        return None
    raw = []
    for slot in RUNNER_SLOTS:
        item = runner_config.get(slot)
        if not item:
            continue
        if isinstance(item, str):
            item = {"provider": item}
        if isinstance(item, dict) and item.get("enabled", True) is False:
            continue
        raw.append(item)
    return raw


def _provider_from_config(item, command):
    if isinstance(item, str):
        name = item
        provider = {}
    elif isinstance(item, dict):
        name = item.get("name") or item.get("provider")
        provider = dict(item)
    else:
        return None

    if name not in SUPPORTED_PROVIDERS:
        return None

    exe = provider.get("command") or (command if _provider_name(command) == name else name)
    provider.update({"name": name, "command": exe})
    return provider


def _provider_name(command: str) -> Optional[str]:
    name = Path(command).name if command else ""
    if name in SUPPORTED_PROVIDERS:
        return name
    return None


def _build_command(provider, prompt, allowed_tools, max_turns, max_budget_usd):
    name = provider["name"]
    exe = provider["command"]
    model = provider.get("model")
    if name == "openrouter":
        return ["openrouter", str(model or "")]
    if name == "claude":
        cmd = [
            exe, "-p", prompt,
            "--allowedTools", allowed_tools,
            "--output-format", "json",
            "--max-turns", str(max_turns),
            "--max-budget-usd", str(max_budget_usd),
            "--no-session-persistence",
        ]
        if model:
            cmd.extend(["--model", str(model)])
        fallback_model = _csv(provider.get("fallback_model") or provider.get("fallback_models"))
        if fallback_model:
            cmd.extend(["--fallback-model", fallback_model])
        return cmd

    cmd = [exe, "exec", "--json", "--sandbox", "workspace-write"]
    if model:
        cmd.extend(["--model", str(model)])
    cmd.append(prompt)
    return cmd


def _run_openrouter(provider, prompt, timeout):
    api_key_env = provider.get("api_key_env", "OPENROUTER_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        return _failure_result(127, "", f"Missing {api_key_env}")

    payload = _openrouter_payload(provider, prompt)
    if isinstance(payload, str):
        return _failure_result(2, "", payload)

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            stdout = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return _failure_result(exc.code, body, f"OpenRouter HTTP {exc.code}")
    except urllib.error.URLError as exc:
        return _failure_result(1, "", f"OpenRouter request failed: {exc.reason}")

    try:
        output = _parse_openrouter(stdout)
        return {
            "ok": False,
            "exit_code": 1,
            "output": output,
            "stdout": stdout,
            "stderr": "OpenRouter diagnosis only; human intervention required.",
            "provider_failed": False,
        }
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        return _failure_result(-2, stdout, f"Parse failure: {exc}")


def _openrouter_payload(provider, prompt):
    payload = {"messages": [{"role": "user", "content": prompt}]}
    model = provider.get("model")
    models = _list(provider.get("models") or provider.get("fallback_models"))
    if models:
        payload["models"] = ([str(model)] if model else []) + models
    elif model:
        payload["model"] = str(model)
    else:
        return "OpenRouter provider requires model"

    options = provider.get("options")
    if isinstance(options, dict):
        payload.update(options)
    return payload


def _csv(value):
    values = _list(value)
    return ",".join(values) if values else ""


def _list(value):
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    return [str(value)]


def _parse_result(provider, returncode, stdout, stderr):
    if returncode != 0:
        return _failure_result(returncode, stdout, stderr or "nonzero exit")
    try:
        if provider == "claude":
            output = _parse_claude(stdout)
        elif provider == "openrouter":
            output = _parse_openrouter(stdout)
        else:
            output = _parse_codex(stdout)
        return {"ok": True, "exit_code": 0, "output": output, "stdout": stdout, "stderr": stderr}
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        return _failure_result(-2, stdout, f"Parse failure: {exc}")


def _parse_claude(stdout):
    parsed = json.loads(stdout)
    for key in ("result", "text", "message"):
        if parsed.get(key):
            return str(parsed[key])[:500]
    content = parsed.get("content")
    if isinstance(content, list):
        text = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        if text:
            return text[:500]
    raise ValueError("Claude JSON had no result text")


def _parse_codex(stdout):
    last = ""
    for line in stdout.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        item = event.get("item", {})
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            last = item.get("text", "")
        elif event.get("type") == "error":
            raise ValueError(event.get("message", "codex error"))
    if not last:
        raise ValueError("Codex JSONL had no final agent message")
    return last[:500]


def _parse_openrouter(stdout):
    parsed = json.loads(stdout)
    choices = parsed.get("choices") or []
    if not choices:
        raise ValueError("OpenRouter response had no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    if content:
        return str(content)[:500]
    raise ValueError("OpenRouter response had no message content")


def _failure_result(exit_code, stdout, stderr):
    output = (stderr or stdout or f"Exit code {exit_code}")[:500]
    return {"ok": False, "exit_code": exit_code, "output": output, "stdout": stdout, "stderr": stderr}


def _load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.rename(STATE_FILE)


def _append_log(path, provider, cmd, result):
    with open(path, "a") as f:
        f.write(f"Timestamp: {datetime.now().isoformat()}\n")
        f.write(f"Provider: {provider}\n")
        f.write(f"Exit code: {result['exit_code']}\n")
        f.write(f"Command: {' '.join(str(c) for c in cmd[:8])}\n")
        f.write(f"\n--- STDOUT ---\n{result.get('stdout', '')}\n")
        f.write(f"\n--- STDERR ---\n{result.get('stderr', '')}\n\n")
