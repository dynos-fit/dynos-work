---
name: execute
description: "Internal dynos-work skill. Orchestrates execution graph segments through specialized executor agents, including dependency management and error recovery."
---

# dynos-work: Execute Skill

Manages the parallel execution of the implementation plan via the execution graph. Handles dependency ordering, executor spawning, and final integration.

## What you do

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

Update `manifest.json` stage to `EXECUTION`. Append to log:
```
{timestamp} [STAGE] → EXECUTION
```

Read `execution-graph.json`. Perform the following pre-execution optimizations:

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

**Model Policy lookup:** Before spawning executors (for non-cached segments), read `classification.type` from `manifest.json` -- this is the task's `task_type`. Then attempt to read the `## Model Policy` table from `dynos_patterns.md` in the project memory directory. Match against (role, `task_type`).

**Agent Routing lookup:** For non-cached segments, attempt to read the `## Agent Routing` table. If a learned agent is available and has a higher composite score, use it and log: `{timestamp} [ROUTE] {executor-name} using learned:{learned-agent-name} (composite: {score})`.

Spawn the prioritized batch of non-cached executor agents in parallel.

Executor agents by type:
- `ui-executor`, `backend-executor`, `ml-executor`, `db-executor`, `refactor-executor`, `testing-executor`, `integration-executor`.

Each executor receives:
1. Its specific segment object from `execution-graph.json`
2. The full text of each acceptance criterion referenced by the segment's `criteria_ids` field, extracted from `spec.md`
3. Evidence files from dependency segments: for each segment ID in the executor's `depends_on` list, read `.dynos/task-{id}/evidence/{dependency-segment-id}.md` and include its contents
4. Instruction to write evidence to `.dynos/task-{id}/evidence/{segment-id}.md`
5. **Prevention rules:** If `dynos_patterns.md` exists, read its `## Prevention Rules` section. Filter to rows where the `Executor` column matches the executor type being spawned. Include matching rules in the executor's spawn instructions.
6. **Reference Implementation (Gold Standard):** If `dynos_patterns.md` exists, read its `## Gold Standard Instances` table. If one or more tasks appear with a `Type` matching the current task's `task_type`, provide the most recent matching `Task ID` to the executor as a reference. Include this block:
   ```
   ## Reference Implementation (Gold Standard)
   You may read relevant files from `.dynos/{id}/` (spec.md, plan.md) and the project's source tree matching that task's modified files.
   ```

Do NOT pass the full `spec.md` or `plan.md` to executors.

After each batch (or cached resolution) completes:
- Update `manifest.json` execution_progress.
- Append to log: `{timestamp} [DONE] {segment-id} — complete`.
- Find next unblocked batch (ordered by Depth) and spawn.

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

Read `plan.md` Test Strategy. Run the specified tests. Use incremental testing if the framework supports it (e.g. `jest --onlyChanged`).

If tests pass:
- Append to log: `{timestamp} [DONE] tests — passed`
- Continue to Audit

If tests fail:
- Append to log: `{timestamp} [FAIL] tests — failed. Repairs required.`
- Update `manifest.json` stage to `REPAIR`
- Trigger the `repair` skill

### Step 5 — Verify completion

After successfully completing execution and tests (and any repairs triggered by tests or audit), verify all referenced `criteria_ids` from `execution-graph.json` are represented in the finalized code. 

Append to log:
```
{timestamp} [DONE] execute — all segments complete and tested
```

## Hard Rules
- **No speculative implementation:** Executors must stay strictly within their segment's `files_expected` and `criteria_ids`.
- **Atomic evidence:** Evidence files must be written only after the segment's implementation is complete.
- **Dependency discipline:** Never spawn an executor before its dependency evidence files are available.
- **Caching Discipline:** Never skip a segment if its `files_expected` have any uncommitted changes not reflected in the existing evidence.
