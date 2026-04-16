---
name: dashboard
description: "Internal dynos-work skill. Start the global dashboard server showing all registered projects."
---

# dynos-work: Dashboard

Start the global dashboard server. Shows all registered projects in a unified web UI.

## Usage

```
/dynos-work:dashboard              # start server at :8766
/dynos-work:dashboard stop         # stop server
/dynos-work:dashboard restart      # restart server
```

## What you do

### serve (default)

```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/sweeper.py" dashboard serve
```

Print the URL: `http://127.0.0.1:8766/global-dashboard.html`

### stop

```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/sweeper.py" dashboard kill
```

### restart

```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/sweeper.py" dashboard restart
```

## Notes

The dashboard runs as a background HTTP server. It survives after the conversation ends. Use `stop` to shut it down.

The dashboard shows:
- Global daemon status and aggregate stats
- Per-project cards (click to expand full detail)
- Active routes, recent tasks, benchmark runs, findings
- Autofix PRs per project
- Maintenance cycle history
