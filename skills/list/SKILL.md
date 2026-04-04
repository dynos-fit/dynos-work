---
name: list
description: "List all registered dynos-work projects."
---

# dynos-work: List

Show all projects registered with dynos-work.

## Usage

```
/dynos-work:list
```

## What you do

```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/dynoregistry.py" list
```

Print the results in a human-readable format showing: project path, status (active/paused/archived), last active timestamp.
