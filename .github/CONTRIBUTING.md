# Contributing to dynos-work

## How the system is structured

```
skills/start/       ← entry point, owns discovery + spec review gates
skills/lifecycle/   ← state machine controller, owns execution through DONE
skills/planning/    ← planner subagent (spec, classification, plan)
skills/audit/       ← standalone audit power user command
skills/status/      ← task status power user command
skills/repair/      ← manual repair power user command
skills/resume/      ← resume interrupted task power user command
agents/             ← executor and auditor agent definitions
```

## Making changes

**Behavior changes** (new stage, new auditor, modified gate logic) require a version bump in `.claude-plugin/plugin.json`.

**Docs only** changes do not require a version bump.

**Commit style:** `type: short description` where type is `feat`, `fix`, `docs`, or `chore`.

## Testing your changes

1. Clear the plugin cache: `rm -rf ~/.claude/plugins/cache/dynos-work`
2. Reinstall: `/plugin install dynos-work`
3. Open a fresh session in a test project
4. Run `/dynos-work:start <small task>`
5. Check `.dynos/task-{id}/execution-log.md` to verify stage sequence

## What not to change

The Lifecycle Controller is the only entity that writes `stage` to `manifest.json` and the only entity that writes `DONE` or `FAILED`. Do not add stage-writing logic to executors or auditors.
