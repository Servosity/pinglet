---
name: pinglet
description: Inspect and operate this Pinglet repo: status, schedules, alerts, run/ignore tasks, add/enable tasks, and parse JSON CLI output.
---

# Pinglet

Use from the repo root:

```bash
uv run python pinglet.py --status
```

Parse JSON commands by checking `ok`. On `ok: false`, report `error` and stop before mutating anything else.

## Inspect

- System health: `uv run python pinglet.py --status`
- All tasks: `uv run python pinglet.py --list --json`
- One task: `uv run python pinglet.py --task-show <task-id>`
- Recent logs: `uv run python pinglet.py --task-logs <task-id> 100`

Use `--task-show` for schedule, LaunchAgent state, recent logs, and whether `on_failure` or `on_diagnose` is configured.

## Agent Runners

Runner order lives in `config.yaml`:

```yaml
agent_runners:
  primary: {provider: codex, model: gpt-5.5}
  secondary: {provider: claude, model: sonnet-4.6, fallback_model: sonnet-5}
  tertiary: {enabled: false, provider: openrouter, model: qwen/qwen3-coder}
```

Slot order is the default. Pinglet falls back after three consecutive provider failures for the same callback.

## Run Or Ignore

- Run a missed task: `uv run python pinglet.py --run-now <task-id>`
- Run any task immediately: `uv run python pinglet.py --task <task-id>`
- Ignore a missed task until its next scheduled run: `uv run python pinglet.py --ignore <task-id>`

## Schedules And Alerts

- Health summary: `uv run python pinglet.py --healthcheck`
- Missed-task detection: `uv run python pinglet.py --heartbeat`
- Change schedule: `uv run python pinglet.py --schedule <task-id> "every 2h"`
- Reload schedule: `uv run python pinglet.py --task-enable <task-id>`

## Add Or Enable

```bash
uv run python pinglet.py --task-add <task-id> \
  --command uv --args run python script.py \
  --working-dir /path/to/project \
  --schedule-spec "daily 7:00"
uv run python pinglet.py --task-enable <task-id>
```

Use `--dry-run` first for destructive or config-changing commands. Never expose `.env`, `config.yaml`, logs, state, Slack DM IDs, auth files, or local home paths in summaries.
