---
name: audit
description: "Power user: Run checkpoint audit, repair any findings, then reach DONE — all in one shot. Use after /dynos-work:execute."
---

# dynos-work: Audit

Runs the full audit-to-done pipeline: audit → repair loop → DONE.

## What you do

### Step 1 — Find active task

Find the most recent active task in `.dynos/`. Read `manifest.json`.

Verify stage is `CHECKPOINT_AUDIT`. If not, print the current stage and what command to run instead.

### Step 2 — Diff scope

Run `git diff --name-only {snapshot.head_sha}` to get all files changed by this task. Pass this list to every auditor. Auditors only inspect these files.

If no snapshot exists (standalone audit), use `git diff --name-only HEAD`.

### Step 3 — Audit (conditional auditor spawning with skip optimization)

Update `manifest.json` stage to `CHECKPOINT_AUDIT`.

**Determine which auditors to spawn (DETERMINISTIC ROUTER):**

Read `manifest.json` for `classification` and `fast_track`. Then run the deterministic router:

```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/dynorouter.py" audit-plan --root . --task-type {task_type} --domains {comma-separated-domains} [--fast-track]
```

This returns a JSON plan with each auditor's action (spawn/skip), model, and route mode. **Use this plan directly.** Do not re-derive model, skip, or routing decisions from markdown tables.

For each auditor in the plan:
- If `action: "skip"`: log `{timestamp} [SKIP] {name} — {reason}` and do not spawn
- If `action: "spawn"`: spawn with the specified `model` (null = default), using `agent_path` if `route_mode` is "learned" or "alongside"
- Log: `{timestamp} [ROUTE] {name} model={model} route={route_mode} source={route_source}`

The router handles fast-track reduction, skip policy, model policy, security floor enforcement, and learned agent routing in deterministic code. No prompt interpretation needed for these decisions.

**Normal mode (fast_track is false or absent):**

**Always spawn (universal, skip-exempt auditors):**
- `spec-completion-auditor`
- `security-auditor`
- `code-quality-auditor`

**Always spawn (skip-eligible universal auditor):**
- `dead-code-auditor`

**Conditionally spawn (skip-eligible):**
- `ui-auditor` — spawn only if classification `domains` includes `"ui"` OR any segment in `execution-graph.json` has `executor` value `"ui-executor"`
- `db-schema-auditor` — spawn only if classification `domains` includes `"db"` OR any segment in `execution-graph.json` has `executor` value `"db-executor"`

**When classification is null or missing:** spawn only the 4 universal auditors. Skip `ui-auditor` and `db-schema-auditor`.

**Auditor skip on zero-finding streak (Skip Policy):**

Before spawning, check whether any skip-eligible auditors should be skipped based on their zero-finding streak from prior tasks.

1. Find the most recent completed task directory (not the current task). Look for `.dynos/task-*/task-retrospective.json` files, sort by task ID descending, and select the first one that is not the current task.
2. Read the `auditor_zero_finding_streaks` field (object mapping auditor name to streak count). If the field is missing, the file does not exist, or the file contains the old single-integer `auditor_zero_finding_streak` format instead, treat all streaks as `0` (skip no one).
4. **Determine skip threshold from Skip Policy table:** Read `dynos_patterns.md` from the project memory directory (`~/.claude/projects/<project>/memory/`, where `<project>` is derived from the cwd by replacing `/` with `-`). Look for a markdown table under the heading `## Skip Policy`. The table has columns `| Auditor | Skip Threshold | Confidence |`. For each skip-eligible auditor, use its row's threshold value. If `dynos_patterns.md` is missing, unreadable, malformed, has no `## Skip Policy` section, or has no row for a given auditor (cold-start), fall back to a default threshold of `3`. Append to log when falling back:
   ```
   {timestamp} [WARN] policy table missing/corrupt -- using defaults
   ```
   Log this warning at most once per audit run, not per auditor.
5. **Dynamic Rigor Scaling (Low Risk):** If the task `risk_level` (from `manifest.json`) is `"low"` AND a skip-eligible auditor's `Confidence` in the Skip Policy table is `> 0.9`, **double** its effective skip threshold for the current run. This rewards high-performing, stable agents by reducing their oversight on low-stakes tasks. Log:
   ```
   {timestamp} [RIGOR] aggressive skip enabled for {auditor-name} (low risk + high confidence)
   ```
6. For each skip-eligible auditor (`dead-code-auditor`, `ui-auditor`, `db-schema-auditor`): if its streak value in `auditor_zero_finding_streaks` is `>=` the resolved (and potentially scaled) skip threshold for that auditor, do not spawn it. Append to log:
   ```
   {timestamp} [SKIP] {auditor-name} — zero-finding streak {N} (threshold {T})
   ```
7. Skip-exempt auditors (`security-auditor`, `spec-completion-auditor`, `code-quality-auditor`) are never skipped regardless of streak or rigor.

**Ensemble Voting for High-Risk Audits (Model Optimization):**

For auditors categorized as **High-Risk** (`security-auditor` and `db-schema-auditor`), use the ensemble voting strategy to minimize costs while maintaining high security:

1.  **Initial Pass (Voting):** Do not spawn the recommended model (Opus) immediately. Instead, spawn **two (2) cheaper models** in parallel: one `Haiku` and one `Sonnet`.
2.  **Agreement Check:**
    *   If **both** models return **Zero Findings**: The audit is considered a **PASS**. Record the success and move to the next stage. Log:
        ```
        {timestamp} [VOTE] {auditor-name} — PASS (Haiku + Sonnet agree)
        ```
    *   If **either** model returns **One or More Findings** (even if they disagree on the specifics): Immediately discard the results of the voting pass and **Escalate** to the authoritative model (`Opus`). Log:
        ```
        {timestamp} [VOTE] {auditor-name} — disagreement/findings detected. Escalating to Opus for authoritative audit...
        ```
3.  **Authoritative Pass (Escalated):** Spawn the `security-auditor` or `db-schema-auditor` using `model: "opus"`. This result is final and binding. This ensures that we only pay the "Opus tax" when there is actual complexity or a potential issue to investigate.

**Visual Audit Pass (Multi-Modal Verification):**

For tasks where `domains` includes `"ui"`, perform a **Visual Audit** in addition to the standard code-based audits:

1. **Environmental Setup:** Ensure the dev server is running (e.g., `npm run dev`). 
2. **Screenshot Capture:** Use the **browser subagent** to navigate to the modified screen(s) and take a high-resolution screenshot. 
3. **Visual Evaluation:** Use a **multi-modal model (Claude 3.5 Sonnet)** to audit the screenshot against the `Design Decisions` defined in the planning phase.
   - Look for: visual regressions, layout shift, poor color contrast, and incorrect copy.
   - Verify that the rendered UI matches the "Design Options" selected by the user.
4. **Report findings:** Any visual findings are added to the main audit finding set with the category `vision-finding`. 
5. **Log result:** `{timestamp} [VISION] UI audit complete -- {N} visual bugs found`.

**Auditor model selection (Model Policy):**

Before spawning each auditor, determine which model to use:

1. Read `dynos_patterns.md` from the project memory directory (same path as above). Look for a markdown table under the heading `## Model Policy`. The table has columns `| Role | Task Type | Recommended Model |`. Match each auditor name against the `Role` column and the current task's `task_type` against the `Task Type` column. Read `classification.type` from manifest — this is the task's `task_type`. Use the `Recommended Model` value from the matching row.
2. If `dynos_patterns.md` is missing, unreadable, malformed, has no `## Model Policy` section, or has no matching row for a given auditor and task type, fall back to the default model. Log once per audit run if the policy table is unavailable (reuse the same `[WARN]` log from skip policy if already emitted; do not duplicate).
3. **Ensemble Voting Trigger:** If the auditor is `security-auditor` or `db-schema-auditor`, follow the **Ensemble Voting** logic described above. This overrides the standard Model Policy selection.
4. **Security floor enforcement:** For any *final* or *non-voting* spawn of `security-auditor`, the model must never be below `opus`.
5. For each auditor, append to log:
   ```
   {timestamp} [MODEL] {auditor-name} using {model} (source: {policy|default|vote})
   ```
   Use `policy` when the model came from a matching Model Policy row. Use `default` when falling back. Use `vote` when the ensemble voting strategy was used.

**Agent Routing (learned auditor selection):**

After resolving skip, model, and voting policies, determine whether each auditor should use a generic prompt, a learned prompt, or both:

1. Prefer the live registry resolver: `python3 hooks/dynoroute.py {auditor-name} {task_type} --root .`. Match the current auditor name and `classification.type` from `manifest.json`. If the resolver returns `source = learned:{agent-name}` with `route_allowed = true`, use that entry's `path`, `mode`, and `composite`. If it returns `generic`, use the built-in auditor. The markdown `## Agent Routing` table in `dynos_patterns.md` is explanatory memory only and should not override the live registry.
2. **Security-auditor replace protection:** The `security-auditor` must NEVER be replaced by a learned auditor. Even if a learned security auditor exists in the Agent Routing table with mode=replace, always run the generic `security-auditor`. A learned security auditor may only run in alongside mode (supplementary, never replacing). This matches the monotonicity constraint (security-auditor never downgraded). If a replace entry is found for `security-auditor`, override it to alongside and log: `{timestamp} [WARN] security-auditor replace blocked -- forced to alongside (monotonicity)`.
3. **Voting Pass Restriction:** Learned auditors never partake in the initial Ensemble Voting pass. Only the generic `security-auditor` or `db-schema-auditor` definitions are used for the voting pass to ensure a consistent baseline.
4. For each auditor that was not skipped, check its row in the Agent Routing table:
   - **No entry:** Use the generic auditor. Append to log:
     ```
     {timestamp} [ROUTE] {auditor-name} using generic (mode: default)
     ```
   - **Mode = `replace`:** Use only the learned auditor prompt (from the `Agent Path` column). Do not spawn the generic auditor for this role. Append to log:
     ```
     {timestamp} [ROUTE] {auditor-name} using learned:{agent-name} (mode: replace, composite: {score})
     ```
   - **Mode = `alongside`:** Spawn BOTH the generic auditor AND the learned auditor for this role. Both run in parallel and both produce audit reports. Append to log:
     ```
     {timestamp} [ROUTE] {auditor-name} using alongside (mode: alongside, composite: {score})
     ```
5. **Finding deduplication in alongside mode:** When both generic and learned auditors produce findings for the same role, deduplicate before counting. Two findings are considered the same if they share the same dedup key: `{file}:{line}:{category}` (where `line` is the finding's reported line from the `location` field, and `category` is the finding ID prefix). If both auditors report the same finding, count it once. Retain the version from the learned auditor (preference for learned). The deduplicated finding set is what feeds into the repair pipeline and retrospective counts.
6. **Alongside promotion tracking:** For each alongside auditor, record whether the learned auditor's findings are a superset of the generic auditor's findings (using dedup keys). The learn step reads this data to decide whether to promote from alongside to replace after 3 tasks.

Append to log:
```
{timestamp} [STAGE] → CHECKPOINT_AUDIT
{timestamp} [SPAWN] {N} auditors in parallel ({list of names})
```

Spawn the determined auditors simultaneously, passing the resolved model for each auditor in the subagent spawn configuration. For alongside-mode auditors, this means two spawns for that role (generic + learned), both counted in {N}.

Each writes its report to `.dynos/task-{id}/audit-reports/{auditor}-checkpoint-{timestamp}.json`.

**Token capture after auditor spawns:** After each auditor subagent spawn completes (including all voting/escalated passes), record the token count from the Agent tool result (the `total_tokens` value from the usage summary returned when the subagent completes). Store as `{auditor-name: token_count}`. If the Agent tool result does not include token usage, record `null` for that auditor and exclude it from any subsequent sum. For ensemble voting, the `token_count` is the **sum** of all voting and escalation spawns for that auditor.

**Eager two-phase repair trigger:**

Do not wait for all auditors to complete before proceeding. Instead, monitor auditor results as they arrive:

1. When the first auditor returns with one or more blocking findings, immediately proceed to Step 4 phase 1 repair with those findings. Do not wait for remaining auditors.
2. **Short-circuit on critical spec failure:** If `spec-completion-auditor` returns findings with severity `critical`, immediately proceed to Step 4 phase 1 repair with those critical findings, even if other auditors have not returned yet. This is a specific instance of the eager repair trigger.
3. If no auditor returns blocking findings after all auditors complete, append to log and proceed to Step 5:
   ```
   {timestamp} [DONE] audit complete — no blocking findings
   ```

Record which auditors are still running at the time phase 1 repair begins. Their results are **late findings** and feed into phase 2 (see Step 4).

### Step 4 — Two-phase repair loop

This step runs only when blocking findings exist. It uses a two-phase model: phase 1 repairs early findings (from the first auditor(s) to return), phase 2 repairs late findings (from auditors that completed after phase 1 began) plus any new findings from phase 1 re-audit.

**If no blocking findings from any auditor:** skip this step entirely. Proceed to Step 5.

**Phase 1 — Early findings repair:**

Collect all blocking findings available at the time of the eager trigger (Step 3). These are the phase 1 findings.

Update stage to `REPAIR_PLANNING`. Append to log:
```
{timestamp} [REPAIR-P1] {N} findings — {list of finding IDs}
{timestamp} [STAGE] → REPAIR_PLANNING
```

Spawn `repair-coordinator` agent with instruction: "Read the provided audit reports. Produce a repair plan for the given findings. Assign each finding to an executor. For each repair task, list the files that will be modified. Write to `.dynos/task-{id}/repair-log.json`."

**Token capture:** After `repair-coordinator` completes, record the token count from the Agent tool result (the `total_tokens` value from the usage summary returned when the subagent completes) as `{repair-coordinator: token_count}`. If unavailable, record `null`.

Wait for completion. Update stage to `REPAIR_EXECUTION`. Append to log:
```
{timestamp} [STAGE] → REPAIR_EXECUTION
```

**Parallel batch spawning:**

Read `repair-log.json` batches. Spawn batches where `parallel: true` concurrently. Batches where `parallel: false` are serialized (wait for preceding batches with shared files to complete first).

Append to log when spawning parallel batches:
```
{timestamp} [SPAWN] repair batch-1, batch-3 in parallel
{timestamp} [SPAWN] repair batch-2 — waiting, shares files with batch-1
```

For each batch, spawn executor agents as assigned in `repair-log.json`:
- `ui-executor`, `backend-executor`, `ml-executor`, `db-executor`, `refactor-executor`, `testing-executor`, `integration-executor`

**Model escalation:** For each finding where `retry_count >= 2`, spawn the executor with `model_override: "opus"` in the subagent spawn configuration. Findings at retry 0 or 1 use the default model. This is a per-finding model routing change, not an instruction-text change. Two findings in the same batch may have different retry counts and therefore different model assignments. Append to log:
```
{timestamp} [ESCALATE] finding {finding-id} retry {N} -> opus
```

Each executor receives: the specific finding, the file(s) to fix, and the relevant acceptance criteria text from `spec.md`.

**Token capture:** After each executor subagent spawn completes, record the token count from the Agent tool result (the `total_tokens` value from the usage summary returned when the subagent completes) as `{executor-name: token_count}`. If multiple spawns use the same executor name, sum their token counts. If unavailable for a spawn, record `null` for that spawn and exclude it from the sum.

After all batches complete, append to log:
```
{timestamp} [DONE] repair-execution-p1 — all phase 1 fixes applied
```

**Late-finding conflict resolution:**

While phase 1 repair is running, remaining auditors from Step 3 may complete. Collect their results:

1. If a late auditor reports findings in files currently being repaired by phase 1, those findings are **not** applied to phase 1. They are queued for phase 2.
2. If a late auditor reports findings in files **not** being repaired by phase 1, those findings are also queued for phase 2.
3. All late findings go to phase 2, regardless of file overlap.

**Phase 1 re-audit (domain-aware, incremental scope):**

Before spawning re-audit auditors, determine which files were modified during phase 1 repair:
1. Read `repair-log.json` task entries to collect the list of files each executor was assigned to modify.
2. Additionally run `git diff --name-only` against the pre-repair commit (the commit hash recorded before repair execution began) to catch any files modified that were not explicitly listed.
3. Union these two sets into the **repair-modified file list**.

Scope re-audit auditors to only the repair-modified file list -- NOT the full original diff scope from Step 2. This avoids redundantly re-auditing files that were already clean.

**Exception:** `spec-completion-auditor` always receives the **original full diff scope** (from Step 2), since spec completion checks overall requirement coverage across the entire task, not file-level issues.

All re-audit auditors still receive the previous audit reports for context (so they know what was originally found and what to verify as fixed).

Spawn only:
- `spec-completion-auditor` (always, full original diff scope)
- `security-auditor` (always, repair-modified files only)
- Any auditor whose findings appear as tasks in the current phase's `repair-log.json` (repair-modified files only)
- Skip auditors whose domain was not touched by the repair (e.g., if only backend files were repaired, skip `ui-auditor` even if it ran in the initial audit)

**Token capture:** After each re-audit auditor spawn completes, record the token count from the Agent tool result (the `total_tokens` value from the usage summary returned when the subagent completes) and add to the running total for that auditor. If unavailable, record `null`.

Wait for results. Any new findings from re-audit are added to the phase 2 queue.

**Phase 2 — Late findings and re-audit findings repair:**

Collect all queued findings: late auditor findings plus any new findings from phase 1 re-audit. If no queued findings exist, skip phase 2.

If queued findings exist, append to log:
```
{timestamp} [REPAIR-P2] {N} findings — {list of finding IDs}
```

Spawn `repair-coordinator` with the phase 2 findings. The coordinator writes to `repair-log.json` with an incremented `repair_cycle` value (phase 2 overwrites with the new cycle).

**Token capture:** After `repair-coordinator` completes, record the token count from the Agent tool result (the `total_tokens` value from the usage summary returned when the subagent completes) and add to the running total for `repair-coordinator`. If unavailable, record `null`.

Apply the same parallel batch spawning and model escalation logic as phase 1 (including token capture via the `total_tokens` value from the Agent tool result after each executor spawn).

**Retry count continuity:** Retry counts are continuous across phases. A finding first repaired in phase 1 at retry 1 and re-found in phase 2 is at retry 2, not retry 0. The `max_retries` limit (3) applies across both phases combined for a given finding.

After phase 2 repairs complete, run a phase 2 re-audit using the same domain-aware incremental scope logic as phase 1 re-audit (scoped to phase 2 repair-modified files).

- If all clear after phase 2 re-audit: append `{timestamp} [DONE] repair-execution-p2 — all phase 2 fixes applied` to log. Proceed to Step 5.
- If new findings remain: increment `retry_count` for each finding. If any finding has exceeded 3 retries, set stage to `FAILED`, append `[FAILED] max retries exceeded for: {finding-ids}`, and stop. Otherwise loop back into another repair cycle (continuing phase 2).

**Degenerate cases:**

- If no late auditors exist (all auditors completed before phase 1 began, or all skippable auditors were skipped), phase 2 only contains re-audit findings (if any). If no re-audit findings exist, phase 2 is skipped entirely.
- If no blocking findings exist from any auditor, both phases are skipped and behavior is identical to the previous pipeline (proceed directly to Step 5).

### Step 5 — Gate to DONE

Read all audit reports. Write `audit-summary.json`.

**Reflect (inline -- no subagent):**

Before writing `completion.json`, generate `task-retrospective.json` in the task directory. This runs inline as part of the DONE gate.

1. Scan `.dynos/task-{id}/audit-reports/*.json`. For each file, attempt to parse it as JSON. If parsing fails or the file is unreadable, skip it silently.
2. From successfully parsed audit reports, extract:
   - **Finding counts by auditor:** For each report, count the number of entries in the `findings` array. Key by `auditor_name`.
   - **Finding counts by category:** For each finding, use the finding ID prefix (the part before the hyphen-number, e.g. `sec` from `sec-001`) as the category. Count findings per category.
3. If `.dynos/task-{id}/repair-log.json` exists and is valid JSON:
   - **Executor repair frequency:** Iterate all tasks across all batches. Count how many repair tasks were assigned to each `assigned_executor` value.
   - **Repair cycle count:** Read the `repair_cycle` field (integer).
   - If the file is missing or malformed, set executor repair frequency to `{}` and repair cycle count to `0`.
4. Scan `.dynos/task-{id}/execution-log.md`. Count lines matching the pattern `[HUMAN] SPEC_REVIEW`. This is the **spec review iteration count**. If the file is missing, set to `0`.
5. Read `manifest.json`. Flatten the `classification` object into scalar fields: `task_type` (string), `task_domains` (comma-separated string, e.g. "ui,backend"), `task_risk_level` (string). Set **task outcome** to `DONE` (this gate only runs for successful completion).
6. Compute token usage and model tracking fields:
   - `token_usage_by_agent`: Collect all per-agent token counts recorded during Steps 3 and 4 (auditor spawns, repair-coordinator spawns, executor spawns, re-audit spawns). Produce an object mapping agent name to total token count. If a given agent's token count was recorded as `null` (metadata unavailable), include it in the object with value `null`.
   - `total_token_usage`: Sum all non-null values in `token_usage_by_agent`. If all values are `null`, set to `0`.
   - `model_used_by_agent`: Collect the actual model used by each subagent spawn during Steps 3 and 4. Produce an object mapping agent name to model name string. This records the actual model used at spawn time, not the recommended model from policy. If the model information is unavailable from spawn metadata, record `null` for that agent.
   - `agent_source`: For each auditor spawned during Step 3, record its routing source. Produce an object mapping agent name to source string. Values are `"generic"` (default/no routing entry), `"learned:{prompt-name}"` (replace mode), or for alongside mode, record both entries: `"{auditor-name}"` maps to `"generic"` and `"{auditor-name}:learned"` maps to `"learned:{prompt-name}"`. This field enables the learn step to correlate agent routing decisions with quality outcomes.
7. Compute agent routing overlap data (alongside mode only):
   - For each auditor that ran in alongside mode during Step 3, compare the deduplicated finding sets from the generic and learned spawns.
   - Compute the dedup key set for each: `{file}:{line}:{category}`.
   - Record `alongside_overlap`: an object mapping auditor name to an object with:
     - `generic_finding_keys`: array of dedup keys from the generic auditor
     - `learned_finding_keys`: array of dedup keys from the learned auditor
     - `learned_is_superset`: boolean, true if every key in `generic_finding_keys` also appears in `learned_finding_keys`
     - `alongside_task_count`: integer, the current alongside task count for this auditor (incremented from the prior retrospective's value, or 1 if no prior value)
   - If no auditors ran in alongside mode, set `alongside_overlap` to `{}`.
8. Compute spawn/waste tracking fields:
   - `subagent_spawn_count`: Scan `.dynos/task-{id}/execution-log.md`. Count all lines containing `[SPAWN]`. If the file is missing, set to `0`.
   - `wasted_spawns`: Count auditor runs that produced zero findings. For each parsed audit report from step 1, if the `findings` array is empty (length 0), increment this counter.
   - `auditor_zero_finding_streaks`: Compute per-auditor zero-finding streaks. Read the most recent prior task's `task-retrospective.json` (not the current task) to get the previous streak values. If it does not exist, or uses the old single-integer `auditor_zero_finding_streak` format, treat all prior streaks as `0`.
     - For each auditor that was **spawned** in this audit cycle and produced **zero findings**: increment its streak from the prior retrospective value by 1.
     - For each auditor that was **spawned** and produced **one or more findings**: reset its streak to `0`.
     - For auditors that were **skipped** (not spawned) in this audit cycle: carry forward their streak value unchanged from the prior retrospective.
   - `executor_zero_repair_streak`: Read `repair-log.json`. Sort executor segments by execution order. Starting from the most recent, count consecutive executor segments that needed zero repairs (i.e., were not assigned any repair tasks). Stop counting at the first segment that had repairs. If `repair-log.json` is missing or malformed, set to `0`.
9. Compute reward vector fields. Each score is clamped to the range `[0, 1]` (minimum 0, maximum 1):
   - `quality_score`: `1 - (surviving_findings / total_findings)`. Where `surviving_findings` is the count of findings still present after the final re-audit (i.e., findings that were not resolved), and `total_findings` is the total number of unique findings discovered across all audit and re-audit passes. If `total_findings` is `0`, set `quality_score` to `0.9` (not 1.0, because zero findings may indicate auditor gaps rather than perfect quality; a perfect 1.0 should only come from resolving known findings).
   - `cost_score`: token-efficiency score normalized by task risk level. Compute `avg_tokens_per_spawn = total_token_usage / max(1, subagent_spawn_count)`. Determine budget per spawn by `risk_level`: low=8000, medium=12000, high=18000, critical=25000 (default 12000). Then compute `cost_score = 1 / (1 + (avg_tokens_per_spawn / budget))`. If `total_token_usage` is `0` or `subagent_spawn_count` is `0`, set `cost_score` to `1.0`. Additionally, if ALL agents used default routing (every entry in `agent_source` is `"generic"`), apply a `0.05` penalty: `cost_score = max(0, cost_score - 0.05)`. This penalizes cold-start/default-fallback runs that could not leverage learned routing.
   - `efficiency_score`: `1 - (repair_cycle_count / 3) - (max(0, spec_review_iterations - 1) * 0.1)`. Uses the `repair_cycle_count` computed in substep 3 and the `spec_review_iterations` computed in substep 4. Multiple spec review iterations indicate the spec required rework, which is an efficiency cost.
10. Write `.dynos/task-{id}/task-retrospective.json` as a flat JSON object (no nesting beyond one level):

```json
{
  "task_id": "task-{id}",
  "task_outcome": "DONE",
  "task_type": "feature",
  "task_domains": "ui,backend",
  "task_risk_level": "medium",
  "findings_by_auditor": { "security-auditor": 2, "code-quality-auditor": 1 },
  "findings_by_category": { "sec": 2, "cq": 1 },
  "executor_repair_frequency": { "backend-executor": 2 },
  "spec_review_iterations": 1,
  "repair_cycle_count": 0,
  "subagent_spawn_count": 12,
  "wasted_spawns": 2,
  "auditor_zero_finding_streaks": { "security-auditor": 0, "spec-completion-auditor": 1, "code-quality-auditor": 3, "dead-code-auditor": 5, "ui-auditor": 2 },
  "executor_zero_repair_streak": 3,
  "token_usage_by_agent": { "security-auditor": 45000, "code-quality-auditor": 38000, "spec-completion-auditor": 52000, "repair-coordinator": 15000, "backend-executor": 62000 },
  "total_token_usage": 212000,
  "model_used_by_agent": { "security-auditor": "opus", "code-quality-auditor": "sonnet", "spec-completion-auditor": "sonnet", "repair-coordinator": "sonnet", "backend-executor": "opus" },
  "agent_source": { "security-auditor": "generic", "code-quality-auditor": "learned:cq-v2", "code-quality-auditor:learned": "learned:cq-v2", "spec-completion-auditor": "generic" },
  "alongside_overlap": { "code-quality-auditor": { "generic_finding_keys": ["src/main.ts:10:cq"], "learned_finding_keys": ["src/main.ts:10:cq", "src/util.ts:3:cq"], "learned_is_superset": true, "alongside_task_count": 2 } },
  "quality_score": 0.67,
  "cost_score": 0.79,
  "efficiency_score": 1.0
}
```

If no audit reports or repair logs exist, write the file with zeroed-out counts:

```json
{
  "task_id": "task-{id}",
  "task_outcome": "DONE",
  "task_type": "feature",
  "task_domains": "backend",
  "task_risk_level": "low",
  "findings_by_auditor": {},
  "findings_by_category": {},
  "executor_repair_frequency": {},
  "spec_review_iterations": 0,
  "repair_cycle_count": 0,
  "subagent_spawn_count": 0,
  "wasted_spawns": 0,
  "auditor_zero_finding_streaks": {},
  "executor_zero_repair_streak": 0,
  "token_usage_by_agent": {},
  "total_token_usage": 0,
  "model_used_by_agent": {},
  "agent_source": {},
  "alongside_overlap": {},
  "quality_score": 1.0,
  "cost_score": 1.0,
  "efficiency_score": 1.0
}
```

Old retrospectives from prior tasks that lack the `token_usage_by_agent`, `total_token_usage`, `model_used_by_agent`, `agent_source`, `alongside_overlap`, `quality_score`, `cost_score`, or `efficiency_score` fields are treated as missing data (default `null`), not errors. The learn skill and any aggregation logic must handle their absence gracefully.

Append to log:
```
{timestamp} [DONE] reflect — task-retrospective.json written
```

**Learn (inline -- no subagent):**

After writing the retrospective, automatically aggregate all retrospectives in the project into the memory file. Follow the steps in `skills/learn/SKILL.md`:

1. Scan all `.dynos/task-*/task-retrospective.json` files in the current working directory.
2. Aggregate: top 5 finding categories, executor reliability rankings, average repair cycles by task type, prevention rules (from finding descriptions), and spawn efficiency metrics.
3. Determine project memory path (`~/.claude/projects/<project>/memory/` where `<project>` is derived from the cwd by replacing `/` with `-`).
4. Write (or overwrite) `dynos_patterns.md` to the project memory directory using the format defined in `skills/learn/SKILL.md`.

If no retrospective files are found (edge case), skip silently.

Append to log:
```
{timestamp} [DONE] learn — dynos_patterns.md updated ({N} tasks aggregated)
```

**Postmortem + Improve (inline -- no subagent):**

After learn, run the postmortem and improvement cycle. These are deterministic Python scripts:

```bash
python3 "${PLUGIN_HOOKS}/dynopostmortem.py" generate --root "${PROJECT_ROOT}"
python3 "${PLUGIN_HOOKS}/dynopostmortem.py" improve --root "${PROJECT_ROOT}"
```

If `PLUGIN_HOOKS` is not available, use the hooks directory relative to this skill file, or run:
```bash
python3 hooks/dynopostmortem.py generate --root .
python3 hooks/dynopostmortem.py improve --root .
```

Append to log:
```
{timestamp} [DONE] postmortem — generated for {task_id}
{timestamp} [DONE] improve — {N} proposals, {M} applied
```

If either step fails, log the error and continue. Do not block completion.

**Trajectory rebuild (inline):**

```bash
python3 "${PLUGIN_HOOKS}/dynostrajectory.py" rebuild --root "${PROJECT_ROOT}"
```

Write `completion.json`. Transition the task to `DONE` by calling `transition_task(task_dir, "DONE")` from `dynoslib.py` (this sets both `stage` and `completion_at`). If calling the function directly is not possible, manually set both `"stage": "DONE"` and `"completion_at": "{ISO timestamp}"` in `manifest.json`. Append to log:
```
{timestamp} [ADVANCE] CHECKPOINT_AUDIT → DONE
```
Print (listing only auditors that were actually spawned, not skipped):
```
Audit complete — ALL PASSED

  {auditor-name}:  PASS
  ... (one line per spawned auditor)

Task complete. Snapshot branch dynos/task-{id}-snapshot can be deleted if desired.
```

---

## Standalone use (no active task)

If no active task is found, run the 4 universal auditors on `git diff --name-only HEAD`. Skip Step 5 (no DONE state to write). Print results and stop.
