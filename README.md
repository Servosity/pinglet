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
| `reliability` | No | global defaults | Per-task reliability overrides (see below) |

### Reliability System

Pinglet includes a reliability system that reduces alert fatigue by:
1. **Automatic retry** with exponential backoff (10s → 60s → 300s)
2. **Consecutive failure threshold** - only alert after N failures
3. **Alert cooldown** - don't spam if task keeps failing
4. **Recovery notifications** - notify when task recovers

#### Global Reliability Configuration

```yaml
reliability:
  retry:
    max_attempts: 3                 # Total attempts per run
    delays_seconds: [10, 60, 300]   # Backoff delays
    jitter: 0.25                    # ±25% randomization
  alert:
    consecutive_failures: 3         # Alert after N failures
    cooldown_minutes: 30            # Min time between alerts
  notify_on_recovery: true
```

#### Per-Task Reliability Overrides

```yaml
tasks:
  my-task:
    # ... other fields ...
    reliability:
      alert:
        consecutive_failures: 5     # Override threshold for this task
```

#### Choosing `consecutive_failures` Threshold

| Threshold | Use Case | Examples |
|-----------|----------|----------|
| 1 | Critical/infrequent tasks where any failure needs attention | Financial syncs, production deploys |
| 2-3 | Important tasks that may have occasional transient failures | API integrations, external dependencies |
| 3-5 | Frequent tasks where transient failures are common | Hourly syncs, git operations |
| 5+ | Very frequent tasks with known flakiness | Minute-level polling, network checks |

**Factors to consider:**
1. **Schedule frequency**: Hourly tasks can tolerate more failures than daily tasks
2. **External dependencies**: API calls are flakier than local operations
3. **Impact of delay**: How bad is a 3-hour delay in noticing failure?
4. **Recovery likelihood**: Will retries likely fix it, or does it need human intervention?

### Exit Code Handling

| Exit Code | Action |
|-----------|--------|
| 0 | Log success, update state, check for recovery notification |
| Non-zero | Retry up to N times, then check threshold for alert |
| 124 (timeout) | Same as non-zero with timeout message |

## Current Tasks

| Task ID | Name | Schedule | Failure Threshold |
|---------|------|----------|-------------------|
| `uce` | UCE Link Collector | 7am/7pm daily | 3 (36hr worst case) |
| `git-sync` | Obsidian Git-Sync | Hourly | 5 (5hr tolerance) |
| `claude-backup` | Claude Code Backup | Hourly | 5 (5hr tolerance) |

## Project Structure

```
pinglet/
├── pinglet.py          # Main entry point
├── config.yaml         # Task registry and configuration
├── lib/
│   ├── alerts.py       # Slack + macOS notifications
│   ├── reliability.py  # Retry, threshold, cooldown logic
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
  - Last alert time (for cooldown)
  - Was failing flag (for recovery detection)

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
