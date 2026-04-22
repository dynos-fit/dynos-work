# Manual Terminal Pipeline

This is the practical entrypoint if you want to drive the **live dynos-work branch** without Claude Code.

Use [tools/manual_pipeline.py](/Users/hassam/Documents/dynos-work/tools/manual_pipeline.py).

## What it does

- bootstraps a task under `.dynos/task-<id>/`
- writes `raw-input.md`, `manifest.json`, and `execution-log.md`
- runs `hooks/ctl.py validate-task`
- prints the exact next stage-specific terminal steps for the task

## Important limitation

This does **not** magically make the live branch fully headless.

The live branch still expects a human or another agent to author:
- `spec.md`
- `plan.md`
- `execution-graph.json`
- audit reports
- parts of the repair loop

So this tool is an honest manual runner, not a fake full autopilot.

## Starting point

From the repo root:

```bash
bin/dynos init
python3 tools/manual_pipeline.py init --text "Your task description here"
```

Or from a file:

```bash
python3 tools/manual_pipeline.py init --input-file /path/to/task.txt
```

The command prints:
- the allocated task id
- the task directory
- validation result
- the next exact terminal actions for the current stage

## Ask for the next steps later

At any time:

```bash
python3 tools/manual_pipeline.py guide .dynos/task-YYYYMMDD-NNN
```

## Re-run validation

```bash
python3 tools/manual_pipeline.py validate .dynos/task-YYYYMMDD-NNN
```

## What the guide will tell you

Depending on the current `manifest.json` stage, it will tell you the next manual commands for:
- bootstrap / classification
- spec normalization and spec approval
- planning and plan review
- execution routing and executor flow
- audit and repair flow
- completion / calibration follow-up

## Example

```bash
python3 tools/manual_pipeline.py init --text "Add retry handling to the webhook processor without changing public API"
python3 tools/manual_pipeline.py guide .dynos/task-20260421-001
```
