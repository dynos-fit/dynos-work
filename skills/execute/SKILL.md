---
name: execute
description: "Execute the approved plan. Orchestrates execution graph segments through specialized executor agents, including dependency management and error recovery."
---

# dynos-work: Execute Skill

Manages the parallel execution of the implementation plan via the execution graph. Handles dependency ordering, executor spawning, and final integration.

## Ruthlessness Standard

- No silent shortcuts.
- No executor prompt may omit learned rules or prevention rules when they exist.
- A segment is not done because code was written; it is done when the required behavior is proven.
- If verification is weak, the segment is weak.
- If a segment can pass with shallow evidence, the prompt is too soft.
- No executor gets to hide behind “close enough.”

## What you do

### Step 0 — Contract validation

Validate that all required inputs from the start skill are present:

```text
python3 hooks/ctl.py validate-contract --skill execute --task-dir .dynos/task-{id}
```

If validation fails with missing required inputs, print the errors and stop. Do not proceed with execution.

### Step 1 — Review the Plan

Read `spec.md`, `plan.md`, and `execution-graph.json`. Ensure you understand the dependency chain.

### Step 2 — Snapshot the Base

Before any modification starts:
1. Capture the current HEAD SHA.
2. Create a temporary git branch `dynos/task-{id}-snapshot`.

Append to log:
```
{timestamp} [DECISION] snapshot created — branch dynos/task-{id}-snapshot at {head_sha}
```

### Step 3 — Execute segments (Optimized Scheduler)

Run deterministic execute setup first:

```text
python3 hooks/ctl.py run-execute-setup .dynos/task-{id}
```

This command runs execution preflight validation, advances `PRE_EXECUTION_SNAPSHOT -> EXECUTION`, builds the executor plan, and writes the `executor-routing` receipt. Use its JSON output directly.

**Inline vs spawn decision (authoritative):**

The inline branch is permitted ONLY when every cell in row 1 of the table below is satisfied. Any deviation forces the spawn path. Read the executor plan router output first, then look up the row that matches `manifest.fast_track`, the number of segments in `execution-graph.json`, and the segment's `route_mode` + `agent_path` from the router's response.

| fast_track | n_segments | route_mode | agent_path | path_taken |
| --- | --- | --- | --- | --- |
| true | 1 | generic | null | inline (no subagent spawn) |
| true | 1 | replace | any non-null | spawn (inline FORBIDDEN) |
| true | 1 | alongside | any non-null | spawn (inline FORBIDDEN) |
| false OR n_segments > 1 | any | any | any | spawn |

If any segment has route_mode in {replace, alongside} with non-null agent_path, the inline branch is FORBIDDEN — take the spawn path.

**Inline execution procedure (only when the decision table row says `inline`):** Inline execution skips the executor subagent entirely and runs the segment in this thread. This avoids the ~30K token overhead of agent context setup, but it does NOT relax any other rule. Even in the inline branch, you MUST still run the router, write the executor-routing receipt, and apply the prompt-injection/prevention pipeline to your own work:

1. Run the executor plan router: `python3 "${PLUGIN_HOOKS}/router.py" executor-plan --root . --task-type {task_type} --graph .dynos/task-{id}/execution-graph.json`
2. Write the executor-routing receipt (required for stage transitions).
3. Re-verify the decision table row. If the router's response contains any segment with `route_mode` in `{"replace", "alongside"}` with a non-null `agent_path`, STOP — the inline branch is FORBIDDEN for this task. Fall through to the spawn path below.
4. Run `inject-prompt` with your base prompt to get the complete prompt with prevention rules. Apply those rules to your own work.
5. Log the routing decision: `{timestamp} [ROUTE] {executor} model={model} route={route_mode} source={route_source}`
6. **Transition the manifest stage `PRE_EXECUTION_SNAPSHOT → EXECUTION`** (mandatory — without this the Step 4 transition to `TEST_EXECUTION` is illegal per `ALLOWED_STAGE_TRANSITIONS`). `transition_task` auto-appends the `[STAGE] → EXECUTION` log line; do not write it manually:

```text
python3 hooks/ctl.py transition .dynos/task-{id} EXECUTION
```

Then read the segment, extract the criteria from `spec.md`, make the code changes yourself, write evidence, and proceed to Step 4. Log: `{timestamp} [INLINE] seg-1 — fast-track inline execution (no subagent spawn)`.

Inline execution does not relax standards. It removes spawn overhead, not rigor.

Skipping the router in inline mode silently ignores learned agents and breaks the self-learning feedback loop.

**Normal execution (fast_track is false or >1 segment):**

`run-execute-setup` already performed deterministic preflight validation and stage advancement. If it failed, repair the plan first; do not patch around a broken plan during execution.

Immediately compute the authoritative execution schedule:

```bash
python3 "${PLUGIN_HOOKS}/ctl.py" run-execution-batch-plan .dynos/task-{id}
```

This command is authoritative for:
- segment state: `completed | cached | pending`
- cache eligibility and cache rejection reasons
- dependency depth / critical path
- next runnable batch and remaining batches

Do NOT re-derive dependency depth, cache reuse, or runnable batches in prompt logic. Consume the JSON output and follow it.

If `cached_segments` is non-empty, treat those segments as already satisfied and reuse their evidence files. Append to log: `{timestamp} [CACHE] {segment-id} — skipping (inputs unchanged)`.

If `next_batch` is empty while `pending_segments` is non-empty, stop and fail the task as blocked. That means dependency resolution or receipt state is inconsistent; do not guess.

Progressive auditing remains allowed, but only after a segment is deterministically known complete or cached.

**Deterministic routing (MANDATORY):** Use the `segments` payload returned by `run-execute-setup` as the authoritative executor plan. Do not rebuild routing in prompt logic. The `executor-routing` receipt has already been written.

Do NOT read project_rules.md tables manually. The router handles model policy, agent routing, and security floors deterministically.

**Learned Agent Injection (MANDATORY — NOT OPTIONAL):** For every segment, you MUST build the executor prompt using the deterministic prompt builder. This is not a suggestion. This is an enforcement gate. For each segment in the executor plan:

```bash
echo "{your base prompt for this segment}" | PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 hooks/router.py inject-prompt --root . --task-type {task_type} --graph .dynos/task-{id}/execution-graph.json --segment-id {seg-id}
```

This command:
1. Reads the base prompt from stdin
2. Looks up the routing decision for this segment (model, route_mode, agent_path)
3. If `route_mode` is `replace` or `alongside`, reads the learned agent `.md` file and appends its rules to the prompt
4. Appends prevention rules from the project's history
5. Logs a `learned_agent_applied` event to `.dynos/events.jsonl`
6. Prints the complete prompt to stdout

**Routing cache (automatic — no extra steps):** `executor-plan` writes its result to `.dynos/task-{id}/router-cache/executor-plan.json` keyed by a fingerprint over every input that drives the plan (graph, policy, effectiveness scores, retrospectives, learned registry, benchmark history, prevention rules). Each `inject-prompt` call checks this cache first; on a fingerprint match it reuses the cached entry instead of re-deriving routing. This guarantees the model the executor was spawned under matches the model the prompt was injected for, even with epsilon-greedy exploration enabled. Cache lookups emit a `router_cache_lookup` event with `status: hit | fingerprint_drift | stale_segment`. Inspect freshness with:

```bash
python3 "${PLUGIN_HOOKS}/router.py" router-cache-status --root . --task-type {task_type} --graph .dynos/task-{id}/execution-graph.json
```

If status is `stale`, re-run `executor-plan` before continuing inject-prompt calls — otherwise per-segment routing will silently fall back to live builds.

**Use the OUTPUT of this command as the prompt for the Agent tool spawn.** Do NOT construct the executor prompt yourself. Do NOT skip this step. If you spawn an executor without running inject-prompt first, the learned agent is silently ignored and the whole self-learning system is broken.

**Sidecar capture (MANDATORY).** `cmd_inject_prompt` writes the SHA-256 of the exact bytes it printed to stdout to `.dynos/task-{id}/receipts/_injected-prompts/{seg-id}.sha256` (single-line lowercase hex digest, no trailing newline) plus a companion `.txt` of the same bytes. Both files are written atomically via tempfile + `os.replace`, so a retry overwrites cleanly. Immediately after `inject-prompt` returns, read the sidecar:

```bash
INJECTED_PROMPT_SHA256=$(cat .dynos/task-{id}/receipts/_injected-prompts/{seg-id}.sha256)
```

You MUST pass this captured digest to `receipt_executor_done(...)` below as `injected_prompt_sha256`. The receipt writer asserts the same sidecar exists and matches; a mismatch raises `ValueError` with the literal substrings `injected_prompt_sha256 sidecar missing` or `injected_prompt_sha256 mismatch`.

If the injected prompt is weak, strengthen the base prompt before spawning. Do not accept vague executor instructions.

If `inject-prompt` is not available (command not found), fall back to manually reading the `agent_path` file from the executor plan and appending its contents to the prompt. But this should never happen in this repo.

**Model selection:** Pass the `model` field from the executor plan as the model parameter when spawning the agent. If `model` is null, use default (omit the model parameter).

**TDD-First Awareness:** Check if `.dynos/task-{id}/evidence/tdd-tests.md` exists. If it does, include this instruction in the base prompt (before piping through inject-prompt): "A TDD test suite has already been committed. Your implementation must make those tests pass. Do NOT write new tests or modify existing test files."

Spawn the `next_batch` executor agents in parallel.

Executor agents by type:
- `ui-executor`, `backend-executor`, `ml-executor`, `db-executor`, `refactor-executor`, `testing-executor`, `integration-executor`, `docs-executor`.

The base prompt for each executor (before inject-prompt) must include:
1. Its specific segment object from `execution-graph.json`
2. The full text of each acceptance criterion referenced by the segment's `criteria_ids` field, extracted from `spec.md`
3. Evidence files from dependency segments: for each segment ID in the executor's `depends_on` list, read `.dynos/task-{id}/evidence/{dependency-segment-id}.md` and include its contents
4. Instruction to write evidence to `.dynos/task-{id}/evidence/{segment-id}.md`

Do NOT pass the full `spec.md` or `plan.md` to executors.

**Segment finalization (MANDATORY):** After each executor completes, finalize the segment through ctl instead of hand-writing receipt / ownership / manifest logic:

```bash
python3 "${PLUGIN_HOOKS}/ctl.py" run-execution-segment-done \
  .dynos/task-{id} \
  "{seg-id}" \
  --injected-prompt-sha256 "{INJECTED_PROMPT_SHA256 captured from sidecar}" \
  --model "{model from plan or null}" \
  --agent-name "{agent_name from plan or None}" \
  --executor-type "{executor from plan}" \
  --evidence-path ".dynos/task-{id}/evidence/{seg-id}.md" \
  --tokens-used {total_tokens from Agent result or None}
```

This command deterministically:
- verifies segment ownership against `execution-graph.json`
- verifies the evidence file exists
- writes `receipt_executor_done(...)`
- updates `manifest.json.execution_progress` from deterministic execution state

After each batch (or cached resolution) completes, record events and verify:

- **Token capture (executor spawn):** automatic. The `SubagentStop` hook
  (wired in `hooks.json`) parses the subagent's transcript and writes the
  spawn record to the active task's `token-usage.json` via
  `hooks/lib_tokens_hook.py`. Do NOT also emit a manual `--type spawn`
  `lib_tokens.py record` call here — it would double-count the tokens.
  (The `--type deterministic` records below are NOT covered by SubagentStop
  and remain the orchestrator's responsibility.)
- **Token capture (deterministic checks):** After file ownership and evidence verification:
  ```bash
  PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/lib_tokens.py" record \
    --task-dir .dynos/task-{id} \
    --agent "ctl-check-ownership" \
    --model "none" \
    --input-tokens 0 \
    --output-tokens 0 \
    --phase execution \
    --stage "EXECUTION" \
    --type deterministic \
    --segment "{segment-id}" \
    --detail "Verified file ownership and evidence — {pass|fail}"
  ```
- Also record the **router decision** before each batch:
  ```bash
  PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/lib_tokens.py" record \
    --task-dir .dynos/task-{id} \
    --agent "router-executor-plan" \
    --model "none" \
    --input-tokens 0 \
    --output-tokens 0 \
    --phase execution \
    --stage "EXECUTION" \
    --type deterministic \
    --detail "Router: {executor}={model} route={route_mode}"
  ```
- Use `run-execution-segment-done` as the authoritative completion gate.
- Append to log: `{timestamp} [DONE] {segment-id} — complete`.
- Re-run `run-execution-batch-plan` to get the next authoritative batch.

**For inline fast-track execution** (no subagent spawn), record with `--type inline`:
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/lib_tokens.py" record \
  --task-dir .dynos/task-{id} \
  --agent "inline-executor" \
  --model "none" \
  --input-tokens 0 \
  --output-tokens 0 \
  --phase execution \
  --stage "EXECUTION" \
  --type inline \
  --segment "seg-1" \
  --detail "Fast-track inline execution (no subagent spawn)"
```

**Test execution** (Step 4): Record test runner events with `--phase execution --stage TEST_EXECUTION`.

When `run-execution-batch-plan` reports no pending segments, advance through ctl:

```bash
python3 "${PLUGIN_HOOKS}/ctl.py" run-execution-finish .dynos/task-{id}
```

This command refuses the transition if any segment is still pending. Do not manually decide that execution is complete.

### Step 4 — Run tests

Read `plan.md` Test Strategy. Run the specified tests. Use incremental testing if the framework supports it (e.g. `jest --onlyChanged`).

If an approved TDD suite exists, run those tests first as the blocking contract suite. If they pass, run any broader regression suite required by `plan.md`.

If tests pass:
- Append to log: `{timestamp} [DONE] tests — passed`
- Continue to Audit

If tests fail:
- Append to log: `{timestamp} [FAIL] tests — failed. Repairs required.`
- Update `manifest.json` stage to `REPAIR_PLANNING` (legal per `ALLOWED_STAGE_TRANSITIONS["TEST_EXECUTION"]`; the older single `REPAIR` stage no longer exists)
- Hand off to `/dynos-work:audit` — its Step 4 owns the two-phase repair loop. There is no standalone `repair` skill.

### Step 5 — Verify completion

After successfully completing execution and tests (and any repairs triggered by tests or audit), verify all referenced `criteria_ids` from `execution-graph.json` are represented in the finalized code. 

Deterministically verify before completion:
- Every segment has an evidence file or a valid cached status.
- Every approved test file still exists unless the plan explicitly superseded it.
- No segment modified files outside its declared ownership without corresponding repair evidence.

Append to log:
```
{timestamp} [DONE] execute — all segments complete and tested
```

### Handoff — Write handoff record

After Step 5, write `.dynos/task-{id}/handoff-execute-audit.json`:

```json
{
  "from_skill": "execute",
  "to_skill": "audit",
  "handoff_at": "{ISO timestamp}",
  "contract_version": "1.0.0",
  "manifest_stage": "{current stage}"
}
```

## Hard Rules
- **No speculative implementation:** Executors must stay strictly within their segment's `files_expected` and `criteria_ids`.
- **Atomic evidence:** Evidence files must be written only after the segment's implementation is complete.
- **Dependency discipline:** Never spawn an executor before its dependency evidence files are available.
- **Caching Discipline:** Never skip a segment if its `files_expected` have any uncommitted changes not reflected in the existing evidence.
- **Ownership discipline:** A segment that edits files outside `files_expected` is invalid until repaired and re-evidenced.
