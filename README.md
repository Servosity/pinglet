# Pinglet

A universal task wrapper that guarantees no silent failures. Wraps scheduled tasks with unified logging, state tracking, and alerts (Slack + macOS).

## Features

- **Reliability System**: Automatic retry with exponential backoff, consecutive failure thresholds, alert cooldown
- **Missed Task Detection**: Hourly heartbeat detects tasks that couldn't run (laptop asleep, etc.)
- **Actionable Notifications**: macOS notifications with Run/Ignore buttons, Slack messages
- **Output Formatting**: Task-configurable output parsing for rich notification summaries (JSON/text)
- **Task Queue**: Sequential execution with configurable gaps between tasks

## Quick Start

```bash
# Run a task
./venv/bin/python pinglet.py --task uce

# List all tasks
./venv/bin/python pinglet.py --list

# Run health check
./venv/bin/python pinglet.py --healthcheck

# Test alerts
./venv/bin/python pinglet.py --test-alerts
```

## Missed Task Detection

Pinglet can detect when scheduled tasks haven't run (e.g., laptop was asleep) and notify you with actionable options.

### Install Heartbeat

```bash
# Install the hourly heartbeat LaunchAgent
./venv/bin/python pinglet.py --install-heartbeat

# To uninstall
./venv/bin/python pinglet.py --uninstall-heartbeat
```

### Manual Commands

```bash
# Run heartbeat check now
./venv/bin/python pinglet.py --heartbeat

# Run a missed task immediately (from notification)
./venv/bin/python pinglet.py --run-now uce

# Ignore a missed task until next scheduled run
./venv/bin/python pinglet.py --ignore uce
```

### How It Works

1. Heartbeat runs hourly via LaunchAgent
2. Checks each task's `last_run` against `healthcheck.expected_intervals`
3. For missed tasks, sends macOS notification with Run/Ignore buttons
4. Also sends Slack message (informational only)
5. 30-second wake delay allows system to stabilize after wake

### Configuration

```yaml
heartbeat:
  enabled: true
  interval_minutes: 60          # How often heartbeat runs
  wake_delay_seconds: 30        # Delay before notifying after detecting gaps

healthcheck:
  expected_intervals:           # Hours - task is "missed" if exceeded
    uce: 14
    git-sync: 2
```

## Output Formatting

Tasks can configure custom output formatters for rich notification summaries.

### Text Format (Default)

Shows last 5 lines of stdout, truncated to 200 characters.

```yaml
tasks:
  my-task:
    output:
      format: text
```

### JSON Format

Parses stdout as JSON and applies template string substitution.

```yaml
tasks:
  obsidian-tab-archiver:
    output:
      format: json
      summary_template: "Archived {tabs_archived} tabs, kept {tabs_kept}"
```

With stdout `{"tabs_archived": 12, "tabs_kept": 8}`, the notification shows:
```
Archived 12 tabs, kept 8
```

## Adding a New Task

### Step 1: Add task configuration to `config.yaml`

```yaml
tasks:
  my-new-task:
    name: My New Task                    # Display name for alerts
    command: /path/to/venv/bin/python    # Executable path (use absolute paths)
    args:                                # Command arguments as list
      - script.py
      - --flag
    working_dir: /path/to/project        # Working directory for execution
    timeout: 300                         # Timeout in seconds (default: 300)
    env:                                 # Optional: environment variables to pass
      - SLACK_USER_TOKEN
    output:                              # Optional: output formatting
      format: text                       # "text" or "json"
      summary_template: null             # Template for JSON format
    reliability:                         # Optional: per-task overrides
      alert:
        consecutive_failures: 3
```

### Step 2: Add health check interval (optional)

```yaml
healthcheck:
  expected_intervals:
    my-new-task: 2    # Alert if not run within 2 hours
```

### Step 3: Create a LaunchAgent (for scheduled execution)

Create `~/Library/LaunchAgents/com.pinglet.my-new-task.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.pinglet.my-new-task</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/pinglet/venv/bin/python</string>
        <string>/path/to/pinglet/pinglet.py</string>
        <string>--task</string>
        <string>my-new-task</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/pinglet</string>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>StandardOutPath</key>
    <string>/path/to/pinglet/logs/launchd-my-new-task.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/pinglet/logs/launchd-my-new-task.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
```

### Step 4: Load the LaunchAgent

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pinglet.my-new-task.plist
```

## CLI Reference

| Command | Description |
|---------|-------------|
| `--task <name>` | Run a registered task |
| `--list` | List all registered tasks |
| `--healthcheck` | Run daily health summary |
| `--test-alerts` | Test notification system |
| `--run-now <task>` | Run a missed task immediately |
| `--ignore <task>` | Mark a missed task as ignored |
| `--heartbeat` | Run heartbeat check for missed tasks |
| `--install-heartbeat` | Install heartbeat LaunchAgent |
| `--uninstall-heartbeat` | Uninstall heartbeat LaunchAgent |

## Configuration Reference

### Notifications

```yaml
notifications:
  on_success: false             # Send notifications on success
  on_failure: true              # Send notifications on failure
  slack_enabled: true
  macos_enabled: true
  success_silent: true          # Success notifications are silent (no sound)
  manual_complete_silent: true  # Manual run notifications are silent
```

### Reliability System

Reduces alert fatigue by:
1. **Automatic retry** with exponential backoff (10s → 60s → 300s)
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
    consecutive_failures: 3     # Alert after N failures
    cooldown_minutes: 30        # Min time between alerts
  notify_on_recovery: true
```

### Choosing `consecutive_failures` Threshold

| Threshold | Use Case |
|-----------|----------|
| 1 | Critical/infrequent tasks |
| 2-3 | Important tasks with occasional transient failures |
| 3-5 | Frequent tasks where transient failures are common |
| 5+ | Very frequent tasks with known flakiness |

## Project Structure

```
pinglet/
├── pinglet.py              # Main entry point
├── config.yaml             # Task registry and configuration
├── lib/
│   ├── alerts.py           # Slack + macOS notifications
│   ├── reliability.py      # Retry, threshold, cooldown logic
│   ├── state.py            # Task state tracking (JSON)
│   ├── logging.py          # Structured logging
│   ├── heartbeat.py        # Missed task detection
│   ├── ignored.py          # Ignored tasks management
│   ├── queue.py            # Task queue for sequential execution
│   └── output_formatter.py # Output formatting (JSON/text)
├── tests/                  # Test suite (79 tests)
├── state/                  # Per-task state files (*.json)
├── logs/                   # Log files
└── launchagents/           # Generated LaunchAgent plists
```

## State Files

- **Task State**: `state/<task-id>.json` - Last run, status, failures, etc.
- **Ignored Tasks**: `state/ignored.json` - Tasks marked to ignore until next run

## Current Tasks

| Task ID | Name | Schedule | Failure Threshold |
|---------|------|----------|-------------------|
| `uce` | UCE Link Collector | 7am/7pm daily | 3 |
| `git-sync` | Obsidian Git-Sync | Hourly | 5 |
| `claude-backup` | Claude Code Backup | Hourly | 5 |

## Troubleshooting

### Test a task manually
```bash
./venv/bin/python pinglet.py --task <task-id>
```

### Check task state
```bash
cat state/<task-id>.json | python -m json.tool
```

### View recent logs
```bash
tail -100 logs/pinglet.log
```

### Check LaunchAgent status
```bash
launchctl list | grep pinglet
```

### Run tests
```bash
./venv/bin/python -m pytest tests/ -v
```
