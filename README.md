<p align="center">
  <img src="assets/pinglet-logo-readme.png" alt="Pinglet - No silent failures" width="500">
</p>

# Pinglet

A universal task wrapper for macOS that guarantees no silent failures. Wraps scheduled tasks with unified logging, state tracking, and alerts (Slack + macOS).

## What's a Pinglet?

A **pinglet** is an individual scheduled task managed by Pinglet. Each pinglet wraps a command with reliability features — retries, state tracking, alerting, and missed-run detection. Examples: the UCE link collector is a pinglet, Obsidian git-sync is a pinglet, the daily health check is a pinglet.

Think of it like pods in Kubernetes or containers in Docker — "pinglet" is the unit noun for a scheduled task-agent in this system.

---

## Usage

Run from repo root. All commands output JSON — parse the `ok` field.

```bash
# Setup (after cloning)
bash scripts/install-hooks.sh

# Full CLI reference
./venv/bin/python ./pinglet.py --help

# List all pinglets
./venv/bin/python ./pinglet.py --list --json

# Full details + state + logs for one pinglet
./venv/bin/python ./pinglet.py --task-show <id>

# Add + enable a pinglet
./venv/bin/python ./pinglet.py --task-add my-task --command /usr/bin/python3 --args script.py --schedule-spec "daily 7:00"
./venv/bin/python ./pinglet.py --task-enable my-task

# Run health check / heartbeat
./venv/bin/python ./pinglet.py --healthcheck
./venv/bin/python ./pinglet.py --heartbeat
```

---

## Features

- **Pinglet Management CLI**: Add, edit, remove, enable, disable pinglets — all via CLI with JSON output
- **Reliability System**: Automatic retry with exponential backoff, consecutive failure thresholds, alert cooldown
- **Missed Pinglet Detection**: Hourly heartbeat detects pinglets that couldn't run (laptop asleep, etc.)
- **Actionable Notifications**: macOS notifications with Run/Ignore buttons, Slack messages
- **Output Formatting**: Per-pinglet output parsing for rich notification summaries (JSON/text)
- **Pinglet Queue**: Sequential execution with configurable gaps between pinglets
- **Monitoring Self-Protection**: Every CLI invocation checks if watchdog agents are alive; surviving agents detect and alert when monitoring is down ("who watches the watchmen")
- **LaunchAgent Health Detection**: Real-time `launchctl` status checks detect disabled (exit 78), failed, and not-loaded agents — not just plist existence
- **Escalation Tiers**: Staleness alerts escalate from warning (2x threshold) to urgent (5x) to critical (10x)

## Adding a New Pinglet

```bash
# Add task with schedule
./venv/bin/python pinglet.py --task-add my-task \
  --command /path/to/venv/bin/python \
  --args script.py --flag \
  --working-dir /path/to/project \
  --name "My Task" \
  --timeout 300 \
  --schedule-spec "daily 7:00"

# Enable it (generates LaunchAgent plist + loads it)
./venv/bin/python pinglet.py --task-enable my-task

# Verify
./venv/bin/python pinglet.py --task-show my-task
```

### Available Config Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--command` | Yes | - | Executable path |
| `--name` | No | Title-cased ID | Display name for notifications |
| `--args` | No | [] | Command arguments |
| `--working-dir` | No | Command's directory | Working directory |
| `--timeout` | No | 300 | Timeout in seconds |
| `--env` | No | [] | Env vars to pass through |
| `--output-format` | No | text | Output format (text/json) |
| `--summary-template` | No | null | Template for JSON output |
| `--failures-before-alert` | No | 3 | Consecutive failures before alerting |
| `--schedule-spec` | No | null | Schedule (see syntax above) |

## Editing and Removing Pinglets

```bash
# Edit (only specified fields update)
./venv/bin/python pinglet.py --task-edit my-task --timeout 600

# Change schedule
./venv/bin/python pinglet.py --schedule my-task "every 2h"
./venv/bin/python pinglet.py --task-enable my-task  # reload with new schedule

# Disable (keeps config, removes LaunchAgent)
./venv/bin/python pinglet.py --task-disable my-task

# Remove entirely (auto-disables + removes config + state)
./venv/bin/python pinglet.py --task-remove my-task

# Preview any change first
./venv/bin/python pinglet.py --task-remove my-task --dry-run
```

## Monitoring & Self-Protection

### LaunchAgent Status Detection

`--list` and `--healthcheck` now query `launchctl` directly for real agent state instead of just checking if the plist file exists. This catches agents that launchd has disabled (exit code 78) — a common failure mode after reboot or macOS updates.

Statuses: `RUNNING`, `IDLE` (loaded, waiting for schedule), `DISABLED` (exit 78), `FAILED`, `NOT_LOADED`, `NOT_INSTALLED`.

### Who Watches the Watchmen

Every CLI invocation checks if the healthcheck and heartbeat monitoring agents are alive. If they're dead, a warning prints to stderr. After any successful task run, a debounced Slack alert fires (24h cooldown) so monitoring failures don't go unnoticed.

### Escalation Tiers

Staleness alerts escalate based on how far past the threshold a task is:

| Multiplier | Level | Example (2h threshold) |
|------------|-------|------------------------|
| 2x | Warning | 4h since last run |
| 5x | Urgent | 10h since last run |
| 10x | Critical | 20h since last run |

### Plist Hardening

Generated plists include `KeepAlive > SuccessfulExit: false` so launchd restarts agents that exit non-zero instead of permanently disabling them. The heartbeat agent uses `KeepAlive: true` for maximum resilience.

## Adaptive Loop

Pinglet implements a detection-recovery-learning flywheel that reduces human intervention over time:

```
          +------------------+
          |   Task Failure   |
          |   (exit != 0)    |
          +--------+---------+
                   |
                   v
          +------------------+
          | Tier 1: Auto-    |<---------+
          | Recovery         |          |
          | (bootout+boot)   |          |
          +--------+---------+          |
                   |                    |
            recovered?                  |
           /          \                 |
         yes           no              |
          |             |               |
          v             v               |
   +-----------+  +------------------+  |
   | Learning  |  | Tier 2: LLM     |  |
   | Loop      |  | Self-Diagnosis  |  |
   | (pattern  |  | (claude -p)     |  |
   |  update)  |  +--------+--------+  |
   +-----------+           |            |
                     fixed?             |
                    /      \            |
                  yes       no          |
                   |         |          |
                   v         v          |
            +-----------+  +----------+ |
            | Learning  |  | Tier 3:  | |
            | Loop      |  | Human    | |
            | (pattern  |  | Alert    | |
            |  update)  |  | (Slack/  | |
            +-----------+  | macOS)   | |
                           +----+-----+ |
                                |       |
                                +-------+
```

### Recovery Cascade

Three tiers execute in order. Each tier only fires if the previous one failed.

| Tier | Action | Trigger |
|------|--------|---------|
| 1 | Auto-recovery (bootout + bootstrap) | Every detection of a disabled/failed agent |
| 2 | LLM self-diagnosis (`claude -p`) | After auto-recovery fails once |
| 3 | Human alert (Slack + macOS) | After `consecutive_detections >= monitoring_alert_threshold` (default: 3) |

### Learning Loop

The heartbeat tracks failure patterns per task in `state/_learning.json` and adapts thresholds automatically.

**Detected patterns:**

| Pattern | Meaning | Adaptation |
|---------|---------|------------|
| `chronic_cycle` | Fails, recovers, fails again repeatedly | Higher alert threshold, `suppressed=true` |
| `intermittent` | Occasional failures with long healthy stretches | Default thresholds maintained |
| `persistent` | Fails and stays failed across multiple checks | Lower alert threshold for faster escalation |

**Noise suppression:** Tasks classified as `chronic_cycle` auto-recoverers are marked `suppressed=true`. They still auto-recover but no longer generate human alerts unless the pattern changes.

**Threshold adaptation:** The learning loop adjusts each task's effective `monitoring_alert_threshold` based on its pattern. Chronic cyclers get a higher threshold (fewer alerts); persistent failures get a lower threshold (faster escalation).

### Default Values

| Setting | Default | Description |
|---------|---------|-------------|
| `retry_max_attempts` | 3 | Retries before declaring failure |
| `retry_delays_seconds` | [10, 60, 300] | Exponential backoff delays |
| `consecutive_failures_before_alert` | 3 | Failures before alerting |
| `alert_cooldown_minutes` | 30 | Min time between alerts |
| `monitoring_alert_threshold` | 3 | Detections before human alert |
| `monitoring_alert_cooldown_hours` | 24 | Cooldown for monitoring alerts |
| `on_failure_timeout` | 180 | Seconds for on_failure callback |
| `on_failure_max_turns` | 5 | Max LLM turns |
| `on_failure_max_budget_usd` | 2.00 | Max spend per callback |
| `self_diagnosis_max_budget_usd` | 1.00 | Max spend per self-diagnosis |

### `on_failure` Callback

When a task fails the alert threshold, Pinglet can invoke an external command (typically `claude -p`) to diagnose and fix the root cause before alerting a human.

**Without `on_failure`**, failure alerts on Slack include:

> *LLM Troubleshooter: Not configured — human intervention required*

**With `on_failure`**, the same alert reports the callback outcome:

> *LLM Troubleshooter: Invoked ✓ (exit 0 — may be fixed, verify next run)*
> *LLM Troubleshooter: Invoked ✗ (exit 1 — human intervention required)*

**Config (config.yaml):**

```yaml
tasks:
  my-task:
    command: ./run.py
    on_failure:
      command: claude
      args:
        - "-p"
        - "Task {task_id} failed (exit {exit_code}). Read {stderr_file}. Check {learning_file}. Fix if possible."
      timeout: 180
      max_turns: 5
      max_budget_usd: 2.00
```

**CLI flags:**

```bash
$P --task-add my-task --command ./run.py \
  --on-failure-command claude \
  --on-failure-prompt "Task {task_id} failed (exit {exit_code}). Read {stderr_file}. Fix if possible." \
  --on-failure-timeout 180 \
  --on-failure-max-turns 5
```

**Template variables** available in `on_failure` prompts:

| Variable | Description |
|----------|-------------|
| `{task_id}` | Task identifier |
| `{task_name}` | Display name |
| `{exit_code}` | Process exit code |
| `{error}` | Error message (if captured) |
| `{log_file}` | Path to combined log file |
| `{stderr_file}` | Path to stderr capture |
| `{stdout_file}` | Path to stdout capture |
| `{working_dir}` | Task working directory |
| `{consecutive_failures}` | Current failure streak count |
| `{state_file}` | Path to task state JSON |
| `{project_root}` | Pinglet install directory |
| `{learning_file}` | Path to `state/_learning.json` |

**Exit code contract:** The callback should exit `0` if it fixed the problem (Pinglet will retry the task), or `1` if human intervention is needed (Pinglet proceeds to alert).

**Sibling: `on_diagnose`** — fires when the **heartbeat** detects a task is stale or its LaunchAgent is disabled (not when the task itself exited non-zero). Same schema, same template variables. If omitted, heartbeat uses a built-in default prompt. Use this to customize how the LLM recovers from missed-run / disabled-agent states.

**Gotcha — Claude Code hooks block the callback:** If you use `claude -p` as the callback and have a `PreToolUse:Bash` hook in `~/.claude/settings.json` that references a per-project path (e.g. `${CLAUDE_PROJECT_DIR}/.claude/hooks/foo.py`), the hook will fail in any project that doesn't have that file, blocking every `Bash` call the LLM tries. The callback exits 0 (the CLI succeeded; the *tools* were denied), so Pinglet thinks it worked. Use absolute paths (`$HOME/.claude/hooks/foo.py`) in Bash hooks.

### `--status` API

The `--status` command returns a JSON overview of all tasks, the adaptive loop state, and recent activity:

```bash
./venv/bin/python pinglet.py --status
```

Example output (abbreviated):

```json
{
  "ok": true,
  "summary": {
    "total_tasks": 4,
    "healthy": 3,
    "failing": 1,
    "disabled": 0
  },
  "adaptive_loop": {
    "learning_file": "state/_learning.json",
    "patterns": {
      "uce": {"pattern": "intermittent", "suppressed": false},
      "git-sync": {"pattern": "chronic_cycle", "suppressed": true}
    },
    "auto_recoveries_24h": 2,
    "human_alerts_24h": 0
  },
  "tasks": [
    {
      "id": "uce",
      "status": "IDLE",
      "last_run": "2026-03-17T07:00:12Z",
      "last_exit_code": 0,
      "consecutive_failures": 0
    }
  ]
}
```

## Missed Pinglet Detection

Pinglet detects when scheduled pinglets haven't run (e.g., laptop was asleep) and notifies with actionable options.

### Install Heartbeat

```bash
./venv/bin/python pinglet.py --install-heartbeat
./venv/bin/python pinglet.py --uninstall-heartbeat
```

### How It Works

1. Heartbeat runs hourly via LaunchAgent
2. Checks each pinglet's `last_run` against `healthcheck.expected_intervals`
3. Checks `launchctl` for disabled/failed agents
4. For missed pinglets, sends macOS notification with Run/Ignore buttons
5. For disabled agents, sends urgent Slack alert with fix commands
6. Also sends Slack message with escalation level (warning/urgent/critical)
7. 30-second wake delay allows system to stabilize after wake

### Manual Commands

```bash
./venv/bin/python pinglet.py --heartbeat       # Run check now
./venv/bin/python pinglet.py --run-now uce     # Run a missed task
./venv/bin/python pinglet.py --ignore uce      # Ignore until next run
```

## Output Formatting

Pinglets can configure custom output formatters for rich notification summaries.

### Text Format (Default)

Shows last 5 lines of stdout, truncated to 200 characters.

### JSON Format

Parses stdout as JSON and applies template string substitution.

```yaml
tasks:
  obsidian-tab-archiver:
    output:
      format: json
      summary_template: "Archived {tabs_archived} tabs, kept {tabs_kept}"
```

With stdout `{"tabs_archived": 12, "tabs_kept": 8}`, the notification shows: `Archived 12 tabs, kept 8`

## Configuration Reference

### Notifications

```yaml
notifications:
  on_success: false
  on_failure: true
  slack_enabled: true
  macos_enabled: true
  success_silent: true
  manual_complete_silent: true
```

### Reliability System

Reduces alert fatigue by:
1. **Automatic retry** with exponential backoff (10s -> 60s -> 300s)
2. **Consecutive failure threshold** - only alert after N failures
3. **Alert cooldown** - don't spam if task keeps failing
4. **Recovery notifications** - notify when task recovers

```yaml
reliability:
  retry:
    max_attempts: 3
    delays_seconds: [10, 60, 300]
    jitter: 0.25
  alert:
    consecutive_failures: 3
    cooldown_minutes: 30
  notify_on_recovery: true
```

### Choosing `consecutive_failures` Threshold

| Threshold | Use Case |
|-----------|----------|
| 1 | Critical/infrequent tasks |
| 2-3 | Important tasks with occasional transient failures |
| 3-5 | Frequent tasks where transient failures are common |
| 5+ | Very frequent tasks with known flakiness |

### Heartbeat

```yaml
heartbeat:
  enabled: true
  interval_minutes: 60
  wake_delay_seconds: 30

healthcheck:
  expected_intervals:
    uce: 14
    git-sync: 2
```

## Project Structure

```
pinglet/
├── pinglet.py              # Main entry point + CLI + watchdog self-check
├── config.yaml             # Task registry and configuration
├── lib/
│   ├── task_manager.py     # Pinglet CRUD, schedule parsing, plist generation, launchd status
│   ├── alerts.py           # Slack + macOS notifications + critical monitoring alerts
│   ├── reliability.py      # Retry, threshold, cooldown logic
│   ├── state.py            # Pinglet state tracking (JSON)
│   ├── logging.py          # Structured logging
│   ├── heartbeat.py        # Missed pinglet detection + disabled agent detection + escalation
│   ├── ignored.py          # Ignored pinglets management
│   ├── queue.py            # Pinglet queue for sequential execution
│   └── output_formatter.py # Output formatting (JSON/text)
├── tests/                  # Test suite (227 tests)
├── state/                  # Per-task state files (*.json)
│   ├── _learning.json      # Adaptive loop pattern history
│   └── _monitoring_down_state.json  # Monitoring agent down-state tracking
├── logs/                   # Log files
└── launchagents/           # Generated LaunchAgent plists
```

## Troubleshooting

```bash
# Show full task debug info (config + state + launchd status + logs)
./venv/bin/python pinglet.py --task-show <task-id>

# View recent logs for a task
./venv/bin/python pinglet.py --task-logs <task-id> 100

# Test a task manually
./venv/bin/python pinglet.py --task <task-id>

# Check LaunchAgent status (shows disabled agents with exit 78)
./venv/bin/python pinglet.py --list

# Raw launchctl status
launchctl list | grep pinglet

# Re-enable a disabled agent
./venv/bin/python pinglet.py --task-enable <task-id>

# Run tests
./venv/bin/python -m pytest tests/ -v
```
