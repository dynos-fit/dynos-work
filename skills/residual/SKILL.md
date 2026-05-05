---
name: residual
description: "Inspect and drain the proactive-residual queue at .dynos/proactive-findings.json. Subcommands: list, run-next."
---

# dynos-work: Residual

Inspect and drain the proactive-residual queue maintained at
`{root}/.dynos/proactive-findings.json` (where `{root}` is the current
project root — the directory that contains `.dynos/`). Two subcommands
are supported: `list` and `run-next`.

## Ruthlessness Standard

- The queue file is the source of truth. Do not derive queue state from
  memory, prior conversation, or vibes — re-read `proactive-findings.json`
  on every invocation.
- `run-next` MUST NOT spawn auditor or executor agents directly. The
  only execution path is `/dynos-work:start`. Any other route bypasses
  the lifecycle, the audit gate, and the round-trip update in
  `cmd_run_audit_finish`.
- Status writes go through `hooks/lib_residuals.update_row_status` (or
  an equivalent LOCK_EX atomic write). Never edit the JSON file by hand
  or with a partial overwrite — concurrent producers (other audits
  finishing) can clobber a non-atomic write.
- Polling is mandatory. A spawned `/dynos-work:start` task is not done
  the moment it returns control. Only the `manifest.json`
  `stage` field tells you when it is finished.

## Subcommand: `list`

Invoked as `/dynos-work:residual list`.

### What you do

1. Resolve `{root}` — the current project root (the directory containing
   `.dynos/`). If running from a repo subdirectory, walk upward until
   `.dynos/` is found.
2. Run the following Python snippet (adjust `ROOT` to the resolved
   project root):

   ```bash
   python3 - <<'PY'
   from pathlib import Path
   import sys
   sys.path.insert(0, str(Path("hooks").resolve()))
   import lib_residuals
   root = Path(".").resolve()
   queue = lib_residuals.queue_path(root)
   data = lib_residuals.load_queue(queue)
   findings = data.get("findings", [])
   if not findings:
       print("dynos-work: no residuals queued")
       sys.exit(0)
   # Sort oldest first by created_at
   findings_sorted = sorted(findings, key=lambda r: r.get("created_at", ""))
   for r in findings_sorted:
       title = r.get("title", "") or ""
       if len(title) > 60:
           title = title[:60]
       print(
           f"{r.get('id','')}\t"
           f"{r.get('status','')}\t"
           f"attempts={r.get('attempts',0)}\t"
           f"{r.get('source_auditor','')}\t"
           f"{title}\t"
           f"{r.get('created_at','')}"
       )
   PY
   ```

3. Output rules — these are CONTRACTUAL:
   - If `proactive-findings.json` does not exist, print exactly
     `dynos-work: no residuals queued` and exit 0.
   - If the file exists but `findings` is `[]`, print exactly
     `dynos-work: no residuals queued` and exit 0.
   - Otherwise print one tab-separated line per row containing, in
     order: `id`, `status`, `attempts`, `source_auditor`,
     `title` (truncated to 60 chars), `created_at`. All six fields must
     appear on every row.
   - Do NOT filter by status. `pending`, `in_progress`, `done`, and
     `failed` rows all appear.
   - Plain text only. No JSON, no ANSI colour codes, no header row
     decoration that would break the six-field shape.

### Edge cases

- Corrupt or unreadable `proactive-findings.json`: `lib_residuals.load_queue`
  treats this as an empty queue and returns `{"findings": []}`. The
  command therefore prints `dynos-work: no residuals queued` rather
  than raising. If you want to surface the corruption to the user,
  print a separate stderr warning (do not change the stdout shape).

## Subcommand: `run-next`

Invoked as `/dynos-work:residual run-next`.

### Step 0 — In-progress guard (do this BEFORE selecting a row)

Before picking a new row, scan the queue for any row whose `status` is
`"in_progress"`. If at least one such row exists, another `run-next`
invocation (or a previously-spawned residual task that has not yet
completed its round-trip) is already in flight. In that case, print
exactly:

```
dynos-work: residual in_progress — run-next already active
```

and exit 0. Do NOT select another row, do NOT mutate the queue, do NOT
spawn `/dynos-work:start`. This guard prevents two concurrent
`run-next` fires from double-selecting and lets a stuck round-trip be
resolved (manually or by `cmd_run_audit_finish`) before another
attempt is consumed.

### Step 1 — Load the queue

Use `lib_residuals.load_queue(lib_residuals.queue_path(root))`. If the
result has an empty `findings` list (file missing or empty), print
exactly `dynos-work: residual queue empty` and exit 0.

### Step 2 — Select the oldest eligible row

Use `lib_residuals.select_next_pending(root)`. This returns the oldest
row (by `created_at` ascending) whose `status == "pending"` AND
`attempts < 3`. A row whose `status == "in_progress"` is NEVER eligible
(it is already in flight). If the function returns `None`, print
exactly `dynos-work: residual queue empty` and exit 0.

### Step 3 — Mark the row in_progress (atomic)

Call `lib_residuals.update_row_status(root, row["id"], "in_progress")`.
This is a LOCK_EX read-modify-write that:

- Sets `status = "in_progress"`.
- Increments `attempts` by 1.
- Sets `last_attempt_at` to the current ISO 8601 UTC timestamp.

If the call raises an exception (filesystem error, race with another
writer, etc.), print the error to stderr and exit non-zero. DO NOT
proceed to step 4 — the queue is in an unknown state and a spawned
task without a corresponding `in_progress` row would lose its
round-trip update.

NOTE: `update_row_status` from the lib only changes `status` (and sets
`last_attempt_at` on the in_progress transition); it does NOT increment
`attempts`. Increment `attempts` in the same critical section by
either calling a helper that wraps both, or by reading the row,
mutating both fields, and writing back via the same LOCK_EX path. The
key invariant is: when control returns from step 3, the row on disk
has `status == "in_progress"` AND `attempts` incremented AND
`last_attempt_at` updated, all visible to subsequent readers.

Recommended Python snippet for step 3:

```bash
python3 - <<'PY'
from pathlib import Path
import sys, json
sys.path.insert(0, str(Path("hooks").resolve()))
import lib_residuals
root = Path(".").resolve()
row = lib_residuals.select_next_pending(root)
if row is None:
    print("dynos-work: residual queue empty")
    sys.exit(0)
# Guard: any in_progress already?
data = lib_residuals.load_queue(lib_residuals.queue_path(root))
if any(r.get("status") == "in_progress" for r in data.get("findings", [])):
    print("dynos-work: residual in_progress — run-next already active")
    sys.exit(0)
lib_residuals.update_row_status(root, row["id"], "in_progress")
# Emit the row payload so the orchestrator can build the start input.
print(json.dumps({"id": row["id"], "description": row.get("description","")}))
PY
```

### Step 4 — Invoke `/dynos-work:start` and POLL until terminal stage

Build the input text for `/dynos-work:start`. The first line MUST be
the residual-id sentinel comment, followed by a blank line, followed
by the row's `description`:

```
<!-- residual-id: {row.id} -->

{row.description}
```

This sentinel is what `cmd_run_audit_finish` greps for via
`lib_residuals.extract_residual_id` to perform the round-trip status
update when the spawned task hits a terminal stage.

Invoke `/dynos-work:start` with that text. The spawned task is a NEW
dynos-work task with its own `task-{id}` directory under `.dynos/` and
its own `manifest.json`.

**WAIT — do NOT continue to step 5 until you have read the spawned
task's `manifest.json` and confirmed `stage` is exactly `"DONE"` or
`"FAILED"`. Reading the manifest once and assuming the task completed
is INCORRECT.** The `/dynos-work:start` skill returns control to you
long before the task is finished — it only kicks off the lifecycle.
The actual work proceeds through PLANNING, EXECUTION, AUDIT, and
REPAIR stages, which can take many minutes.

#### Polling protocol (mandatory)

1. Note the new task's directory `.dynos/task-{id}/` from the
   `/dynos-work:start` output. (If the start skill does not echo the
   id, inspect `.dynos/` for the most recent `task-*` directory whose
   `manifest.json` references this residual via the sentinel — but
   prefer reading the id from the start skill's output to avoid
   ambiguity.)
2. Read `.dynos/task-{id}/manifest.json`. Inspect the `stage` field.
3. If `stage` is `"DONE"` or `"FAILED"`, polling is over — proceed to
   step 5.
4. Otherwise, sleep approximately 30 seconds and re-read the manifest.
   Repeat. The lifecycle is monotonic; the manifest will eventually
   reach a terminal stage.
5. If the manifest is missing or unreadable for more than a few
   consecutive polls (suggesting the spawned task crashed before
   writing the manifest), give up polling, treat this as a
   round-trip-missing case, and fall through to step 5 — the fallback
   path will revert the row to `pending`.

The skill prose deliberately uses 30-second polling rather than busy
waiting. The lifecycle stages take seconds to minutes; polling tighter
than every few seconds wastes CPU and risks rate-limiting the
filesystem. Polling looser than a minute slows feedback for the
operator. 30 seconds is the recommended cadence.

### Step 5 — Read the queue and report the round-trip outcome

Once the spawned task is at a terminal stage (or polling has been
abandoned because the manifest is permanently missing), re-read the
queue:

```python
data = lib_residuals.load_queue(lib_residuals.queue_path(root))
row = next((r for r in data["findings"] if r["id"] == residual_id), None)
```

Then dispatch on `row["status"]`:

| Observed status | Meaning | Output |
|---|---|---|
| `"done"` | `cmd_run_audit_finish` saw the sentinel and applied the round-trip update on a successful audit. | `residual {id} → done` |
| `"failed"` | `cmd_run_audit_finish` recorded a third FAILED attempt (`attempts >= 3` after the third failure). | `residual {id} → failed` |
| `"pending"` and `attempts < 3` | The spawned task FAILED but more attempts are allowed; `cmd_run_audit_finish` set `status` back to `"pending"` for the next `run-next` to retry. | `residual {id} → pending` |
| `"in_progress"` | The round-trip update did NOT happen. The orchestrator crashed, the manifest never reached `DONE/FAILED`, or `cmd_run_audit_finish` did not see the sentinel. The attempt is sacrificed. | Revert the row to `"pending"` WITHOUT incrementing `attempts` (call `update_row_status(root, id, "pending")`; do NOT touch `attempts`). Print `residual {id} → pending (round-trip missing, reverted)`. |

Exit 0 in all four cases. The queue is now in a consistent state for
the next `run-next` invocation.

#### Fallback (round-trip missing) — important details

The "still in_progress at terminal" case represents a real failure
mode: a spawned residual task whose lifecycle finished but whose queue
row was never updated. Possible causes include:

- The orchestrator process died between `manifest stage = DONE` and
  the `cmd_run_audit_finish` round-trip write.
- The spawned task wrote a manifest but `cmd_run_audit_finish` did not
  recognise the sentinel (e.g. malformed first line — but
  `extract_residual_id` is anchored, so this is rare).
- Polling was abandoned because the manifest was unreadable for many
  consecutive reads.

In all these cases, the attempt is sacrificed — we do NOT increment
`attempts` further on revert, because the original `update_row_status`
in step 3 already incremented it once. Reverting `status` to
`"pending"` lets the next `run-next` retry. After three sacrificed
attempts the row will not be eligible for selection (`attempts < 3`
guard in `select_next_pending`), and an operator must inspect it
manually.

To revert without touching `attempts`, call:

```python
lib_residuals.update_row_status(root, residual_id, "pending")
```

`update_row_status` is documented to change ONLY `status` (and
`last_attempt_at` on the in_progress transition). It does not modify
`attempts`. The increment that already happened in step 3 stays.

## Output contract summary

| Condition | Output (exact) | Exit |
|---|---|---|
| `list`: queue file missing or `findings == []` | `dynos-work: no residuals queued` | 0 |
| `list`: rows present | one tab-separated line per row (id, status, attempts, source_auditor, title[60], created_at) | 0 |
| `run-next`: queue file missing or `findings == []` or no eligible row | `dynos-work: residual queue empty` | 0 |
| `run-next`: another row already `in_progress` | `dynos-work: residual in_progress — run-next already active` | 0 |
| `run-next`: spawned task → row.status == "done" | `residual {id} → done` | 0 |
| `run-next`: spawned task → row.status == "failed" | `residual {id} → failed` | 0 |
| `run-next`: spawned task → row.status == "pending" and attempts < 3 | `residual {id} → pending` | 0 |
| `run-next`: spawned task → row.status still "in_progress" | revert to `"pending"`, print `residual {id} → pending (round-trip missing, reverted)` | 0 |
| `run-next`: step-3 atomic write failed | error message to stderr | non-zero |

Plain text only. No JSON wrapping. No ANSI colour. The `list` output
must be parseable by `awk '{print $1}'` style scripts (tabs separate
fields).

## When to use

- After a `/dynos-work:audit` run you want to drain newly-surfaced
  proactive residual findings.
- As the body of a periodic loop that calls `run-next` until the
  output is `dynos-work: residual queue empty`.
- To inspect the queue before deciding whether to drain (`list`).

## What NOT to do

- Do NOT spawn auditor or executor agents directly to "fix" a
  residual. Only `/dynos-work:start` is the execution path; it
  enforces the full lifecycle and the audit gate.
- Do NOT skip the polling loop in step 4. Reading `manifest.json`
  once and assuming completion is INCORRECT.
- Do NOT mutate `proactive-findings.json` outside of
  `lib_residuals.update_row_status` (or an equivalent LOCK_EX atomic
  write). Concurrent audits writing to the queue will clobber a
  non-atomic write.
- Do NOT JSON-wrap the stdout output. The output contract is plain
  bare strings; tests (and operators) compare them by exact equality.
