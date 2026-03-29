---
name: execute
description: "Power user: Build execution graph, snapshot, and run all executor segments. Runs EXECUTION_GRAPH_BUILD → PRE_EXECUTION_SNAPSHOT → EXECUTION. Use after /dynos-work:plan."
---

# dynos-work: Execute

Builds the execution graph, creates a git snapshot, then runs all executor segments in dependency order. When done, run `/dynos-work:test`.

## What you do

### Step 1 — Find active task

Find the most recent active task in `.dynos/`. Read `manifest.json`, `spec.md`, `plan.md`.

Verify stage is `EXECUTION_GRAPH_BUILD`. If not, print the current stage and what command to run instead.

### Step 2 — Build execution graph

Append to execution log:
```
{timestamp} [STAGE] → EXECUTION_GRAPH_BUILD
{timestamp} [SPAWN] execution-coordinator — build execution graph
```

Spawn the `execution-coordinator` agent with instruction: "Read `spec.md` and `plan.md`. Build the execution graph. Write to `.dynos/task-{id}/execution-graph.json`. Each segment must declare: id, executor, description, files_expected, depends_on, parallelizable."

Wait for completion. Verify `execution-graph.json` exists. Append to log:
```
{timestamp} [DONE] execution-coordinator — {N} segments planned
```

### Step 3 — Git snapshot

Update `manifest.json` stage to `PRE_EXECUTION_SNAPSHOT`. Append to log:
```
{timestamp} [STAGE] → PRE_EXECUTION_SNAPSHOT
```

1. Run `git stash create` if uncommitted changes exist
2. Run `git branch dynos/task-{id}-snapshot` at current HEAD
3. Record in `manifest.json` under `snapshot`: branch name, stash_ref (or null), head_sha

Append to log:
```
{timestamp} [DECISION] snapshot created — branch dynos/task-{id}-snapshot at {head_sha}
```

### Step 4 — Execute segments

Update `manifest.json` stage to `EXECUTION`. Append to log:
```
{timestamp} [STAGE] → EXECUTION
```

Read `execution-graph.json`. Find all segments with empty `depends_on`. Spawn their executor agents in parallel.

Executor agents by type:
- `ui-executor` → ui-executor agent
- `backend-executor` → backend-executor agent
- `ml-executor` → ml-executor agent
- `db-executor` → db-executor agent
- `refactor-executor` → refactor-executor agent
- `testing-executor` → testing-executor agent
- `integration-executor` → integration-executor agent

Each executor receives: task description, its segment, `spec.md`, `plan.md`, instruction to write evidence to `.dynos/task-{id}/evidence/{segment-id}.md`.

After each batch completes:
- Update `manifest.json` execution_progress
- Append to log: `{timestamp} [DONE] {segment-id} — complete`
- Find next unblocked batch and spawn

Repeat until all segments have evidence files.

### Step 5 — Done

Update `manifest.json` stage to `TEST_EXECUTION`. Append to log:
```
{timestamp} [ADVANCE] EXECUTION → TEST_EXECUTION
```

Print:
```
Execution complete. {N}/{N} segments done.

Next: /dynos-work:test
```
