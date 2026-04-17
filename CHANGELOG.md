# Changelog: dynos-work

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to **Semantic Versioning**.

---

## [7.0.0] - 2026-04-16
### "Verified Foundry": Tool-Grounded Verification, Compliance, Least Privilege

The foundry moves from LLM-reviewing-LLM to deterministic-tool-checking-LLM across every verification surface. Autofix is extracted to a separate repository. The learning layer becomes explicitly optional.

### Added
- **Compliance auditing**: security-auditor flags GPL/AGPL deps, generates SBOM via cyclonedx-bom/syft, verifies dependency provenance via Sigstore, checks for missing privacy code (data export, account deletion). New `compliance` category with `comp-` prefix.
- **Conditional plan sections**: `## API Contracts` required when domains include backend/ui/security; `## Data Model` required when domains include db.
- **Plan gap analysis**: deterministic hook cross-references API Contracts and Data Model tables against actual route definitions (10+ frameworks) and model/schema definitions (8+ ORMs). Plans can't claim endpoints or tables that don't exist.
- **Doc-accuracy auditing**: code-quality-auditor runs `validate_docs_accuracy.py` on tasks touching `.md` files.
- **Per-agent tool boundaries**: all 17 agents declare minimum tool sets in YAML frontmatter. Auditors cannot Write/Edit. Planners cannot Bash.
- **LEARNING_ENABLED policy flag**: `dynos config set learning_enabled false` disables the entire learning layer. Foundry-only mode is now a single config flip.
- **Phase-labeled start.md**: 6 phase headers make structure visible. Zero behavior change.
- **DORA metrics**: `lead_time_seconds`, `change_failure`, `recovery_time_seconds` in retrospectives. `dynos stats dora` aggregator.
- **Usage telemetry**: module-level dormancy detection. `dynos stats usage` CLI.
- **`dynos config` CLI**: get/set project policy without hand-editing JSON.
- **`hooks/compliance_check.py`**: license scanning, SBOM, Sigstore provenance, privacy checks across 10 ecosystems.
- **`hooks/plan_gap_analysis.py`**: deterministic plan verification against codebase.
- **`hooks/lib_usage_telemetry.py`**: append-only JSONL telemetry for dormancy detection.

### Removed
- **Autofix system** (6,327 LOC): `hooks/proactive.py` (3,592 lines), `skills/autofix/`, 5 test files, all autofix constants, Q-table functions, daemon integration, `--autofix` CLI flags. Extracted to `dynos-fit/autofix`.
- `DYNOS_AUTOFIX_WORKTREE` environment variable.
- `"autofix"` from `VALID_PIPELINES`.

### Changed
- `security-auditor` covers `security` + `compliance` categories.
- `code-quality-auditor` covers `code-quality` + `doc-accuracy` categories.
- `repair-coordinator` routes compliance and doc-accuracy findings.
- Audit report schema gains optional `category` field.
- `validate_task_artifacts` enforces conditional headings + gap analysis.
- `compute_reward` includes DORA fields.
- Router gated on `is_learning_enabled()`.
- Event bus skips learning handlers when disabled.
- `maintain.py` stripped of all autofix code; daemon preserved for non-autofix maintenance.
- `start/SKILL.md` gains phase labels; trajectory/learned skill injection gated on learning flag.
- README rewritten (no autofix, DORA metrics, least privilege, tool-grounded verification).

---

## [6.0.0] - 2026-04-03
### "Runtime Control Plane": Deterministic Foundry, Live Dashboard, Maintainer Daemon

This release turns the plugin from a primarily prompt-defined foundry into a runtime-backed adaptive control system. The workflow remains human-directed, but key guarantees now live in code: artifact validation, route gating, benchmark-driven promotion, freshness blocking, lineage, and persistent maintenance automation.

### Added
- Deterministic control runtime:
  - `hooks/dynoslib.py`
  - `hooks/dynosctl.py`
  - `hooks/validate_task_artifacts.py`
- Task artifact enforcement for:
  - manifest validation
  - spec/plan structure
  - execution-graph coverage, ownership, and cycle detection
  - repair-log and retrospective validation
- RL-inspired adaptive runtime components:
  - `hooks/dynostate.py`
  - `hooks/dynostrajectory.py`
  - `hooks/dynosdream.py`
- Learned component lifecycle tooling:
  - `hooks/dynoevolve.py`
  - `hooks/dynoeval.py`
  - `hooks/dynogenerate.py`
  - `hooks/dynofixture.py`
- Benchmark and rollout harnesses:
  - `hooks/dynobench.py`
  - `hooks/dynorollout.py`
  - `hooks/dynochallenge.py`
- Live routing and automation:
  - `hooks/dynoroute.py`
  - `hooks/dynoauto.py`
- Observability and traceability:
  - `hooks/dynoreport.py`
  - `hooks/dynolineage.py`
  - `hooks/dynodashboard.py`
- Persistent maintainer runtime:
  - `hooks/dynomaintain.py`
  - background daemon mode
  - manual invoke mode
  - maintenance status and PID tracking
- New contributor and internals docs:
  - `UNDER_THE_HOOD.md`
  - `ARCHITECTURE.md`
- Automated live dashboard generation and refresh from hooks
- Fixture synthesis from completed task retrospectives
- Benchmark index and lineage graph for task -> component -> fixture -> run traceability

### Changed
- README rewritten from scratch to be user-facing instead of runtime-internal
- Founder mode retained as an advisory design-review layer inside the start flow
- Learned routing now resolves from the live registry, not markdown tables
- Promotion and rollback now depend on benchmark evidence plus must-pass category checks
- Route resolution now blocks stale learned components by freshness policy
- TaskCompleted hook now runs learn, automation, and dashboard refresh automatically
- SessionStart hook now refreshes dashboard state and can ensure the maintainer daemon is running
- `/dynos-work:maintain` is now a clearer user-facing manual maintainer path

### Fixed
- Eliminated several documentation/runtime mismatches where skills described behavior that was not enforced in code
- Added regression tests for route resolution, auto benchmarking, fixture synthesis, lineage, dashboard generation, challenger rollout, and maintainer cycles

### Security
- Learned components can no longer silently remain active after benchmark regression or staleness
- Promotion remains blocked unless challenger evidence clears configured policy thresholds


## [5.0.0] - 2026-04-03
### "Foundry Intelligence": Decision Transformer Architecture

The system evolves from a self-learning platform into a **Trajectory-Driven, Human-Directed Autonomous Software Foundry** with a full Decision Transformer (DT) memory layer.

### Added
- **`trajectory` Skill:** Manages State-Action-Reward (SAR) sequences. Reconstructs the full trajectory of every task and stores it in `.dynos/trajectories.json` for future retrieval.
- **`state-encoder` Agent:** Produces a structured State Signature ($) from a module's AST, Dependency Graph, and Finding Entropy. Powers the DT retrieval step.
- **DT-Informed Discovery:** The `start` skill now spawns the `state-encoder` at the beginning of every task and retrieves the 3 most similar successful past trajectories. Discovery questions are generated from known failure points in those trajectories.
- **MCTS/Dreaming as a Consultant Service:** The `founder` skill is refactored from an independent entry point into a strategic simulation service. It runs Sandbox Playouts for hard/critical design options and returns a **Design Certificate** (PASS/FAIL, Security Score, Performance Metrics, Recommendation) before the user chooses.
- **Unified Foundry Start:** Merged the old "Phase 0 Founder Mode" shortcut into the Standard Discovery pipeline. Every task — regardless of prompt size — now follows the full pipeline ending in mandatory human-approval gates.

### Changed
- **`start` Skill:** Removed the "Phase 0" bypass path. All tasks now begin with RL-informed discovery (Step 1), MCTS Dreaming for hard/critical subtasks (Step 2), and mandatory Spec Review + Plan Review gates for all tasks.
- **`founder` Skill:** Demoted from independent entry point to a strategic "Dreaming Engine." Its MCTS and Sandbox Simulation logic is now called as a service by `start` for design option vetting only.
- **Spec Review & Plan Review Gates:** Now explicitly enforced as Hard Rules for every task. No skip paths exist.

### Security
- **Zero-Trust Founding:** Founder's sandbox simulations are now audited by an Opus-level auditor before Design Certificates are issued to the user.

---

## [4.0.0] - 2026-04-03
### "God-Mode" Evolution: The Autonomous Software Foundry

This is a major architectural overhaul, transforming the system from a task-based plugin into a **Self-Learning, High-Performance Autonomous Engineering Platform.**

### Added
- **`founder` Skill:** A new strategic entry point for minimal prompts. Uses **MCTS** and **Dreaming** (Sandbox Simulation) to bootstrap full production-grade systems.
- **`maintain` Skill:** The Autonomous Backend worker. Performs proactive self-audits, identifies technical debt clusters, and opens automated PRs.
- **`evolve` Skill:** Manages agent lifecycle through **Shadow Mode** and **Simulation Benchmarking** at `/tmp`.
- **Ensemble Voting:** High-risk audits now utilize a multi-model consensus (Haiku/Sonnet) before escalating to Opus for 58% cost efficiency.
- **Critical Path Scheduler:** An optimized execution engine that prioritizes the most "blocking" segments and runs audits in parallel with code implementation.
- **Incremental Caching:** Skips executor spawns for unchanged segments, reducing token costs by up to 80% on re-runs.
- **Web Dashboard:** A premium, visual Engineering Control Center (`.dynos/dashboard.html`) with finding density heatmaps and ROI charts.
- **RAG-Lite Docs Refresh:** Automated fetching of official library documentation during the learning phase.
- **Global Pattern Syncing:** Cross-project intelligence sharing via external memory paths.

### Changed
- **Planning Agent:** Upgraded to **Hierarchical Planning** (Master/Worker) to handle high-complexity tasks.
- **`start` Skill:** Integrated **Founder Mode** (Phase 0) and strategic interrogation loops.
- **`execute` Skill:** Implemented **Progressive Pipelining** for real-time background auditing.
- **`learn` Skill:** Decoupled from agent management; now focused on high-density pattern extraction and "Human Insight" gates.
- **Model Policies:** Transitioned to dynamic, EMA-based model routing (ROI tracking).

### Security
- **Multi-Modal Visual Audit:** Integrated browser-based vision checks for UI-domain tasks.
- **Simulation Isolation:** All candidate agents are now verified in a **Security Sandbox** at `/tmp` before promotion.
- **Downtime Shield:** Automated auto-merge policy is strictly prohibited from merging if any tests are failing.

---

## [3.0.0] - 2026-04-03

### Added
- Learned agents system: dynos-work generates project-specific executors and auditors that improve over time
- Agent generation in learn step: analyzes codebase patterns and repair history, rate-limited to every 3 tasks, passive (no extra spawns)
- Agent Routing table in `dynos_patterns.md`: routes to learned agents when they outperform generics
- Alongside/replace mode for learned auditors: both run for 3-task proving window, then replace when proven
- Self-pruning: learned agents soft-deleted to `.archive/` after 3 consecutive tasks of underperformance
- EMA quad key: (role, model, task_type, source) tracks generic vs learned agent effectiveness separately
- Finding-overlap tracking for alongside auditor evaluation
- Path validation on learned agent file reads (defense-in-depth)
- Sanitization on agent generation instructions (prompt injection prevention)
- Security-auditor replace protection (can never be replaced by learned agent)
- Priority-stack composite weights: 0.6 quality + 0.25 efficiency + 0.15 cost

### Changed
- `skills/learn/SKILL.md` gains Steps 6-9: agent generation, routing table, pruning, mode transitions
- `skills/execute/SKILL.md` reads Agent Routing table, routes to learned executors when composite is higher
- `skills/audit/SKILL.md` reads Agent Routing table, supports alongside/replace mode, tracks agent_source and finding overlap in retrospective
- EMA effectiveness tracking extended from triple to quad key

---

## [2.12.0] - 2026-04-03

### Added
- `/dynos-work:dry-run` skill with declarative contract validation: each pipeline skill has a `contract.json` sidecar declaring input/output schemas; dry-run validates the chain
- `/dynos-work:dashboard` skill: hybrid data source rendering policy state from `dynos_patterns.md` plus time-series trends (token cost, model distribution, quality score) from retrospectives
- UCB exploration bonus in model selection: `ucb_score = composite + 0.5 * sqrt(ln(total) / sample_count)` replaces pure composite, naturally exploring under-observed model/role/task_type triples
- Prevention rule aging with FIFO eviction: rules carry `created_task_id`, oldest evicted first at 15-rule cap, 3-task eviction exemption prevents bulk flushing
- Baseline Policy reconstruction from best 3-task quality window when no baseline exists
- TaskCompleted hook activation: auto-triggers learn after task completion, conditional auto-commit gated on `dynos_auto_commit` in project settings.json
- 12 `contract.json` sidecar files across all skill directories
- Rule sanitization in learn skill to prevent prompt injection via finding descriptions

### Changed
- Token tracking extraction now specifies exact field path (`total_tokens` from Agent tool usage summary)
- `classification.type` / `task_type` naming normalized with explicit mapping notes across audit, execute, and repair-coordinator
- Auto-commit uses `git add -u` (tracked files only) instead of `git add -A` to prevent staging secrets
- Prevention rules table gains `Created` column for age tracking

### Removed
- `agents/execution-coordinator.md` (orphaned, functionality absorbed by planning agent)

---

## [2.11.0] - 2026-04-02

### Added
- Actor-critic inspired adaptive model selection: EMA effectiveness scores tracked per (role, model, task_type) triple
- Model Policy table in `dynos_patterns.md`: recommends optimal model for each agent based on observed quality, cost, and efficiency
- Skip Policy table: learned skip thresholds per auditor replace hardcoded value of 3
- Effectiveness Scores table: raw EMA data driving policy derivation
- Baseline Policy: rolling snapshot for regression detection and revert
- Reward vector computation in reflect step: quality_score, cost_score, efficiency_score
- Real token tracking: `token_usage_by_agent`, `total_token_usage`, `model_used_by_agent` in retrospective
- Meta-validator: bounds checking, monotonicity constraints (security-auditor always Opus), regression detection with rolling baseline blend-back
- Cold-start gate: hardcoded defaults for first 5 tasks, then adaptive
- Policy readers in audit skill, execute skill, and repair-coordinator with fallback to defaults
- Per-task-type baseline token budgets for cost_score (feature: 50k, refactor: 30k, bugfix: 20k, other: 40k)

### Changed
- `skills/learn/SKILL.md` gains Step 5 (Policy Update) with EMA, policy derivation, meta-validation, and baseline management
- `skills/audit/SKILL.md` Step 3 reads Skip Policy and Model Policy; Step 5 Reflect captures tokens and computes reward vector
- `skills/execute/SKILL.md` Step 3 reads Model Policy for executor model selection
- `agents/repair-coordinator.md` reads Model Policy for retry 0-1 findings; retry >= 2 always Opus (non-negotiable)
- `dynos_patterns.md` gains four new sections: Model Policy, Skip Policy, Effectiveness Scores, Baseline Policy
- README updated with adaptive model selection and actor-critic self-improvement details

---

## [2.10.0] - 2026-04-02

### Added
- Eager two-phase repair: repair starts on first auditor findings (phase 1) while slower auditors continue running; late results feed into phase 2
- Short-circuit on critical spec failure: spec-completion-auditor critical findings trigger immediate phase 1 repair
- Parallel repair batch execution: non-overlapping batches run concurrently, only file-conflicting batches are serialized
- Model escalation on retry: findings failing twice (retry >= 2) automatically upgrade executor to Opus
- Auditor skip on zero-finding streak: skip-eligible auditors (dead-code, ui, db-schema) auto-skipped after 3+ consecutive zero-finding tasks
- Per-auditor zero-finding streak tracking in task retrospective (replaces old single-integer field)
- Late-finding conflict resolution: all late auditor findings queue for phase 2, no interruption of in-progress repairs
- Cross-phase retry continuity: max_retries (3) applies across both phases combined per finding

### Changed
- `skills/audit/SKILL.md` Steps 3, 4, 5 rewritten for two-phase pipeline
- `agents/repair-coordinator.md` updated with phase awareness, `model_override` field, and cross-phase retry rules
- `auditor_zero_finding_streak` (integer) replaced by `auditor_zero_finding_streaks` (object map) in retrospective schema
- Repair-coordinator now sets `parallel` field on batches and `model_override` on escalated tasks
- README updated with new sync optimization details

---

## [2.9.0] - 2026-03-30

### Added
- Self-improving token efficiency with prevention rules injected into executor spawn instructions
- Incremental re-audit scoping: re-audit after repair inspects only repair-modified files
- Spec-completion auditor retains full scope during re-audit for overall requirement coverage
- Prevention rules from `dynos_patterns.md` filtered by executor type and injected at spawn time

### Changed
- Audit skill reflect step computes spawn efficiency metrics (subagent_spawn_count, wasted_spawns, zero-finding streaks)

---

## [2.8.0] - 2026-03-30

### Added
- Debug skill and forensic investigator agent for deep root cause analysis
- Token consumption optimizations across agents, skills, and hooks
- Workflow optimization reducing subagent spawns by ~22%

### Changed
- Removed non-Claude platform configs (Cursor, Gemini CLI, OpenCode, Codex)
- Session hook and config files updated for v2.8.0

---

Versions prior to 2.8.0 predate this changelog.

[5.0.0]: https://github.com/dynos-fit/dynos-work/compare/v4.0.0...v5.0.0
[4.0.0]: https://github.com/dynos-fit/dynos-work/compare/v3.0.0...v4.0.0
[3.0.0]: https://github.com/dynos-fit/dynos-work/compare/v2.12.0...v3.0.0
[2.12.0]: https://github.com/dynos-fit/dynos-work/compare/v2.11.0...v2.12.0
[2.11.0]: https://github.com/dynos-fit/dynos-work/compare/v2.10.0...v2.11.0
[2.10.0]: https://github.com/dynos-fit/dynos-work/compare/v2.9.0...v2.10.0
[2.9.0]: https://github.com/dynos-fit/dynos-work/compare/v2.8.0...v2.9.0
[2.8.0]: https://github.com/dynos-fit/dynos-work/compare/v2.7.0...v2.8.0
[7.0.0]: https://github.com/dynos-fit/dynos-work/compare/v6.0.0...v7.0.0
[6.0.0]: https://github.com/dynos-fit/dynos-work/compare/v5.0.0...v6.0.0
