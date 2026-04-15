---
name: autofix
description: "Toggle automatic code scanning. Finds bugs, dead code, and security issues — opens PRs with fixes while you sleep."
---

# dynos-work: Autofix

Toggle automatic code scanning and fixing for the current project.

## Usage

```
/dynos-work:autofix on        # enable autofix
/dynos-work:autofix off       # disable autofix
/dynos-work:autofix status    # check if enabled
```

## What you do

Parse the user's argument (on, off, or status).

### on

1. Ensure the project is registered and daemon is running:
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/dynoregistry.py" register "$(pwd)" 2>/dev/null || true
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/dynomaintain.py" start --root "$(pwd)" --autofix 2>/dev/null || true
```

2. Print:
```
Autofix enabled. Your code will be scanned for bugs, dead code, and security issues.
Safe fixes will open PRs automatically. Risky findings will open GitHub issues for your review.
```

### off

1. Remove the autofix flag:
```bash
rm -f "$(pwd)/.dynos/maintenance/autofix.enabled"
```

2. Print:
```
Autofix disabled. The daemon will continue running for learning (patterns, postmortems) but won't scan or fix code.
To re-enable: /dynos-work:autofix on
```

### status

1. Check the flag:
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/dynomaintain.py" status --root "$(pwd)"
```

2. Print whether autofix is on or off, daemon running or stopped, and last scan time if available.

### Default (no argument)

Show the status.
