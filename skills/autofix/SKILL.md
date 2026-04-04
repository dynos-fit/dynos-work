---
name: autofix
description: "Run the autofix scanner: detect technical debt, open PRs for low-risk fixes, open issues for high-risk findings."
---

# dynos-work: Autofix

Run the proactive scanner to detect and fix technical debt.

## Usage

```
/dynos-work:autofix                  # scan all registered projects
/dynos-work:autofix scan             # same as above
/dynos-work:autofix scan /path       # scan one project (must be registered)
/dynos-work:autofix list             # show current findings
/dynos-work:autofix clear            # reset findings history
```

## What you do

### scan (default)

If no path is given, scan ALL registered active projects:

```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 -c "
import json, sys
sys.path.insert(0, '${PLUGIN_HOOKS}')
from dynoglobal import load_registry
reg = load_registry()
for p in reg.get('projects', []):
    if p.get('status') == 'active':
        print(p['path'])
"
```

For each project path, run:
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/dynoproactive.py" scan --root <path>
```

Print the JSON results for each project.

If a specific path is given, check it is registered first (query the registry). If not registered, tell the user to run `/dynos-work:init` in that project first.

### list

```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/dynoproactive.py" list --root .
```

Print the findings in a human-readable format.

### clear

```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/dynoproactive.py" clear --root .
```

Confirm the reset.
