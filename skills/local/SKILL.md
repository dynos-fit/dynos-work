---
name: local
description: "Internal dynos-work skill. Manage the project daemon: start, stop, status, logs, run-once, dashboard."
---

# dynos-work: Local

Manage the local project daemon.

## Usage

```
/dynos-work:local start              # start daemon
/dynos-work:local stop               # stop daemon
/dynos-work:local status             # show daemon status
/dynos-work:local logs               # show cycle history
/dynos-work:local run-once           # run single maintenance cycle
/dynos-work:local dashboard          # generate per-project HTML dashboard
```

## What you do

Parse the user's arguments to determine the subcommand. Run the corresponding Python command:

### start
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/dynomaintain.py" start --root .
```

### stop
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/dynomaintain.py" stop --root .
```

### status
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/dynomaintain.py" status --root .
```

Print the JSON result in a human-readable format: running/stopped, last cycle time, cycle count.

### logs
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/dynomaintain.py" logs --root .
```

### run-once
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/dynomaintain.py" run-once --root .
```

### dashboard
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/dynodashboard.py" generate --root .
```

Print the path to the generated HTML file. This is generate-only (no server). The global dashboard (`/dynos-work:dashboard`) is the web server for all projects.

## Default

If no subcommand is given, show the status.
