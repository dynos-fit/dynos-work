---
name: test
description: "Power user: Run the project test suite and gate on pass/fail. Use after /dynos-work:execute. On pass, run /dynos-work:audit. On fail, run /dynos-work:repair."
---

# dynos-work: Test

Runs the project's test suite. Gates the lifecycle on pass/fail. When done, run `/dynos-work:audit` (pass) or `/dynos-work:repair` (fail).

## What you do

### Step 1 — Find active task

Find the most recent active task in `.dynos/`. Read `manifest.json`.

Verify stage is `TEST_EXECUTION`. If not, print the current stage and what command to run instead.

### Step 2 — Detect and run tests

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

### Step 3 — Gate on result

**If all tests pass:**
```
{timestamp} [TEST] {command} — passed ({N} tests)
{timestamp} [ADVANCE] TEST_EXECUTION → CHECKPOINT_AUDIT
```
Update stage to `CHECKPOINT_AUDIT`. Print:
```
All tests passed. Next: /dynos-work:audit
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
Tests failed. Failing tests: [list]

Next: /dynos-work:repair
```
