---
name: execute
description: "Power user: Snapshot, run all executor segments, then run the test suite. Runs PRE_EXECUTION_SNAPSHOT → EXECUTION → TEST_EXECUTION. Use after /dynos-work:start."
---

# dynos-work: Execute

Creates a git snapshot, runs all executor segments in dependency order, then runs the test suite. The execution graph (`execution-graph.json`) is generated during `/dynos-work:start`. When done, run `/dynos-work:audit` (pass) or `/dynos-work:repair` (fail).

## What you do

### Step 1 — Find active task

Find the most recent active task in `.dynos/`. Read `manifest.json`, `spec.md`, `plan.md`, `execution-graph.json`.

Verify stage is `PRE_EXECUTION_SNAPSHOT`. If not, print the current stage and what command to run instead.

Verify `execution-graph.json` exists (generated during `/dynos-work:start`). If missing, print error and stop.

### Step 2 — Git snapshot

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

### Step 3 — Execute segments

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

Each executor receives:
1. Its specific segment object from `execution-graph.json`
2. The full text of each acceptance criterion referenced by the segment's `criteria_ids` field, extracted from `spec.md` (include the criterion number and full text, not just IDs)
3. Evidence files from dependency segments: for each segment ID in the executor's `depends_on` list, read `.dynos/task-{id}/evidence/{dependency-segment-id}.md` and include its contents
4. Instruction to write evidence to `.dynos/task-{id}/evidence/{segment-id}.md`
5. If `dynos_patterns.md` exists in project memory, check it for patterns relevant to your executor type.

Do NOT pass the full `spec.md` or `plan.md` to executors. The extracted criteria and segment contain all the context the executor needs.

After each batch completes:
- Update `manifest.json` execution_progress
- Append to log: `{timestamp} [DONE] {segment-id} — complete`
- Find next unblocked batch and spawn

Repeat until all segments have evidence files.

Append to log:
```
{timestamp} [ADVANCE] EXECUTION → TEST_EXECUTION
```

### Step 4 — Run tests

Update `manifest.json` stage to `TEST_EXECUTION`. Append to log:
```
{timestamp} [STAGE] → TEST_EXECUTION
```

Detect the test command:
- `pubspec.yaml` → `flutter test`
- `package.json` with `scripts.test` → `npm test`
- `Cargo.toml` → `cargo test`
- `go.mod` → `go test ./...`
- `pytest.ini` / `pyproject.toml` / `setup.py` → `pytest`
- `Makefile` with `test` target → `make test`
- None found → skip, advance to CHECKPOINT_AUDIT

Run the test command via Bash. Capture output. Append to log:
```
{timestamp} [TEST] {command} — running
```

### Step 5 — Gate on result

**If all tests pass:**
```
{timestamp} [TEST] {command} — passed ({N} tests)
{timestamp} [ADVANCE] TEST_EXECUTION → CHECKPOINT_AUDIT
```
Update stage to `CHECKPOINT_AUDIT`. Print:
```
Execution complete. {N}/{N} segments done. All tests passed.

Next: /dynos-work:audit
```

**If tests fail:**
Write `.dynos/task-{id}/test-results.json`:
```json
{
  "run_at": "ISO timestamp",
  "command": "...",
  "passed": false,
  "output_summary": "...",
  "failing_tests": ["..."]
}
```
Append to log:
```
{timestamp} [TEST] {command} — FAILED ({N} failing)
{timestamp} [ADVANCE] TEST_EXECUTION → REPAIR_PLANNING
```
Update stage to `REPAIR_PLANNING`. Print:
```
Execution complete. {N}/{N} segments done.
Tests failed: [list of failing tests]

Next: /dynos-work:repair
```

**If no test framework found:**
Append to log:
```
{timestamp} [TEST] no test framework detected — skipping
{timestamp} [ADVANCE] TEST_EXECUTION → CHECKPOINT_AUDIT
```
Update stage to `CHECKPOINT_AUDIT`. Print:
```
Execution complete. {N}/{N} segments done. No test framework detected — skipping tests.

Next: /dynos-work:audit
```
