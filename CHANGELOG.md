# Changelog

All notable changes to dynos-work are documented here.

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
