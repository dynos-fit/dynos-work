# dynos-work: Open Heart Surgery

## What This Is

A detailed refactoring plan to simplify dynos-work back to its core purpose: **a reinforcement-learning self-improving system for AI agent task execution.**

The system has grown to 64 Python files (24,759 lines), 21 skills, a 9-step daemon, a 4-stage event waterfall, two overlapping learning loops, a Q-learning system that doesn't actually learn, and a benchmarking subsystem that doesn't actually benchmark. This document describes how to cut it down to what matters.

---

## The Core Loop (What We Actually Want)

```
Task completes → Score outcome → Update agent effectiveness → Promote/demote agents → Route better agents to future tasks
```

That's it. Everything else is either supporting this loop or ceremony around it.

---

## Current Architecture (What Actually Exists)

### Module Inventory: 64 Python files across 5 tiers

**Tier 0 — Foundation (cannot remove):**
- `dynoslib_core.py` (477 lines) — Stage machine, path helpers, task transitions, retrospective collection. Imported by 35/64 modules.
- `dynoslib_defaults.py` — Constants, token budgets, model policies.
- `dynoslib_log.py` — Event logging to `.dynos/events.jsonl`.

**Tier 1 — State Management (essential but bloated):**
- `dynoslib_registry.py` — Learned agent registry. Reads/writes `~/.dynos/projects/{slug}/learned-agents/registry.json`. The single source of truth for agent state (mode, status, scores, evaluations).
- `dynoslib_benchmark.py` (421 lines) — Fixture synthesis, evaluation, benchmark history. Writes to `~/.dynos/projects/{slug}/benchmarks/`.
- `dynoslib_trajectory.py` (308 lines) — Task similarity search. Rebuilds `trajectories.json` from retrospectives.
- `dynoslib_queue.py` — Automation queue for scheduled re-benchmarking.
- `dynoslib_events.py` — File-based event emission/consumption.
- `dynoslib_receipts.py` (490 lines) — Receipt chain validation for execution evidence.
- `dynoslib_tokens.py` (321 lines) — Token usage tracking per phase.
- `dynoslib_contracts.py` — Runtime contract validation at pipeline boundaries.
- `dynoslib_validate.py` (579 lines) — Artifact validation: task contracts, segment ownership.

**Tier 2 — Learning/Evolution (the actual RL system):**
- `dynoevolve.py` — Generates learned agent markdown from retrospective patterns. Registers in shadow mode.
- `dynopatterns.py` (845 lines) — Computes EMA effectiveness scores per (role, task_type, model). Generates `dynos_patterns.md` with model policy, skip policy, baseline tables.
- `dynorouter.py` (917 lines) — Deterministic routing: UCB1 model selection, skip decisions, learned agent injection, ensemble voting. The brain of the system.
- `dynoeval.py` — Offline evaluator: decides promotions from benchmark scores.
- `dynopostmortem.py` (468 lines) — Analyzes completed tasks for anomalies.
- `dynopostmortem_improve.py` (429 lines) — Applies improvements to policy.json, prevention-rules.json, registry.

**Tier 3 — Automation/Orchestration (ceremony):**
- `dynomaintain.py` (493 lines) — Persistent daemon running 9-step cycle every 3600s.
- `dynoeventbus.py` — Drains event queue, fires follow-on handlers in 4-stage waterfall.
- `dynoauto.py` — Schedules and runs benchmark challengers.
- `dynobench.py` — Benchmark runner (fixture rollouts).
- `dynofixture.py` — Syncs benchmark fixtures to disk.
- `dynoslib_qlearn.py` (442 lines) — Tabular Q-learning for repair loop decisions.

**Tier 4 — Visualization/Reporting (read-only consumers):**
- `dynodashboard.py` (807 lines) — HTML dashboard.
- `dynoglobal_dashboard.py` (2,215 lines) — Global multi-project HTML dashboard.
- `dynoreport.py` — Runtime observability report.
- `dynolineage.py` — Lineage graph for agents/fixtures/tasks.

**Tier 5 — Global/Multi-Project (not core):**
- `dynoglobal.py` (881 lines) — Cross-project sweep daemon.
- `dynoregistry.py` — Global project registry CLI.
- `dynoglobal_stats.py` — Anonymous cross-project statistics.

**Other utilities:**
- `dynoslib_crawler.py` (665 lines) — Import graph builder (Python/JS/TS/Dart/Rust/Go), PageRank scoring.
- `dynoslib_tokens_hook.py` — Token tracking hook.
- Various one-off scripts, CLI wrappers, route helpers.

### Skills: 21 total
start, execute, audit, plan, investigate, repair, resume, status, evolve, learn, trajectory, maintain, local, global, dashboard, init, register, list, dry-run, founder.

### Daemon Cycle: 9 steps (sequential, any failure recorded but swallowed)
1. `dynostrajectory.py rebuild` — Rebuild similarity store
2. `dynopatterns.py` — Compute EMA effectiveness scores
3. `dynoevolve.py auto` — Generate learned agents
4. `dynopostmortem.py generate-all` — Detect anomalies
5. `dynopostmortem.py improve` — Apply policy improvements
6. `dynofixture.py sync` — Sync benchmark fixtures
7. `dynoauto.py run` — Run benchmark challengers
8. `dynodashboard.py generate` — Render HTML dashboard
9. `dynoreport.py` — Aggregate statistics

### Event Waterfall: 4 stages (each emits the next)
```
task-completed → [learn, trajectory]
  → learn-completed → [evolve, patterns]
    → evolve-completed → [postmortem, improve, benchmark]
      → benchmark-completed → [dashboard, register]
```

---

## What's Wrong

### 1. The benchmarking system doesn't benchmark anything

`synthesize_fixture_for_entry()` takes existing retrospective scores, splits them into "candidate" (top 5 by composite) and "baseline" (the rest), and compares the averages. This is a statistical summary of past performance, not a benchmark. No agent is re-executed. No task is re-run. The whole fixture/history/index/evaluation/challenger machinery (6 files, ~1,500 lines) exists to compare numbers that already live in `task-retrospective.json`.

### 2. Two overlapping learning loops

**Loop 1 (dynoevolve):** Reads retrospectives → generates agent markdown → registers in shadow.
**Loop 2 (dynopostmortem + improve):** Reads retrospectives → detects anomalies → tunes policy.json, prevention-rules.json, and can also seed agents.

Both read the same input (retrospectives). Both can create/modify agents. Both modify the same shared state (registry, prevention rules). The distinction between "generate an agent" and "propose an improvement that seeds an agent" is artificial.

### 3. Q-learning that doesn't learn

`dynoslib_qlearn.py` (442 lines) implements tabular Q-learning for repair loop decisions. But:
- Rewards are binary (+1/-1), not contextual
- No exploration bonus
- The Q-table is loaded but rarely updated during the maintenance cycle
- It's a persisted heuristic cache, not a learning system
- The router's UCB1 already makes better model selection decisions

### 4. `dynoslib_core` is a god object

477 lines, imported by 35 of 64 modules. Contains: stage machine, path helpers, task transitions, receipt gates, retrospective collection, post-completion triggers, JSON I/O, token estimates. Everything depends on it because everything is in it.

### 5. The daemon does too much

9 sequential steps, most of which duplicate work done by the event bus. When a task completes, the event bus fires learn → evolve → patterns → postmortem → improve → benchmark → dashboard. Then the daemon runs on a timer and does the same things again. The daemon is a cron-style catch-up mechanism for when events are missed, but it runs the full pipeline every time.

### 6. Prevention rules are text, not structure

Prevention rules are just strings appended to executor prompts: "Category 'sec' has 35 findings across tasks. Add extra scrutiny for sec-class issues." There's no structured enforcement, no measurement of whether they help, no feedback loop. They're aspirational comments, not a control mechanism.

### 7. Markdown as a data format

`dynopatterns.py` generates a 200+ line markdown file (`dynos_patterns.md`) with effectiveness tables, model policy, skip policy, and baseline data. `dynorouter.py` then parses this markdown back into structured data. The markdown exists for human readability but creates a fragile parse-serialize roundtrip.

---

## The Surgery

### Phase 1: Consolidate the Learning Loop

**Goal:** One loop, not two. One place that reads retrospectives and updates agent/policy state.

**Merge:**
- `dynoevolve.py` + `dynopostmortem.py` + `dynopostmortem_improve.py` → **`dynolearn.py`**

**What `dynolearn.py` does:**
1. Collect all retrospectives
2. Compute effectiveness scores (currently in `dynopatterns.py`)
3. Generate/update learned agents (currently in `dynoevolve.py`)
4. Detect anomalies and tune policy (currently in `dynopostmortem_improve.py`)
5. Write all state: registry.json, policy.json, prevention-rules.json, effectiveness scores

**Delete:**
- `dynopostmortem.py` (analysis absorbed into learn)
- `dynopostmortem_improve.py` (actions absorbed into learn)
- `dynopatterns.py` (score computation absorbed into learn)

**Lines saved:** ~1,742 lines across 3 files, replaced by ~600 lines in one file.

### Phase 2: Kill the Fake Benchmarking

**Goal:** If we're not re-executing agents, don't pretend we are.

**Replace the entire fixture/benchmark/evaluation/challenger stack with:**
- A simple `evaluate_agent()` function that compares an agent's mean retrospective scores against the task-type baseline
- Compute directly from retrospectives. No fixtures, no history.json, no index.json, no challengers.

**Delete:**
- `dynoslib_benchmark.py` (fixture synthesis, evaluation)
- `dynobench.py` (benchmark runner)
- `dynofixture.py` (fixture sync)
- `dynoauto.py` (challenger scheduling)
- `dynoeval.py` (offline evaluator)
- `dynobench_backfill_model.py` (one-time migration)
- `benchmarks/generated/*.json` (generated fixtures)
- `~/.dynos/projects/{slug}/benchmarks/` (history, index)

**Move into `dynolearn.py`:**
- `evaluate_agent(agent, retrospectives) → {delta_quality, delta_composite, recommendation}`
- Promotion/demotion logic (shadow → alongside → replace)

**Lines saved:** ~1,500+ lines across 6 files, replaced by ~100 lines in `dynolearn.py`.

### Phase 3: Simplify the Daemon

**Goal:** The daemon should be a 3-step catch-up loop, not a 9-step pipeline.

**New daemon cycle:**
1. `dynolearn.py` — The unified learning step (scores + agents + policy)
2. `dynodashboard.py generate` — Refresh visibility
3. `dynoreport.py` — Observability

**Remove from daemon:**
- `dynostrajectory.py rebuild` — Move to event bus only (runs on task-completed, not on timer)
- `dynoevolve.py auto` — Absorbed into `dynolearn.py`
- `dynopostmortem.py generate-all` — Absorbed into `dynolearn.py`
- `dynopostmortem.py improve` — Absorbed into `dynolearn.py`
- `dynofixture.py sync` — Deleted (Phase 2)
- `dynoauto.py run` — Deleted (Phase 2)

**Simplify the event waterfall:**
```
task-completed → [learn, trajectory]
  → learn-completed → [dashboard]
```

Two stages instead of four. No intermediate events.

### Phase 4: Kill Q-Learning, Simplify Prevention Rules

**Q-learning:**
- Delete `dynoslib_qlearn.py` (442 lines)
- Replace with simple win-rate tracking in `dynolearn.py`: per (model, role, finding_type) → success_count / total_count
- The router's UCB1 already handles exploration/exploitation

**Prevention rules:**
- Keep as-is for now (they're low-cost text injection), but add a measurement: track whether findings in prevented categories decrease after injection
- If no measurable effect after 10 tasks, auto-prune the rule
- This is a future phase, not blocking

### Phase 5: Break Up `dynoslib_core`

**Split into:**
- `dynoslib_paths.py` — Path helpers, slug computation, directory resolution
- `dynoslib_state.py` — Stage machine, transitions, receipt gates
- `dynoslib_io.py` — JSON I/O, atomic writes, retrospective collection

**Why:** Currently 35 modules import `dynoslib_core` for one or two functions. After the split, each module imports only what it needs. Easier to test, easier to understand dependency flow.

### Phase 6: Kill Markdown as Data Format

> Note: `dynoproactive.py` (the autofix scanner) has been extracted to a separate repo: [dynos-fit/autofix](https://github.com/dynos-fit/autofix).

**Replace `dynos_patterns.md` with `dynos_patterns.json`:**
- Structured JSON with effectiveness scores, model policy, skip policy
- `dynorouter.py` reads JSON directly (no markdown parsing)
- Human readability via `dynoreport.py` or the dashboard, not the data file itself

### Phase 7: Rationalize Shared State

**Current problem:** Multiple modules write to the same files (registry.json, policy.json, prevention-rules.json) without coordination.

**Rule:** Each shared state file has exactly one writer module:
- `registry.json` → written only by `dynolearn.py`
- `policy.json` → written only by `dynolearn.py`
- `prevention-rules.json` → written only by `dynolearn.py`
- `dynos_patterns.json` → written only by `dynolearn.py`
- `queue.json` → written only by `dynoslib_queue.py` (if queue survives Phase 2; otherwise delete)
- `events.jsonl` → written only by `dynoslib_events.py`

Other modules read these files but never write them.

---

## Expected Outcome

### Before
- 64 Python files, 24,759 lines
- 9-step daemon cycle
- 4-stage event waterfall
- 2 learning loops
- 6-file benchmarking subsystem that doesn't benchmark
- Q-learning system that doesn't learn
- Markdown-as-data-format

### After
- ~40 Python files, ~16,000 lines (~35% reduction)
- 3-step daemon cycle
- 2-stage event waterfall
- 1 unified learning loop (`dynolearn.py`)
- Direct retrospective comparison (no fixture machinery)
- Simple win-rate tracking (no Q-tables)
- JSON data throughout

### Files Deleted (estimated)
- `dynopostmortem.py` (468 lines)
- `dynopostmortem_improve.py` (429 lines)
- `dynopatterns.py` (845 lines)
- `dynoslib_benchmark.py` (421 lines)
- `dynobench.py` (~300 lines)
- `dynofixture.py` (~200 lines)
- `dynoauto.py` (~300 lines)
- `dynoeval.py` (~200 lines)
- `dynoslib_qlearn.py` (442 lines)
- `dynobench_backfill_model.py` (~100 lines)

**Total deleted:** ~3,705 lines across 10 files

### Files Created
- `dynolearn.py` (~600 lines) — Unified learning: scores + agents + policy + evaluation
- `dynoslib_paths.py` (~100 lines) — Path helpers
- `dynoslib_state.py` (~200 lines) — Stage machine
- `dynoslib_io.py` (~100 lines) — JSON I/O

### Files Modified
- `dynomaintain.py` — Reduced to 3-step cycle
- `dynoeventbus.py` — Simplified to 2-stage waterfall
- `dynorouter.py` — Read JSON instead of parsing markdown
- `dynoslib_core.py` — Gutted, replaced by split modules
- All importers of `dynoslib_core` — Update imports

---

## Execution Order

Do this in order. Each phase is independently shippable:

1. **Phase 7 first** (markdown → JSON) — Lowest risk, unblocks Phase 1
2. **Phase 5** (break up core) — Mechanical refactor, no behavior change
3. **Phase 1** (consolidate learning) — The big one. Merge 3 files into 1.
4. **Phase 2** (kill fake benchmarks) — Delete 6 files, move evaluation into learn
5. **Phase 3** (simplify daemon) — Shrink from 9 to 3 steps
6. **Phase 4** (kill Q-learning) — Delete 1 file, add win-rate tracking
7. **Phase 7** (rationalize state) — Enforce single-writer rule

---

## What NOT to Touch

- **`dynorouter.py`** — The routing logic is well-designed. UCB1 model selection, skip decisions, ensemble voting. Keep it.
- **Skills** — The skill definitions are thin wrappers. They reference the Python modules. As modules are renamed/merged, update skill references, but don't restructure the skill system itself.
- **Dashboard UI** (`hooks/dashboard-ui/`) — Read-only consumer. Update API endpoints if data shapes change, but don't refactor the React code.
- **`dynoslib_core.py` stage machine** — The stage transitions and receipt gates are correct. Move them to `dynoslib_state.py` but don't simplify the state machine itself.
- **Event bus architecture** — Keep the file-based event queue. Just reduce the number of event types and handlers.

---

## Validation Strategy

After each phase:
1. Run existing tests: `cd hooks/dashboard-ui && npx vitest run`
2. Run a task end-to-end: `/dynos-work:start` → `/dynos-work:execute` → `/dynos-work:audit`
3. Verify the maintenance daemon completes a cycle without errors
4. Verify `dynorouter.py` still routes correctly (check a few routing decisions)
5. Verify learned agents are still generated and registered after task completion

The system should produce identical routing decisions and agent promotions before and after refactoring.
