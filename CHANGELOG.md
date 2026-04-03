# Changelog

All notable changes to dynos-work are documented here.

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

## [2.9.0] - 2026-03-30

### Added
- Self-improving token efficiency with prevention rules injected into executor spawn instructions
- Incremental re-audit scoping: re-audit after repair inspects only repair-modified files
- Spec-completion auditor retains full scope during re-audit for overall requirement coverage
- Prevention rules from `dynos_patterns.md` filtered by executor type and injected at spawn time

### Changed
- Audit skill reflect step computes spawn efficiency metrics (subagent_spawn_count, wasted_spawns, zero-finding streaks)

## [2.8.0] - 2026-03-30

### Added
- Debug skill and forensic investigator agent for deep root cause analysis
- Token consumption optimizations across agents, skills, and hooks
- Workflow optimization reducing subagent spawns by ~22%

### Changed
- Removed non-Claude platform configs (Cursor, Gemini CLI, OpenCode, Codex)
- Session hook and config files updated for v2.8.0

Versions prior to 2.8.0 predate this changelog.
