---
name: list
description: "Internal dynos-work skill. List all registered dynos-work projects."
---

# dynos-work: List

Show all projects registered with dynos-work.

## Usage

```
/dynos-work:list
```

## What you do

```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/dynoregistry.py" list
```

Print the results in a human-readable format showing: project path, status (active/paused/archived), last active timestamp.
