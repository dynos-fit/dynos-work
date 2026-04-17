---
name: global
description: "Internal dynos-work skill. Manage the global cross-project sweeper daemon."
---

# dynos-work: Global

Manage the global daemon that sweeps all registered projects.

## Usage

```
/dynos-work:global start             # start sweeper
/dynos-work:global stop              # stop sweeper
/dynos-work:global status            # show health
/dynos-work:global run-once          # single sweep
/dynos-work:global logs              # sweep history
```

## What you do

Parse the user's arguments to determine the subcommand. Run the corresponding command:

### start
```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/dynoglobal.py" start
```

### stop
```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/dynoglobal.py" stop
```

### status
```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/dynoglobal.py" status
```

Print the JSON result in a human-readable format: running/stopped, PID, last sweep time, project count.

### run-once
```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/dynoglobal.py" run-once
```

### logs
```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/dynoglobal.py" logs
```

## Default

If no subcommand is given, show the status.
