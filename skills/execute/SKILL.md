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

**Inline execution for fast-track tasks:** If `manifest.json` has `"fast_track": true` AND the execution graph has exactly 1 segment, execute the segment **directly** (inline) instead of spawning a subagent. This avoids the ~30K token overhead of agent context setup. However, you MUST still run the router and apply learned agent rules before executing:

1. Run the executor plan router: `python3 "${PLUGIN_HOOKS}/router.py" executor-plan --root . --task-type {task_type} --graph .dynos/task-{id}/execution-graph.json`
2. Write the executor-routing receipt (required for stage transitions).
3. If the plan returns `route_mode: "replace"` or `"alongside"` with a non-null `agent_path`, read the learned agent file and follow its rules during your inline execution.
4. Run `inject-prompt` with your base prompt to get the complete prompt with learned rules and prevention rules. Apply those rules to your own work.
5. Log the routing decision: `{timestamp} [ROUTE] {executor} model={model} route={route_mode} source={route_source}`
6. **Transition the manifest stage `PRE_EXECUTION_SNAPSHOT → EXECUTION`** (mandatory — without this the Step 4 transition to `TEST_EXECUTION` is illegal per `ALLOWED_STAGE_TRANSITIONS`). `transition_task` auto-appends the `[STAGE] → EXECUTION` log line; do not write it manually:

```text
python3 hooks/ctl.py transition .dynos/task-{id} EXECUTION
```

Then read the segment, extract the criteria from `spec.md`, make the code changes yourself, write evidence, and proceed to Step 4. Log: `{timestamp} [INLINE] seg-1 — fast-track inline execution (no subagent spawn)`.

Inline execution does not relax standards. It removes spawn overhead, not rigor.

Skipping the router in inline mode silently ignores learned agents and breaks the self-learning feedback loop.

**Normal execution (fast_track is false or >1 segment):**

Update `manifest.json` stage to `EXECUTION` (transition_task auto-appends the `[STAGE] → EXECUTION` log line):

```text
python3 hooks/ctl.py transition .dynos/task-{id} EXECUTION
```

Read `execution-graph.json`. Before spawning any executor, perform deterministic preflight validation. The validator supports two flags that prevent redundant work:

- `--use-receipt` short-circuits the entire validation when the `plan-validated` receipt's captured artifact hashes (spec.md, plan.md, execution-graph.json) match the current files. Planning already validated these exact artifacts; if nothing has drifted, redoing the work is pure waste.
- `--no-gap` keeps the cheap structural checks but skips `plan_gap_analysis` (an unbounded repo walk).

Default: pass `--use-receipt` first; the validator falls through to a full check if the receipt is stale.

```text
python3 hooks/validate_task_artifacts.py .dynos/task-{id} --use-receipt
```

Then enforce the following checks:

1. `execution-graph.json` must parse as valid JSON.
2. Every segment ID must be unique.
3. Every `depends_on` reference must resolve to an existing segment.
4. The dependency graph must be acyclic.
5. No file may appear in more than one segment's `files_expected`.
6. Every `criteria_id` must map to a real acceptance criterion in `spec.md`.
7. Every acceptance criterion must be covered by at least one segment.
8. If `.dynos/task-{id}/evidence/tdd-tests.md` exists, the execution graph must still cover every criterion represented by the approved tests.

If any preflight validation fails, stop and return to `/dynos-work:start` or `/dynos-work:plan` to repair the plan artifacts.

Do not patch around a broken plan during execution. A bad plan is an upstream failure, not an executor challenge.

After preflight validation, perform the following execution optimizations:

1. **Critical Path Identification:** 
   - Calculate the "Dependency Depth" for every segment (defined as the maximum number of steps required to reach the end of the graph from that segment).
   - Segments with the highest depth are on the **Critical Path**. 
   - **Spawn Priority:** In each parallel batch, prioritize spawning segments with the highest depth first to prevent them from becoming bottlenecks.

2. **Incremental Execution (Incremental Caching):**
   - For each segment, check if an evidence file already exists at `.dynos/task-{id}/evidence/{segment-id}.md` from a previous run.
   - If it exists, compare the current `spec.md`, `plan.md`, and the **modified time** of all files in the segment's `files_expected`. 
   - **Skip Gate:** If the specs haven't changed AND the files haven't been manually edited since the last evidence write, **SKIP the executor spawn** and mark the segment as `CACHED` in the manifest. Reuse the existing evidence.
   - Append to log: `{timestamp} [CACHE] {segment-id} — skipping (inputs unchanged)`.

3. **Progressive Auditing (Pipelining):**
   - Do not wait for the entire graph to finish before auditing.
   - As soon as a segment completes (or is resolved from cache), if it is a high-risk domain (`security`, `db`), immediately spawn its corresponding **Auditor** (following the Skip and Model Policies in `audit` skill) in the background.
   - Append to log: `{timestamp} [PIPE] {auditor-name} — background audit triggered for {segment-id}`.

**Deterministic routing (MANDATORY):** Before spawning any executor, run the router to get a structured spawn plan:

```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/router.py" executor-plan --root . --task-type {task_type} --graph .dynos/task-{id}/execution-graph.json
```

This returns a JSON object with model, route mode, and agent path for each segment.

Log each decision: `{timestamp} [ROUTE] {executor} model={model} route={route_mode} source={route_source}`

**Receipt: executor-routing (MANDATORY):** Immediately after building the executor plan, write the routing receipt:

```bash
python3 -c "
from pathlib import Path
from lib_receipts import receipt_executor_routing
import json
plan = json.loads('''${EXECUTOR_PLAN_JSON}''')
receipt_executor_routing(Path('.dynos/task-{id}'), plan['segments'])
"
```

This receipt is required by `transition_task()` before the task can reach CHECKPOINT_AUDIT. If you skip it, the audit transition will be blocked.

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
5. Logs a `learned_agent_injected` event to `.dynos/events.jsonl`
6. Prints the complete prompt to stdout

**Routing cache (automatic — no extra steps):** `executor-plan` writes its result to `.dynos/task-{id}/router-cache/executor-plan.json` keyed by a fingerprint over every input that drives the plan (graph, policy, effectiveness scores, retrospectives, learned registry, benchmark history, prevention rules). Each `inject-prompt` call checks this cache first; on a fingerprint match it reuses the cached entry instead of re-deriving routing. This guarantees the model the executor was spawned under matches the model the prompt was injected for, even with epsilon-greedy exploration enabled. Cache lookups emit a `router_cache_lookup` event with `status: hit | fingerprint_drift | stale_segment`. Inspect freshness with:

```bash
python3 "${PLUGIN_HOOKS}/router.py" router-cache-status --root . --task-type {task_type} --graph .dynos/task-{id}/execution-graph.json
```

If status is `stale`, re-run `executor-plan` before continuing inject-prompt calls — otherwise per-segment routing will silently fall back to live builds.

**Use the OUTPUT of this command as the prompt for the Agent tool spawn.** Do NOT construct the executor prompt yourself. Do NOT skip this step. If you spawn an executor without running inject-prompt first, the learned agent is silently ignored and the whole self-learning system is broken.

If the injected prompt is weak, strengthen the base prompt before spawning. Do not accept vague executor instructions.

If `inject-prompt` is not available (command not found), fall back to manually reading the `agent_path` file from the executor plan and appending its contents to the prompt. But this should never happen in this repo.

**Model selection:** Pass the `model` field from the executor plan as the model parameter when spawning the agent. If `model` is null, use default (omit the model parameter).

**TDD-First Awareness:** Check if `.dynos/task-{id}/evidence/tdd-tests.md` exists. If it does, include this instruction in the base prompt (before piping through inject-prompt): "A TDD test suite has already been committed. Your implementation must make those tests pass. Do NOT write new tests or modify existing test files."

Spawn the prioritized batch of non-cached executor agents in parallel.

Executor agents by type:
- `ui-executor`, `backend-executor`, `ml-executor`, `db-executor`, `refactor-executor`, `testing-executor`, `integration-executor`, `docs-executor`.

The base prompt for each executor (before inject-prompt) must include:
1. Its specific segment object from `execution-graph.json`
2. The full text of each acceptance criterion referenced by the segment's `criteria_ids` field, extracted from `spec.md`
3. Evidence files from dependency segments: for each segment ID in the executor's `depends_on` list, read `.dynos/task-{id}/evidence/{dependency-segment-id}.md` and include its contents
4. Instruction to write evidence to `.dynos/task-{id}/evidence/{segment-id}.md`

Do NOT pass the full `spec.md` or `plan.md` to executors.

**Receipt: executor-{seg-id} (MANDATORY):** After each executor completes, write its receipt:

```python
from lib_receipts import receipt_executor_done
receipt_executor_done(
    task_dir=Path(".dynos/task-{id}"),
    segment_id="{seg-id}",
    executor_type="{executor from plan}",
    model_used="{model from plan or null}",
    learned_agent_injected={True if route_mode was replace/alongside else False},
    agent_name="{agent_name from plan or None}",
    evidence_path=".dynos/task-{id}/evidence/{seg-id}.md",
    tokens_used={total_tokens from Agent result or None},
)
```

This proves the segment completed with the correct routing. The receipt is checked by `validate-receipts`.

After each batch (or cached resolution) completes, record events and verify:

- **Token capture (executor spawn):** After each executor returns:
  ```bash
  PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/lib_tokens.py" record \
    --task-dir .dynos/task-{id} \
    --agent "{executor_name}-{segment-id}" \
    --model "{model_name}" \
    --input-tokens {input_tokens} \
    --output-tokens {output_tokens} \
    --phase execution \
    --stage "EXECUTION" \
    --type spawn \
    --segment "{segment-id}" \
    --detail "{what the executor implemented}"
  ```
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
- Deterministically verify that only files from the segment's `files_expected` were modified. If available in this repo, use `python3 hooks/ctl.py check-ownership .dynos/task-{id} {segment-id} {files...}`. If extra files changed, fail the segment and route it to repair instead of accepting the evidence.
- Deterministically verify that the segment wrote its evidence file.
- Update `manifest.json` execution_progress.
- Append to log: `{timestamp} [DONE] {segment-id} — complete`.
- Find next unblocked batch (ordered by Depth) and spawn.

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

Repeat until all segments have evidence files.

Append to log:
```
{timestamp} [ADVANCE] EXECUTION → TEST_EXECUTION
```

### Step 4 — Run tests

Update `manifest.json` stage to `TEST_EXECUTION` (transition_task auto-appends the `[STAGE] → TEST_EXECUTION` log line):

```text
python3 hooks/ctl.py transition .dynos/task-{id} TEST_EXECUTION
```

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
