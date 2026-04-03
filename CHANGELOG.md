# Changelog: dynos-work

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to **Semantic Versioning**.

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

[4.0.0]: https://github.com/hassam/dynos-work/compare/v3.0.0...v4.0.0
