# Pinglet

A universal task wrapper that guarantees no silent failures. Wraps scheduled tasks with unified logging, state tracking, and alerts (Slack + macOS).

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

## Adding a New Task

### Step 1: Add task configuration to `config.yaml`

Add a new entry under the `tasks:` section:

```yaml
tasks:
  # ... existing tasks ...

  my-new-task:
    name: My New Task                    # Display name for alerts
    command: /path/to/venv/bin/python    # Executable path (use absolute paths)
    args:                                # Command arguments as list
      - script.py
      - --flag
      - value
    working_dir: /path/to/project        # Working directory for execution
    timeout: 300                         # Timeout in seconds (default: 300)
    env:                                 # Optional: environment variables to pass through
      - SLACK_USER_TOKEN
      - API_KEY
```

### Step 2: Add health check interval (optional)

If you want health checks to alert when the task hasn't run:

```yaml
healthcheck:
  expected_intervals:
    # ... existing intervals ...
    my-new-task: 2    # Alert if not run within 2 hours
```

### Step 3: Create a LaunchAgent (for scheduled execution)

Create a plist file at `~/Library/LaunchAgents/com.pinglet.my-new-task.plist`:

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

    <!-- For hourly execution -->
    <key>StartInterval</key>
    <integer>3600</integer>

    <!-- OR for specific times (7am and 7pm) -->
    <!--
    <key>StartCalendarInterval</key>
    <array>
        <dict>
            <key>Hour</key><integer>7</integer>
            <key>Minute</key><integer>0</integer>
        </dict>
        <dict>
            <key>Hour</key><integer>19</integer>
            <key>Minute</key><integer>0</integer>
        </dict>
    </array>
    -->

    <key>StandardOutPath</key>
    <string>/path/to/pinglet/logs/launchd-my-new-task.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/pinglet/logs/launchd-my-new-task.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

### Step 4: Load the LaunchAgent

```bash
# Load the agent
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pinglet.my-new-task.plist

# To unload (for updates)
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.pinglet.my-new-task.plist

# Manual test run
launchctl kickstart -k gui/$(id -u)/com.pinglet.my-new-task
```

## Configuration Reference

### Task Configuration Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | No | task key | Display name shown in alerts |
| `command` | Yes | - | Absolute path to executable |
| `args` | No | `[]` | List of command arguments |
| `working_dir` | No | pinglet dir | Working directory for execution |
| `timeout` | No | `300` | Timeout in seconds |
| `env` | No | `[]` | Environment variables to pass through from parent |

### Exit Code Handling

| Exit Code | Action |
|-----------|--------|
| 0 | Log success, update state, silent |
| Non-zero | Slack alert + macOS notification + log |
| 124 (timeout) | Same as non-zero with timeout message |

## Current Tasks

| Task ID | Name | Schedule |
|---------|------|----------|
| `uce` | UCE Link Collector | 7am/7pm daily |
| `git-sync` | Obsidian Git-Sync | Hourly |
| `claude-backup` | Claude Code Backup | Hourly |

## Project Structure

```
pinglet/
├── pinglet.py          # Main entry point
├── config.yaml         # Task registry and configuration
├── lib/
│   ├── alerts.py       # Slack + macOS notifications
│   ├── state.py        # Task state tracking (JSON)
│   └── logging.py      # Structured logging
├── state/              # Per-task state files (*.json)
├── logs/               # Log files
└── venv/               # Python virtual environment
```

## Logs and State

- **Logs**: `logs/pinglet.log` - Combined log for all tasks
- **State**: `state/<task-id>.json` - Per-task state including:
  - Last run time
  - Last status (success/failed/timeout)
  - Consecutive failures count
  - Total runs/failures

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
