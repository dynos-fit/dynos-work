# Foundry Hardening Spec — OSS Tier 0/1

**Status:** Proposal. Decomposes into three execution-phase tasks.

**Predecessor docs:** `docs/write-boundary-spec.md`, `ARCHITECTURE.md`, `memory/project_audit_forgery_incident.md`, `memory/project_open_trust_residuals.md`.

**Scope:** This spec covers what an open-source `dynos-work` should ship. It closes one CRITICAL finding (C1: native Python I/O bypasses write-policy) and one HIGH finding (H1: cross-task `eventbus_handler` forgery) without degrading the single-developer workflow. **C2 (predictable HMAC project-secret) is resolved**: `hooks/lib_log.py::_resolve_event_secret` now generates the project secret via `secrets.token_hex(32)` and persists it in a 0o600 file — the `sha256(path:hostname)` derivation no longer exists in the codebase. Phase 2 of this spec upgrades the secret storage to an OS keychain (Tier 1) but the original predictability vulnerability is already closed. Higher-assurance controls (signed artifacts, two-actor approval, off-host append-only logs, confidential computing, post-quantum crypto) are explicitly out of scope and tracked as Tier 2+ for downstream/proprietary forks.

---

## 0. Bottom Line

Today, dynos-work has a homemade userspace integrity layer. Several defenses (HMAC-chained receipt log, spawn-log cross-check, write-policy gate) are well-engineered. None of them survives an executor subagent that does `Bash python3 -c "Path('spawn-log.jsonl').write_text(...)"` because all enforcement lives in code that the subagent's Bash tool can also run.

This spec does NOT add features. It moves three existing defenses from advisory (a comment in `write_policy.py`) to mechanical (kernel-enforced, hardware-anchored, per-task isolated) — and does so under a `compliance-tier` setting that defaults to today's behavior so no current user sees breakage.

**Tier 0 (default, unchanged):** Today's behavior. Userspace policy. Cleartext secret cache. Project-global events.jsonl. The single developer running `/dynos-work:start` on their laptop sees zero change.

**Tier 1 (opt-in, ships with OSS):** Kernel-enforced subagent sandbox + OS-keychain-anchored project secret + per-task events log. Closes C1, C2, H1. Activated by `compliance-tier=tier-1` in the project's policy.json. UX cost: one-time keychain setup at first task; sub-millisecond sandbox launch overhead per spawn.

---

## 1. Threat Model (OSS scope)

### Adversary classes IN scope

- **A1 — Compromised orchestrator LLM.** The model running the skill prompt is hostile or coerced (jailbreak, prompt injection from upstream input). It can call any tool the harness exposes and can spawn subagents whose prompts it controls.
- **A2 — Compromised subagent.** A spawned executor or auditor returns hostile output, attempts to escalate role, attempts to write a control-plane file via Bash → Python.
- **A3 — Same-UID persistent-state attacker.** Any process with the same UID as the orchestrator (other terminals, background jobs, post-exploitation footholds) has read/write access to `~/.dynos/projects/<slug>/` and `.dynos/`.

### Adversary classes OUT of scope (deferred to Tier 2+)

- **A4 — Supply-chain attacker** controlling a learned-agent file or a prevention rule. Tier 2 closes this with signed artifacts; Tier 1 leaves it residual and documents the gap.
- **A5 — Distribution attacker** substituting plugin contents in transit. Closed by repo-level mechanisms (git, GitHub) and operator install hygiene; Tier 2 adds Sigstore.
- **A6 — Insider** acting outside policy. Closed by Tier 2 two-actor approval; Tier 1 single-developer use accepts insider risk by definition.
- **A7 — Hardware compromise** of the workstation. Defended by the host platform (Secure Boot, TPM, full-disk encryption); not in foundry's scope.
- **A8 — LLM provider trust.** Mitigated at the contract layer with the model provider; not in foundry's code.

### Security goals (must hold under any A1–A3 compromise at Tier 1)

- **G1 — No forged receipts.** A subagent cannot create or modify a receipt that passes chain validation.
- **G2 — No forged spawns.** The `spawn-log.jsonl` cross-check (the post-2026-04-30 forgery defense) cannot be defeated by a subagent appending to the log.
- **G3 — No project-secret recovery.** An A3 attacker reading the persistent project directory cannot recover the HMAC project secret.
- **G4 — No cross-task event injection.** Events written by task A cannot be consumed as evidence by task B's gates.

---

## 2. Architectural Principles (Tier 1)

- **P1 — Userspace policy is advisory; kernel policy is authoritative.** Every load-bearing trust boundary at Tier 1 is enforced by the OS (Landlock on Linux 5.13+, `sandbox-exec` on macOS, `bwrap` namespace fallback on older Linux) at subagent process boundary. `write_policy.py` remains as a fast pre-check + diagnostic layer; it is no longer the only enforcement.
- **P2 — Anchored secrets only.** No HMAC key derives from environment-deterministic inputs (path, hostname, user). The project secret lives in the OS keychain (macOS Keychain Services, Linux Secret Service / libsecret). Per-task derivations chain via HKDF-SHA256 from the anchored secret.
- **P3 — Per-task evidence isolation.** Events used as evidence for a specific task's gates live in that task's directory, signed under that task's per-task HMAC. Cross-task aggregation reads from the on-host stream but only consumes events whose signature verifies under the consuming task's anchor.
- **P4 — Tier 0 backward compatibility.** Tier 1 must be opt-in. A repository with `compliance-tier` unset or `tier-0` runs unchanged. A repository setting `tier-1` runs hardened. Both modes pass the same test suite.

---

## 3. Phased Program

Each phase becomes one `/dynos-work:start` task. ACs are concrete and testable.

### Phase 1 — Subagent process sandbox (closes C1)

**Goal:** Make `write_policy.py`'s decisions kernel-enforced for the subagent boundary at Tier 1.

**Acceptance criteria:**

1. New module `hooks/sandbox.py` exposes `launch_sandboxed_subagent(role, allowed_writes: list[Path], task_dir: Path)` that wraps the harness's Agent-spawn entry point with a per-platform kernel sandbox:
   - **Linux 5.13+**: Landlock ABI v3+ (filesystem rules) plus seccomp-bpf (block `ptrace`, `process_vm_writev`, `bpf`).
   - **Linux <5.13**: `bwrap` (bubblewrap) with read-only bind mounts for `hooks/` + `agents/`, tmpfs for everything else, an explicit allowlist of writable paths.
   - **macOS**: `sandbox-exec` with a generated `.sb` profile derived from `write_policy.py`'s decisions for the role. Acknowledged that `sandbox-exec` is deprecated; macOS support is best-effort and gap-documented (see R1).
2. The role-specific allowlist is materialized from `hooks/write_policy.py`'s decision matrix at sandbox-launch time and embedded in the kernel policy. The Python-level `decide_write` function is no longer consulted by sandboxed subagents — they simply receive `EACCES` from the kernel on disallowed writes.
3. `hooks/agent-spawn-log` runs in its own sandbox where `spawn-log.jsonl` is the ONLY writable path (no other principal — including the orchestrator's own process — can `open()` it for writing under Tier 1).
4. Adversarial test in `tests/test_sandbox_executor_cannot_write_control_plane.py`:
   - Spawns an executor subagent under Tier 1.
   - Subagent runs `Bash python3 -c "from pathlib import Path; Path('.dynos/task-X/spawn-log.jsonl').write_text(...)"`.
   - Test asserts: write fails with `PermissionError`, no spawn-log entry was added, and a `sandbox_violation` event is emitted under the supervisor's secret (separate from the task's HMAC).
5. Tier 0 path is preserved: when `compliance-tier` is unset or `tier-0`, `launch_sandboxed_subagent` becomes a passthrough that calls the existing Agent-spawn entry point with no kernel policy applied. Existing test suite must pass unchanged.
6. Sandbox launch overhead measured at <2ms per spawn on Linux Landlock and <10ms on macOS `sandbox-exec`. Recorded as a deterministic event for telemetry; regression is a CI failure.
7. New CLI: `python3 hooks/ctl.py sandbox-test --role <role>` runs a self-check that verifies the sandbox actually denies a known-bad write. Operator runs this once after installation; result becomes part of `compliance-check` (Phase 4).

**Out of scope for Phase 1:** Windows support. Sandboxing the orchestrator itself (orchestrator is the Claude Code harness; not foundry's process to sandbox). Confidential computing / SEV-SNP / TDX (Tier 3).

**Audit gate:** After implementation, run a one-shot adversarial test pack that exercises every documented write_policy denial path through a Tier 1 subagent's Bash → Python escape route. All must produce `EACCES`, not silent success.

### Phase 2 — OS-keychain-anchored project secret (closes C2 at Tier 1)

**Goal:** Upgrade the project secret from the 0o600 file written by `hooks/lib_log.py::_resolve_event_secret` (which already uses `secrets.token_hex(32)` — the original `sha256(path:hostname)` derivation is no longer present) to an OS-keychain-anchored secret at Tier 1.

**Historical note:** C2 as originally filed (predictable `sha256(path:hostname)` derivation) is **resolved** — `_resolve_event_secret` in `hooks/lib_log.py` (function starting at line 194) now generates secrets via `secrets.token_hex(32)` written to a 0o600 file. Phase 2 advances the storage from a 0o600 file to the OS keychain (Tier 1) and adds HKDF-SHA256 per-task derivation.

**Acceptance criteria:**

1. New module `hooks/secret_anchor.py` exposes `get_project_secret(root: Path) -> bytes` that resolves the project secret in this order:
   - (a) `DYNOS_EVENT_SECRET` env var (unchanged from today, preserves Tier 0 escape hatch).
   - (b) **Tier 1 only**: OS keychain entry under service `"dynos-work-project"`, account `<project-slug>`. Read via:
     - **macOS**: Keychain Services API via `security find-generic-password` or `keychain` Python binding.
     - **Linux**: libsecret / Secret Service API via `keyring` Python library (already a common dep). Falls through if no D-Bus session (e.g., headless server) — see R2.
   - (c) **Tier 1 only**: if no keychain entry exists, generate a 32-byte secret from `secrets.token_bytes(32)` and store it in the keychain. First-run is automatic; no operator gesture required.
   - (d) **Tier 0 only**: existing 0o600 file at `_persistent_project_dir(root) / "event-secret"`. Behavior unchanged.
2. `hooks/lib_log.py::_resolve_event_secret` delegates to `secret_anchor.get_project_secret` when `compliance-tier=tier-1`. The Tier 0 path (0o600 file with `secrets.token_hex(32)`) is preserved unchanged.
3. Per-task derivation moves from string concatenation to HKDF-SHA256:
   - Replace `_derive_per_task_secret(secret, task_id)` body with `HKDF(salt=task_id.encode(), info=b"dynos-work/v1/per-task-event-secret", length=32, algorithm=SHA256).derive(secret)`.
   - The HKDF call lives in `hooks/secret_anchor.py`. `lib_log.py` imports the helper.
   - Tier 0 path keeps the existing string-concat derivation (mathematical change is breaking; gated by tier).
4. Adversarial test in `tests/test_secret_anchor_unrecoverable_from_disk.py`:
   - Under Tier 1, after first run, the persistent project directory contains NO file whose contents reveal the secret.
   - Test reads every byte of every file under `_persistent_project_dir(root)` and asserts none decode to the active project secret.
   - Test deletes the on-disk persistent dir, regenerates from keychain, and asserts the secret survives.
5. Migration: a one-shot CLI `python3 hooks/ctl.py secret-anchor migrate` reads the existing 0o600 file (Tier 0), writes the same value to the keychain, and deletes the file. Operator can roll back by re-flipping `compliance-tier=tier-0`.
6. Documentation in `docs/secret-anchor.md` covers the threat model (G3), the keychain location per platform, and the migration procedure.

**Out of scope for Phase 2:** TPM/HSM/YubiKey integration (Tier 2). Multi-device anchor with M-of-N recovery (Tier 2). Key rotation procedure (Tier 2 — Tier 1 keys persist for the project's lifetime).

**Audit gate:** Cryptographic review of HKDF parameters and the keychain wrapper. The audit tool from Phase 1 is extended to confirm the post-Tier-1 disk state contains no recoverable secret.

### Phase 3 — Per-task events.jsonl (closes H1 partially)

**Goal:** Close the documented cross-task `eventbus_handler` forgery residual (memory: project_open_trust_residuals.md, finding 1) within OSS scope. The full closure (off-host append-only log) remains Tier 2.

**Acceptance criteria:**

1. New events written by `hooks/lib_log.py::log_event` go to `<task_dir>/events.jsonl` instead of `.dynos/events.jsonl` when `task` is non-None and `compliance-tier=tier-1`. The project-global `.dynos/events.jsonl` continues to exist for non-task events and for cross-task aggregation reads, but is no longer the source of truth for any single task's gates.
2. `hooks/lib_log.py::iter_events` and every consumer that reads `events.jsonl` for a specific task (search: `policy_engine.py::_build_events_by_task`, `lib_validate.py` retrospective integrity check, `receipts/stage.py::_assert_spawn_log_evidence`) reads from `<task_dir>/events.jsonl` first; falls back to project-global `.dynos/events.jsonl` only at Tier 0.
3. The per-task HMAC signature on each event (already present in the codebase, derived per-task from the project secret) is now verified at consume time, not just at write time. A consumer reading `<task_dir>/events.jsonl` rejects entries whose signature doesn't verify under the consuming task's HKDF-derived key.
4. The cross-task forgery scenario from `memory/project_open_trust_residuals.md`: an attacker appending `{"event":"eventbus_handler","task":"task-target","handler":"X"}` to project-global `.dynos/events.jsonl`. Under Tier 1:
   - The handler-event consumer in `receipt_post_completion` reads `<target_task_dir>/events.jsonl`, not project-global.
   - The forged event in project-global is invisible to the gate.
   - Adversarial test in `tests/test_per_task_events_blocks_cross_task_forgery.py` confirms the forged event has no effect on `self_verify`.
5. Tier 0 path: the project-global file remains the write target for backward compat; the receipt-post-completion gate continues to read it. No behavior change for existing repos.
6. Migration: existing `.dynos/events.jsonl` is split lazily — when a Tier-1 task's gate first reads its events, the consumer scans project-global once and copies the relevant per-task entries (those whose HMAC verifies under the task's key) into `<task_dir>/events.jsonl`. Subsequent reads skip the project-global scan. CLI `python3 hooks/ctl.py events migrate-task <task_dir>` runs the same migration explicitly.

**Out of scope for Phase 3:** Off-host append-only log shipping (Tier 2). Byzantine fault tolerance for log replication (Tier 3). Real-time intrusion detection on the event stream (Tier 2).

**Audit gate:** The adversarial cross-task forgery test must pass. The H1 residual is then formally closed in `memory/project_open_trust_residuals.md`.

### Phase 4 — Compliance-tier configuration plumbing

**Goal:** Make Tier 1 opt-in with a single config flag.

**Acceptance criteria:**

1. `~/.dynos/projects/<slug>/policy.json` gains a `compliance_tier: "tier-0" | "tier-1"` field. Default when absent: `"tier-0"`.
2. `hooks/lib_core.py::get_compliance_tier(root: Path) -> str` reads the field, returns `"tier-0"` if absent or invalid. All Tier-1-gated code consults this function — no scattered conditionals.
3. New CLI `python3 hooks/ctl.py compliance-tier set tier-1` performs the migration end-to-end:
   - Verifies platform support (Phase 1's `sandbox-test` passes).
   - Migrates the secret to the keychain (Phase 2).
   - Splits the events log lazily on next task run (Phase 3).
   - Writes the policy.json field.
   - Emits a one-shot summary to stdout: what changed, how to revert.
4. New CLI `python3 hooks/ctl.py compliance-check` runs all Tier-relevant self-tests (sandbox enforces denials, secret is keychain-anchored, per-task events isolated) and prints a JSON status report. Exit non-zero on any check failing under the active tier.
5. Reverting from Tier 1 to Tier 0 is supported via `compliance-tier set tier-0`. The keychain entry is left in place (idempotent), the events log is not un-split (existing per-task files remain valid), and the policy.json is updated.

---

## 4. What this spec is NOT

- It is NOT a hardening of dynos-work-as-it-exists for high-assurance contexts. The full program for that is Tier 2/3 (signed artifacts, two-actor approval, off-host append-only log, confidential computing, post-quantum crypto, formal verification, red team). Anything beyond Tier 1 lives in a separate spec for proprietary forks or downstream accreditation work.
- It does NOT close A4 (supply-chain), A5 (distribution), or A6 (insider). These are explicitly deferred to Tier 2.
- It does NOT add UX friction to the Tier 0 default. A regular OSS user running `/dynos-work:start` on their laptop sees the same workflow they see today.

---

## 5. Out of Scope (Tier 2+ punt list)

Tracked here so future operators / forks know what's deferred:

- **Signed learned-agents and prevention rules** (closes A4 / audit C3). Requires Ed25519 keypair management + trusted-signers list. Tier 2.
- **Two-actor approval for cross-task influence** (closes A6 in regulated contexts). Requires hardware-key tap + second human signer. Tier 2.
- **Off-host append-only log shipping** (closes H1 fully + A6 detection). Requires external log collector (Splunk HEC, S3 with object-lock, ZFS append-only mount). Tier 2.
- **Hardware-rooted secret** (TPM 2.0 / YubiKey HMAC / HSM-backed PKCS#11). Replaces OS-keychain. Tier 2.
- **Confidential computing** (Intel TDX / AMD SEV-SNP enclaves for the most sensitive paths). Tier 3.
- **Post-quantum crypto** (ML-KEM, ML-DSA per NIST PQC). Tier 3.
- **Formal verification** (TLA+ for the protocol, F* / Coq / Lean for critical functions). Tier 3.
- **N-version programming** for critical paths. Tier 3.
- **SBOM, reproducible builds, Sigstore-signed releases** (closes A5). Tier 2 — could ship in OSS later as a separate hardening but not part of this spec.

---

## 6. Migration Path

### For an existing OSS user

1. Pull the latest dynos-work.
2. No action required. Tier 0 is the default. Existing repos run unchanged.

### For a user opting into Tier 1

1. Run `python3 hooks/ctl.py compliance-tier set tier-1`.
2. The CLI runs platform support checks and migrates state.
3. Existing tasks remain readable; their gates continue to work.
4. New tasks run under the kernel sandbox + keychain secret + per-task events log.

### Reverting

`python3 hooks/ctl.py compliance-tier set tier-0`. State is preserved; behavior reverts.

---

## 7. Risk Notes

- **R1 — macOS sandbox is best-effort.** `sandbox-exec` is deprecated and Apple has not provided a public successor. Tier 1 on macOS uses it anyway because nothing else covers the use case. We document this gap and recommend Linux for the strongest Tier 1 posture. Future: explore Virtualization.framework lightweight VM if `sandbox-exec` is removed in a future macOS release.
- **R2 — Headless Linux without D-Bus.** Some CI runners and bare-metal servers lack a Secret Service / libsecret backend. Phase 2's keychain resolution falls through; user must either (a) provide `DYNOS_EVENT_SECRET` env var or (b) install `gnome-keyring` headless mode or (c) stay on Tier 0. Documented in `docs/secret-anchor.md`.
- **R3 — Performance regression on per-task events.** Phase 3's per-task file means N small files instead of one large one. Filesystem seek overhead on cross-task aggregation. Acceptable for OSS workloads (hundreds-to-thousands of tasks per project). Tier 3 will address with an indexed log store; Tier 1 accepts the simple file-per-task design.
- **R4 — Sandbox false positives.** A Phase 1 sandbox that's too tight will block legitimate executor work (e.g., writing a test file under `tests/`). The role-specific allowlist must be wide enough to cover every legitimate write path documented in `write_policy.py`, OR Tier 1 adoption stalls. Mitigation: the allowlist is materialized from `write_policy.py`, which already has the correct rules; Phase 1 is a translation, not a re-derivation.
- **R5 — Backward compat is load-bearing.** Tier 0 must keep working for existing repos and existing test suites. Every Tier 1 change is gated by `get_compliance_tier()` checks. A Tier 0 regression in CI is a release blocker.
- **R6 — A4–A6 still unmitigated at Tier 1.** A user adopting Tier 1 because they "want it secure" may believe they're protected against supply-chain or insider attack. They are not. The README and the `compliance-tier set tier-1` CLI output must explicitly state which threat classes Tier 1 closes (A1–A3) and which it does NOT (A4–A6).

---

## 8. Decomposition into Tasks

| Task | Phase | Effort | Order |
|------|-------|--------|-------|
| `phase-1-sandbox` | 1 | High (kernel work, per-platform) | 1 |
| `phase-2-secret-anchor` | 2 | Medium | 2 |
| `phase-3-per-task-events` | 3 | Medium | 3 |
| `phase-4-tier-cli` | 4 | Low | 4 (after 1–3) |

Phase 1 should land first because Phase 2 and 3's adversarial tests rely on the sandbox being mechanically enforced — otherwise the tests can be defeated by the same bypass they're trying to close. Phase 4 wires the user-facing flag last so partial implementations can't be enabled accidentally.

---

*End of Tier 0/1 OSS spec. Tier 2+ specs are produced separately by downstream / proprietary forks who need accreditation.*
