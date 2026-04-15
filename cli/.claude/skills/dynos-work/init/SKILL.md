---
name: init
description: "Internal dynos-work skill. Set up the current project: register with dynos-work and start the local daemon."
---

# dynos-work: Init

Register the current project and start the local maintenance daemon.

## Usage

```
/dynos-work:init
/dynos-work:init --autofix
```

## What you do

1. Register the project:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/dynoregistry.py" register "$(pwd)"
```

2. Create the `.dynos` directory:
```bash
mkdir -p .dynos
```

3. Check if a daemon is already running:
```bash
PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/hooks:${PYTHONPATH:-}" python3 "${CLAUDE_PLUGIN_ROOT}/hooks/dynomaintain.py" status --root .
```

If `running` is `true` in the JSON output, print "Daemon already running" and stop.

4. Start the daemon. If the user passed `--autofix` in the arguments, include the flag:
```bash
PYTHONPATH="${CLAUDE_PLUGIN_ROOT}/hooks:${PYTHONPATH:-}" python3 "${CLAUDE_PLUGIN_ROOT}/hooks/dynomaintain.py" start --root . [--autofix]
```

5. Print the result:
```
Project registered and daemon started.
Use /dynos-work:start to begin a task.
Use /dynos-work:local status to check daemon health.
```
