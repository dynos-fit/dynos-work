# dynos-work — Function-by-function workflow trace

For every Python function the pipeline touches: the **code**, then in prose — **what it does**, **when it fires**, **why**, and **what it hands off to**. Ordered by the workflow (boot → start → execute → audit → post-completion → memory → calibration → daemon).

---

## Adaptive algorithms index

Every place the system uses a statistical or RL-inspired algorithm, marked with the algorithm name in **bold** throughout the document:

| Algorithm | Where | What it decides | File |
|---|---|---|---|
| **UCB1 (Upper Confidence Bound)** | Model selection | Which model (opus/sonnet/haiku) to assign each agent | `router.py:resolve_model` |
| **EMA (Exponential Moving Average)** | Effectiveness scoring | Per-(role, model, task_type, source) quality/cost/efficiency tracking | `memory/patterns.py:_compute_ema` |
| **Weighted composite scoring** | Model policy, skip policy | `0.5*quality + 0.3*cost + 0.2*efficiency` ranks model candidates | `memory/patterns.py:_derive_model_policy` |
| **Tabular Q-learning** | Repair planning | Which executor + model to assign per finding type | `hooks/lib_qlearn.py:build_repair_plan` |
| **Epsilon-greedy exploration** | Q-learning action selection | Balance exploit (best Q-value) vs explore (random action) | `hooks/lib_qlearn.py:select_action` |
| **Bellman update** | Q-value update | `Q(s,a) += α * (r + γ * max Q(s',a') - Q(s,a))` | `hooks/lib_qlearn.py:update_q_value` |
| **EMA baseline blend-back** | Regression detection | Pull EMA toward baseline on 2+ consecutive quality drops | `memory/patterns.py:_compute_ema` |
| **Adaptive skip threshold** | Auditor skipping | `threshold = clamp(3 + 2*(1 - avg_quality), 1, 10)` | `memory/patterns.py:_derive_skip_policy` |
| **MCTS-lite (Monte Carlo Tree Search)** | Design option scoring | Sandbox simulation with rollout noise for founder design review | `memory/dream.py:run_mcts` |
| **Trajectory similarity search** | Discovery priors | Cosine-like similarity over (type, domains, risk, repair, spawns) | `memory/lib_trajectory.py:search_trajectories` |

---

## PART 0 — BOOT: when a Claude Code session opens

Claude Code loads `.claude-plugin/plugin.json`, reads `hooks.json`, and registers three event hooks: `SessionStart`, `TaskCompleted`, `SubagentStop`. The `SessionStart` hook matcher is `startup|clear|compact` so it fires on every fresh session / `/clear` / auto-compact.

### hooks/session-start (bash wrapper)

```bash
python3 "${SCRIPT_DIR}/registry.py"  set-active "${PROJECT_ROOT}" >/dev/null 2>&1 || true
python3 "${SCRIPT_DIR}/dashboard.py" generate   --root "${PROJECT_ROOT}" >/dev/null 2>&1 || true
python3 "${SCRIPT_DIR}/daemon.py"  ensure     --root "${PROJECT_ROOT}" >/dev/null 2>&1 || true
# …then inline Python to build .dynos/routing-context.json
```

**When it fires:** every session boot / clear / compact.
**Why:** touch the registry (so the project shows "recently active" globally), regenerate the dashboard so users see fresh data, conditionally start the local maintenance daemon, and pre-cache routing policy so Claude has it inside the session prompt.

Each of those three Python calls is a standalone function.

---

### registry.cmd_set_active

```python
def cmd_set_active(args: argparse.Namespace) -> int:
    """Update last_active_at to current ISO timestamp. Silently exits 0 if not registered."""
    root = Path(args.path).resolve()
    try:
        reg = load_registry()
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1
    abs_path = str(root)
    entry = None
    for proj in reg.get("projects", []):
        if proj.get("path") == abs_path:
            entry = proj
            break
    if entry is None:
        return 0  # silently no-op if project isn't registered
    entry["last_active_at"] = now_iso()
    try:
        save_registry(reg)
    except OSError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1
    log_global(f"CLI: set-active for project {root}")
    print(json.dumps({"set_active": str(root), "last_active_at": entry["last_active_at"]}, indent=2))
    return 0
```

**What it does:** opens `~/.dynos/registry.json`, finds the entry whose `path` matches the project root, bumps `last_active_at` to now, saves. If the project isn't registered it does nothing and exits 0 — deliberately silent so fresh clones don't spam errors.

**When:** on every session start.
**Why:** so the global sweeper daemon (and the global dashboard) can prioritize active projects over dormant ones. The sweeper uses `last_active_at` for exponential backoff on idle repos.
**Hands off to:** nothing — terminal.

---

### dashboard.py generate

**What it does:** reads every `.dynos/task-*/manifest.json`, `task-retrospective.json`, `token-usage.json`, and audit reports. Aggregates quality trends, cost, open findings, learned route state. Writes `.dynos/dashboard-data.json` and a rendered `.dynos/dashboard.html` (HTML template now lives in `hooks/dashboard-ui/`).

**When:** session start; after every event-bus drain; every maintenance cycle.
**Why:** the dashboard must always be fresh because the user might open it right after starting a session. Errors are swallowed (`|| true` in bash) so a bad JSON never blocks the hook.
**Hands off to:** `.dynos/dashboard-data.json` (consumed by `/dynos-work:dashboard` and the served HTML).

---

### daemon.cmd_ensure

```python
def cmd_ensure(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    policy = maintainer_policy(root)
    if not policy.get("maintainer_autostart", False):
        print(json.dumps({"status": "autostart_disabled"}, indent=2))
        return 0
    if current_pid(root) is not None:
        print(json.dumps({"status": "already_running", "pid": current_pid(root)}, indent=2))
        return 0
    start_args = argparse.Namespace(root=str(root), poll_seconds=policy["maintainer_poll_seconds"])
    return cmd_start(start_args)
```

**What it does:** idempotent guard. Reads `~/.dynos/projects/{slug}/policy.json` via `maintainer_policy()`. If `maintainer_autostart` is false, exits. If a daemon PID is already alive, exits. Otherwise delegates to `cmd_start()` which spawns a detached daemon.

**When:** every session boot.
**Why:** the local daemon runs background maintenance — pattern refresh, benchmark sweeps, proactive findings. It should be running *but only if the user opted in* (via `dynos init` or setting `maintainer_autostart: true` in `policy.json`).
**Hands off to:** `daemon.cmd_start` → `daemon.cmd_run_loop` → `daemon.maintenance_cycle` (see PART 8).

---

### daemon.maintainer_policy

```python
def maintainer_policy(root: Path) -> dict:
    default = {
        "maintainer_autostart": False,
        "maintainer_poll_seconds": 3600,
    }
    path = policy_path(root)
    data: dict = {}
    if path.exists() and path.read_text().strip():
        try:
            data = load_json(path)
        except (json.JSONDecodeError, OSError):
            data = {}
    # Merge defaults into existing data without clobbering other keys
    merged = {**data}
    for k, v in default.items():
        if k not in merged:
            merged[k] = v
    if not path.exists() or not data:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, merged)
    if merged != data:
        path.write_text(json.dumps({**data, **merged}, indent=2) + "\n")
    return merged
```


**What it does:** loads daemon-specific settings from the shared `policy.json`; tops up defaults without touching other namespaces.
**When:** inside `cmd_ensure`, `cmd_start`, and `cmd_run_loop` whenever poll interval is needed.
**Why:** autostart is opt-in, but now other subsystems' settings survive a read from this helper.

---

### inline: `router.load_prevention_rules` and `lib_core.project_policy` (pre-cache routing context)

After the three Python one-liners, the bash hook runs an inline Python script:

```python
from router import load_prevention_rules
from lib_core import project_policy, _persistent_project_dir
from pathlib import Path
root = Path('${PROJECT_ROOT}')
policy = project_policy(root)
rules = load_prevention_rules(root)
ctx = {
    'policy': {k: v for k, v in policy.items() if k in ('token_budget_multiplier', 'fast_track_skip_plan_audit', 'model_overrides')},
    'prevention_rules': len(rules),
    'has_learned_agents': (_persistent_project_dir(root) / 'learned-agents' / 'registry.json').exists(),
}
Path('${PROJECT_ROOT}/.dynos/routing-context.json').write_text(json.dumps(ctx, indent=2))
```

#### lib_core.project_policy

```python
def project_policy(root: Path) -> dict:
    """Read project policy from persistent dir. Merges defaults without clobbering existing keys."""
    path = _persistent_project_dir(root) / "policy.json"
    default: dict[str, Any] = {
        "freshness_task_window": 5,
        "active_rebenchmark_task_window": 3,
        "shadow_rebenchmark_task_window": 2,
        "token_budget_multiplier": 1.0,
        "fast_track_skip_plan_audit": False,
    }
    data: dict = {}
    if path.exists() and path.read_text().strip():
        try:
            data = load_json(path)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            data = {}
    # Merge: existing keys take precedence over defaults
    merged = {**default, **data}
    # Only write if file doesn't exist yet (never overwrite existing keys)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, merged)
    return merged
```


**What it does:** returns the merged project policy (defaults ⨁ file). Creates the file only if missing.
**When:** every time any code needs to know project policy — the router calls it for every model/skip/route decision, the session-start hook calls it to build the context blob, `/start` calls it during fast-track gating, etc.
**Why:** centralizes all user overrides in one place; no longer destroys data on corruption.
**Hands off to:** caller uses the returned dict.

#### router.load_prevention_rules

```python
def load_prevention_rules(root: Path) -> list[dict]:
    """Load project-local prevention rules from persistent storage."""
    rules_path = _persistent_project_dir(root) / "prevention-rules.json"
    if not rules_path.exists():
        return []
    try:
        data = load_json(rules_path)
        return data.get("rules", [])
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        return []
```

**What it does:** reads the list of prevention rules — short strings like "always validate JWT before decode" — accumulated across prior tasks from finding categories. Returns `[]` on any error.

**When:** every session start (count goes in the context blob), every executor spawn (injected into the prompt).
**Why:** lets the router inject learned "avoid this" patterns into every executor prompt. The actual crash safety and filtering now lives in `router.build_executor_plan`, not here — `load_prevention_rules` is intentionally permissive.
**Hands off to:** `router.build_executor_plan` filters them per-executor.

---

## PART 1 — USER TYPES `/dynos-work:start`

The skill file `skills/start/SKILL.md` is invoked. The skill is instructions for Claude — not Python — but every step drops into Python via a subprocess or agent spawn.

### Step 0 — Metadata & Initialization

Claude creates `.dynos/task-{id}/manifest.json`, `raw-input.md`, `execution-log.md`, then runs:

```bash
python3 hooks/registry.py register "$(pwd)"
python3 hooks/daemon.py  start --root "$(pwd)"
python3 hooks/ctl.py       validate-task .dynos/task-{id}
```

#### registry.cmd_register

```python
def cmd_register(args: argparse.Namespace) -> int:
    """Register a project in the global registry."""
    raw_path = args.path
    root = Path(raw_path).resolve()

    if not root.is_dir():
        print(json.dumps({"error": f"path is not a directory: {root}"}, indent=2), file=sys.stderr)
        return 1
    str_root = str(root)
    if str_root.startswith("/tmp/") or str_root.startswith("/var/tmp/"):
        print(json.dumps({"error": f"refusing to register temporary directory: {root}"}, indent=2), file=sys.stderr)
        return 1

    dynos_dir = root / ".dynos"
    if not dynos_dir.is_dir():
        print(json.dumps({"error": f".dynos/ directory not found in {root}"}, indent=2), file=sys.stderr)
        return 1

    try:
        reg = register_project(root)   # from sweeper.py
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    entry = next((p for p in reg.get("projects", []) if p.get("path") == str(root)), None)
    print(json.dumps({"registered": str(root), "entry": entry}, indent=2))
    return 0
```

**What it does:** adds the project to `~/.dynos/registry.json` and creates the persistent state directory `~/.dynos/projects/{slug}/`. Refuses to register `/tmp/` paths (prevents test fixtures from polluting the real registry). Idempotent.

**When:** first call is from `session-start` (silent). The start skill calls it again to make registration explicit. Any subsequent call is a no-op.
**Why:** the registry is where the global daemon learns which projects exist.
**Hands off to:** writes to `~/.dynos/registry.json`, creates `~/.dynos/projects/{slug}/`.

#### daemon.cmd_start

```python
def cmd_start(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    lock_file = maintenance_dir(root) / "start.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(lock_file, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fd.close()
        print(json.dumps({"status": "start_in_progress"}, indent=2))
        return 0
    try:
        existing = current_pid(root)
        if existing is not None:
            print(json.dumps({"status": "already_running", "pid": existing}, indent=2))
            return 0
        hooks_dir = Path(__file__).resolve().parent
        poll_seconds = int(args.poll_seconds or maintainer_policy(root)["maintainer_poll_seconds"])
        cmd = ["python3", str(hooks_dir / "daemon.py"), "run-loop",
               "--root", str(root), "--poll-seconds", str(poll_seconds)]
        process = subprocess.Popen(cmd, cwd=root,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True)
        time.sleep(0.2)
        print(json.dumps({"status": "started", "pid": process.pid, "poll_seconds": poll_seconds}, indent=2))
        return 0
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
```

**What it does:** acquires a non-blocking `flock` on `start.lock` so concurrent `start` calls can't race. If another process is starting, exits cleanly. If a daemon is already alive, exits cleanly. Otherwise spawns `daemon.py run-loop` in a detached process group (survives parent session exit) with stdio redirected to /dev/null.

**When:** from the start skill (ensures the daemon is running before a task begins), and from `cmd_ensure` if autostart is enabled.
**Why:** daemon-side work (shadow benchmarks, proactive findings) should be running in the background.
**Hands off to:** the new process runs `cmd_run_loop` (PART 8).

#### ctl.cmd_validate_task

```python
def cmd_validate_task(args: argparse.Namespace) -> int:
    errors = validate_task_artifacts(Path(args.task_dir).resolve(), strict=args.strict)
    if errors:
        print("Validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Validation passed.")
    return 0
```

**What it does:** thin wrapper — delegates to `lib_validate.validate_task_artifacts()`. Prints errors and exits 1 if anything's wrong.
**When:** Step 0 calls it non-strict (only manifest needs to exist). Step 5 calls it strict (plan + graph required). The audit skill calls it before DONE.
**Why:** one entry point for all artifact validation keeps rules centralized in `lib_validate.py`.
**Hands off to:** `validate_task_artifacts()` (Step 5).

### Step 1 — Discovery Intake

No Python. Skill reads `raw-input.md`, optionally reads `.dynos/trajectories.json` (only if `learning_enabled=true`), generates up to 5 questions via `AskUserQuestion`, writes Q&A to `discovery-notes.md`.

### Step 2 — Discovery + Design + Classification

Before spawning the planner, the skill checks for a learned planning skill:

```bash
python3 -c "from router import resolve_route; r = resolve_route(Path('.'), 'plan-skill', '{task_type}'); print(r['agent_path'] or '')"
```

#### router.resolve_route

```python
def resolve_route(root: Path, role: str, task_type: str) -> dict:
    """Determine whether to use generic, learned, or alongside agent."""
    if not is_learning_enabled(root):
        result = {"mode": "generic", "agent_path": None, "agent_name": None,
                  "composite_score": 0.0, "source": "learning_enabled=false"}
        log_event(root, "router_route_decision", role=role, task_type=task_type,
                  mode="generic", agent_name=None, composite_score=0.0, source="learning_enabled=false")
        return result

    registry = ensure_learned_registry(root)
    agents = registry.get("agents", [])

    learned = None
    for agent in agents:
        if (agent.get("role") == role
            and agent.get("task_type") == task_type
            and agent.get("status") not in ("archived", "demoted_on_regression")):
            learned = agent
            break

    if not learned:
        result = {"mode": "generic", "agent_path": None, "agent_name": None,
                  "composite_score": 0.0, "source": "no learned agent"}
        log_event(root, "router_route_decision", ...)
        return result

    mode = learned.get("mode", "shadow")
    composite = float(learned.get("benchmark_summary", {}).get("mean_composite", 0.0) or 0.0)
    agent_path = learned.get("path", "")
    agent_name = learned.get("agent_name", "")

    # Security-auditor can never be replaced by a learned agent
    if role == "security-auditor" and mode == "replace":
        mode = "alongside"

    # Shadow mode means not yet proven — use generic
    if mode == "shadow":
        return {"mode": "generic", "agent_path": agent_path, "agent_name": agent_name,
                "composite_score": composite, "source": f"shadow (not yet promoted): {agent_name}"}

    # Path resolution against persistent dir (with fallbacks)
    # ... (finds learned agent .md file, returns generic if not on disk)

    return {"mode": mode, "agent_path": agent_path, "agent_name": agent_name,
            "composite_score": composite, "source": f"learned:{agent_name}"}
```

**What it does:** decides whether to use (a) the generic built-in agent, (b) a learned agent in `replace` mode, or (c) one in `alongside` mode. Reads `~/.dynos/projects/{slug}/learned-agents/registry.json`, finds the entry matching `(role, task_type)` with `status` not in `{archived, demoted_on_regression}`, and reads its `mode`.

Three safety guards:
1. **Security floor** — `security-auditor` in `replace` gets demoted to `alongside`.
2. **Learning disabled** — opt-out returns `generic`.
3. **Missing file** — registry points at a file that's gone → falls back to `generic`.

**When:** every executor spawn, every auditor spawn, every learned-skill check.
**Why:** the router is the single source of truth for "which agent runs this next spawn".
**Hands off to:** returned `agent_path` gets injected into the spawn prompt by `build_executor_prompt` / `build_auditor_prompt` (PART 2 & 3).

---

Then Claude spawns the `planning` subagent (`agents/planning.md`, model=opus) with Phase: Discovery + Design + Classification. It writes the classification JSON back into `manifest.json`. Then:

```bash
python3 hooks/ctl.py transition .dynos/task-{id} SPEC_NORMALIZATION
```

#### ctl.cmd_transition

```python
def cmd_transition(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        previous, manifest = transition_task(task_dir, args.next_stage, force=args.force)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{manifest['task_id']}: {previous} -> {manifest['stage']}")
    return 0
```

Thin wrapper. Delegates to `lib_core.transition_task`.

#### lib_core.transition_task

```python
def transition_task(task_dir: Path, next_stage: str, *, force: bool = False) -> tuple[str, dict]:
    """Transition a task to a new stage, enforcing allowed transitions."""
    manifest_path = task_dir / "manifest.json"
    manifest = load_json(manifest_path)
    current_stage = manifest.get("stage")
    if next_stage not in ALLOWED_STAGE_TRANSITIONS:
        raise ValueError(f"Unknown stage: {next_stage}")
    if not force and next_stage not in ALLOWED_STAGE_TRANSITIONS.get(current_stage, set()):
        raise ValueError(f"Illegal stage transition: {current_stage} -> {next_stage}")

    # ---- Receipt-based transition gates (unmissable) ----
    if not force:
        from lib_receipts import read_receipt, validate_chain
        gate_errors: list[str] = []

        if next_stage == "EXECUTION":
            if read_receipt(task_dir, "plan-validated") is None:
                gate_errors.append("receipt: plan-validated (plan was never validated)")

        if next_stage == "CHECKPOINT_AUDIT":
            if read_receipt(task_dir, "executor-routing") is None:
                gate_errors.append("receipt: executor-routing (executor routing was never recorded)")

        # REPAIR_PLANNING: deterministic hard cap at 3 retries per finding
        MAX_REPAIR_RETRIES = 3
        if next_stage == "REPAIR_PLANNING" and current_stage == "REPAIR_EXECUTION":
            repair_log_path = task_dir / "repair-log.json"
            if repair_log_path.exists():
                repair_log = load_json(repair_log_path)
                exhausted: list[str] = []
                for batch in repair_log.get("batches", []):
                    for task_entry in batch.get("tasks", []):
                        fid = task_entry.get("finding_id", "unknown")
                        retries = task_entry.get("retry_count", 0)
                        if retries >= MAX_REPAIR_RETRIES:
                            exhausted.append(f"{fid} (retry_count={retries})")
                if exhausted:
                    gate_errors.append(
                        f"repair cap exceeded (max {MAX_REPAIR_RETRIES} retries) "
                        f"for finding(s): {', '.join(exhausted)}. "
                        f"Transition to FAILED instead — human review needed."
                    )

        # DONE requires retrospective + audit reports (post-completion receipt
        # is written AFTER the transition by the event bus, so cannot be gated here)
        if next_stage == "DONE":
            if not (task_dir / "task-retrospective.json").exists():
                gate_errors.append("task-retrospective.json (run /dynos-work:audit to generate)")
            if not list(task_dir.glob("audit-reports/*.json")):
                gate_errors.append("audit-reports/ (no audit reports found — audit was never run)")
            if read_receipt(task_dir, "retrospective") is None:
                gate_errors.append("receipt: retrospective (reward was never computed via receipts)")

        if gate_errors:
            raise ValueError(
                f"Cannot transition to {next_stage} — missing required artifacts:\n"
                + "\n".join(f"  - {e}" for e in gate_errors)
            )

    manifest["stage"] = next_stage
    if next_stage == "DONE":
        manifest["completion_at"] = now_iso()
    if next_stage == "FAILED" and manifest.get("blocked_reason") is None:
        manifest["blocked_reason"] = "transitioned to FAILED"
    write_json(manifest_path, manifest)

    _auto_log(task_dir, current_stage, next_stage, force)

    from lib_log import log_event
    log_event(task_dir.parent.parent, "stage_transition",
              task=manifest["task_id"], from_stage=current_stage,
              to_stage=next_stage, forced=force)

    try:
        from lib_tokens import record_tokens, phase_for_stage
        record_tokens(task_dir=task_dir, agent="transition_task", model="none",
                      input_tokens=0, output_tokens=0,
                      phase=phase_for_stage(next_stage), stage=next_stage,
                      event_type="stage-transition",
                      detail=f"{current_stage} → {next_stage}" + (" (forced)" if force else ""))
    except Exception:
        pass

    if next_stage == "DONE":
        _fire_task_completed(task_dir)

    return current_stage, manifest
```


The `post-completion` receipt is still written by the event bus, just no longer as a precondition.

**What it does:** the *only* function in the entire system that changes a task's stage. Five things, in order:
1. Structural check against `ALLOWED_STAGE_TRANSITIONS`.
2. Receipt gates: EXECUTION needs `plan-validated`, CHECKPOINT_AUDIT needs `executor-routing`, DONE needs retrospective artifacts + receipt.
3. Repair hard cap: 3 retries per finding, enforced in code.
4. Writes `manifest.json`, appends `execution-log.md` via `_auto_log`, records token-ledger event, logs to `events.jsonl`.
5. On DONE, calls `_fire_task_completed` which emits `task-completed` and drains the event bus.

**When:** every stage transition.
**Why:** illegal flows are impossible. The fix makes DONE actually reachable on first try.
**Hands off to:** on DONE, `_fire_task_completed` → `lib_events.emit_event` → `eventbus.drain` → the memory/calibration/benchmark/dashboard pipeline (PART 4).

#### lib_core._fire_task_completed

```python
def _fire_task_completed(task_dir: Path) -> None:
    """Run the post-completion pipeline: event emit → drain (learn, postmortem, evolve, etc).

    This is the ONLY place that fires the pipeline. Never swallow errors silently
    — print them but don't block the transition.
    """
    import subprocess

    root = task_dir.parent.parent
    hooks_dir = root / "hooks"
    env_path = f"{hooks_dir}:{__import__('os').environ.get('PYTHONPATH', '')}"

    # Step 1: Emit task-completed event with task identity
    task_id = task_dir.name
    payload = json.dumps({"task_id": task_id, "task_dir": str(task_dir)})
    try:
        subprocess.run(
            ["python3", str(hooks_dir / "lib_events.py"), "emit",
             "--root", str(root), "--type", "task-completed", "--source", "task",
             "--payload", payload],
            env={**__import__("os").environ, "PYTHONPATH": env_path},
            capture_output=True, text=True, timeout=10)
    except Exception as exc:
        print(f"[dynos] event emit failed: {exc}")

    # Step 2: Drain all events
    try:
        result = subprocess.run(
            ["python3", str(hooks_dir / "eventbus.py"), "drain", "--root", str(root)],
            env={**__import__("os").environ, "PYTHONPATH": env_path},
            capture_output=True, text=True, timeout=120)
        if result.stdout.strip():
            print(f"[dynos] post-completion pipeline: {result.stdout.strip()}")
    except Exception as exc:
        print(f"[dynos] event drain failed: {exc}")
```


**What it does:** fires the post-completion pipeline in-process: emit typed event with task identity, drain up to 120s.
**When:** every DONE transition.
**Why:** with the payload now carrying task identity, the drain writes the `post-completion` receipt to the *correct* task even under concurrent completions.
**Hands off to:** `lib_events.emit_event` (PART 4.1) and `eventbus.drain` (PART 4.2).

### Step 2b — Fast-Track Gate

```python
def compute_fast_track(manifest: dict) -> bool:
    classification = manifest.get("classification")
    if not isinstance(classification, dict):
        return False
    if classification.get("risk_level") != "low":
        return False
    domains = classification.get("domains", [])
    if not isinstance(domains, list) or len(domains) != 1:
        return False
    return True


def apply_fast_track(task_dir: Path) -> bool:
    """Check fast-track eligibility and write to manifest. Returns True if fast-tracked."""
    manifest_path = task_dir / "manifest.json"
    manifest = load_json(manifest_path)
    fast = compute_fast_track(manifest)
    manifest["fast_track"] = fast
    write_json(manifest_path, manifest)
    return fast
```

**What it does:** returns True iff `risk_level == "low"` AND exactly one domain. Writes `manifest.fast_track`.
**When:** Step 2b, right after classification.
**Why:** fast-track tasks skip hierarchical planning, use only two auditors, may skip plan audit entirely. Gate is binary, in Python, can't be gamed by the LLM.
**Hands off to:** start skill reads `manifest.fast_track` for hierarchical-vs-standard planning; audit skill reads it to limit auditors.

### Step 3 — Spec Normalization

Planning agent spawn with `Phase: Spec Normalization`. Writes `spec.md`. Skill validates headings and AC numbering. On success: `ctl.py transition … SPEC_REVIEW`.

### Step 4 — Spec Review (HUMAN GATE)

`AskUserQuestion` approval. On approval → `ctl.py transition … PLANNING`.

### Step 5 — Plan + Execution Graph

Hierarchical vs standard mode chosen by `hooks/planner.py` (new deterministic helper; see below). Planning agent writes `plan.md` and `execution-graph.json`. Then:

```bash
python3 hooks/validate_task_artifacts.py .dynos/task-{id}
```

#### **NEW: hooks/planner.py** (deterministic planning decisions)

Added in this cycle. Three subcommands:

```python
def cmd_start_plan(args): ...       # resolve deterministic start/spec/planning decisions
def cmd_planning_mode(args): ...    # standard vs hierarchical
def cmd_task_policy(args): ...      # generate policy-packet.json for a task
```

**What it does:** pulls together `collect_retrospectives`, `load_prevention_rules`, `resolve_model/skip/route` (from router), and `build_audit_plan` to produce a single JSON blob the start skill reads. Does inline trajectory lookup (`_find_similar_trajectories`, `_trajectory_adjustments`) to surface adjacent-task risk signals.

**When:** called by `/start` during Step 2 and Step 5 to avoid the skill making derived decisions in prose.
**Why:** the skill shouldn't re-derive hierarchical-vs-standard from spec text — that's deterministic (risk_level high/critical OR >10 ACs). Centralizes planning decisions like the router centralizes routing.
**Hands off to:** skill consumes the JSON and proceeds.

#### validate_task_artifacts.main

```python
def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip())
        return 2

    task_dir = Path(sys.argv[1]).resolve()
    errors = validate_task_artifacts(task_dir, strict=True)

    # Auto-emit deterministic validation event to per-task token ledger
    try:
        from lib_tokens import record_tokens, phase_for_stage
        from lib_core import load_json
        manifest = load_json(task_dir / "manifest.json")
        stage = manifest.get("stage", "")
        record_tokens(
            task_dir=task_dir, agent="validate_task_artifacts", model="none",
            input_tokens=0, output_tokens=0, phase=phase_for_stage(stage),
            stage=stage, event_type="deterministic",
            detail=f"{'PASS' if not errors else 'FAIL'} — {len(errors)} error(s)"
                   + (f": {errors[0]}" if errors else ""))
    except Exception:
        pass

    if errors:
        print("Artifact validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    # Auto-emit plan-validated receipt when execution graph exists
    try:
        from lib_receipts import receipt_plan_validated
        from lib_core import load_json as _load_json
        graph_path = task_dir / "execution-graph.json"
        spec_path = task_dir / "spec.md"
        if graph_path.exists() and spec_path.exists():
            graph = _load_json(graph_path)
            segments = graph.get("segments", [])
            all_criteria: list[int] = []
            for seg in segments:
                for cid in seg.get("criteria_ids", []):
                    if cid not in all_criteria:
                        all_criteria.append(cid)
            receipt_plan_validated(task_dir,
                segment_count=len(segments), criteria_coverage=sorted(all_criteria))
    except Exception:
        pass

    print(f"Artifact validation passed for {task_dir}")
    return 0
```

**What it does:** CLI entry. Runs `validate_task_artifacts` in strict mode. On success, *auto-writes the `plan-validated` receipt* — this is what unlocks `EXECUTION`.
**When:** Step 5 plan generation; after every replan.
**Why:** validation is the single choke point for "is the plan OK to hand to executors". Auto-emitting the receipt means the skill can't pass validation and then skip execute — the executor gate opens only when this succeeds.
**Hands off to:** `validate_task_artifacts` + `receipt_plan_validated` + `record_tokens`.

#### lib_validate.validate_task_artifacts

(Full code unchanged from prior trace. Schema summary:)

- manifest exists + parses + stage/classification valid enums
- spec.md has all required headings; acceptance criteria is a gap-free numbered list starting at 1
- plan.md has required headings (conditional `API Contracts` / `Data Model` based on domains); `## Reference Code` paths exist or are tagged "to-be-created"; runs `plan_gap_analysis.run_gap_analysis` to verify API / Data Model claims against real code
- execution-graph.json: unique segment IDs, valid executor type, no file double-ownership, acyclic `depends_on`, every AC covered
- repair-log.json + task-retrospective.json (if present) validate against their schemas

**Returns** a list of errors; empty = valid.

#### plan_gap_analysis.run_gap_analysis

(Full code unchanged from prior trace. Parses `## API Contracts` / `## Data Model` markdown tables, greps the codebase for route-regex and model patterns per framework, produces `{claimed_endpoints, verified, unverified}` and `{claimed_tables, verified, unverified}`. `findings_from_report` converts unverified entries to validation-error strings.)

**What it catches:** a planner hallucinating an endpoint (`POST /api/v2/users/batch`) that doesn't actually exist in the code (`POST /api/v1/users/bulk`). Early catch lets the planner fix the table or mark entries `to-be-created`.

#### lib_receipts.receipt_plan_validated + write_receipt

```python
def receipt_plan_validated(task_dir, segment_count, criteria_coverage, validation_passed=True):
    return write_receipt(task_dir, "plan-validated",
        segment_count=segment_count, criteria_coverage=criteria_coverage,
        validation_passed=validation_passed)


def write_receipt(task_dir: Path, step_name: str, **payload) -> Path:
    """Write a receipt proving a pipeline step completed."""
    receipts = _receipts_dir(task_dir)
    receipts.mkdir(parents=True, exist_ok=True)
    receipt = {"step": step_name, "ts": now_iso(), "valid": True, **payload}
    receipt_path = receipts / f"{step_name}.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, default=str))

    root = task_dir.parent.parent
    log_event(root, "receipt_written", task=task_dir.name, step=step_name)

    # Auto-append to execution-log.md via _LOG_MESSAGES template
    # (e.g. "[DONE] plan validated — 5 segments, criteria [1,2,3,4,5]")
    template = _LOG_MESSAGES.get(step_name)
    if template:
        # format and append via append_execution_log
        ...
    return receipt_path
```

**What it does:** `receipt_plan_validated` is one of a family of convenience writers (see `receipt_planner_spawn`, `receipt_plan_audit`, `receipt_tdd_tests`, `receipt_executor_routing`, `receipt_executor_done`, `receipt_audit_routing`, `receipt_audit_done`, `receipt_retrospective`, `receipt_post_completion`). All funnel into `write_receipt`, which creates the receipts dir, writes `{step_name}.json` with `{step, ts, valid: true, **payload}`, logs to `events.jsonl`, and appends a human line to `execution-log.md`.

**When:** immediately after each pipeline step.
**Why:** receipts are the unforgeable record. The state-machine gates in `transition_task` refuse to advance if required receipt files are absent. `valid: true` + file existence is the protocol.
**Hands off to:** next `transition_task` call reads them via `read_receipt`.

### Step 6 — Plan Review (HUMAN GATE)

No Python — skill logic.

### Step 7 — Plan Audit

Runs gap analysis explicitly and spawns `spec-completion-auditor` to verify the plan covers every acceptance criterion. Then:

```python
def receipt_plan_audit(task_dir, tokens_used, finding_count=0, model_used=None):
    """Write receipt proving plan audit (spec-completion check) ran. Also records tokens."""
    if tokens_used and tokens_used > 0:
        _record_tokens(task_dir, "plan-audit-check", model_used or "default", tokens_used)
    return write_receipt(task_dir, "plan-audit-check",
        tokens_used=tokens_used, finding_count=finding_count, model_used=model_used)
```

Records the plan-audit subagent's token usage (so effectiveness scoring knows the cost) and writes the receipt.

### Step 8 — TDD-First Gate

Spawns `testing-executor` with "TDD-First Mode". Writes test files + `.dynos/task-{id}/evidence/tdd-tests.md`. Skill validates every AC maps to ≥1 test. Then `receipt_tdd_tests`.

### Step 9 — Handoff to execute

```bash
python3 hooks/ctl.py transition .dynos/task-{id} PRE_EXECUTION_SNAPSHOT
```

`transition_task` accepts this (legal from `PLAN_AUDIT`). Task is now ready for `/dynos-work:execute`.

---

## PART 2 — USER TYPES `/dynos-work:execute`

### Step 1 — Contract check

```bash
python3 hooks/ctl.py validate-contract --skill execute --task-dir .dynos/task-{id}
```

#### ctl.cmd_validate_contract

```python
def cmd_validate_contract(args: argparse.Namespace) -> int:
    from lib_contracts import validate_inputs, validate_outputs
    task_dir = Path(args.task_dir).resolve()
    project_root = Path(args.root).resolve() if args.root else task_dir.parent.parent

    if args.direction == "input":
        errors = validate_inputs(args.skill, task_dir, project_root, strict=args.strict)
    elif args.direction == "output":
        errors = validate_outputs(args.skill, task_dir)
    else:
        errors = validate_inputs(args.skill, task_dir, project_root, strict=args.strict)
        errors.extend(validate_outputs(args.skill, task_dir))
    result = {"skill": args.skill, "valid": len(errors) == 0, "errors": errors}
    print(json.dumps(result, indent=2))
    return 1 if errors else 0
```

**What it does:** loads the skill's `contract.json`, verifies inputs match the declared schema.
**When:** first thing `/execute` does.
**Why:** clean error message instead of a cryptic failure mid-execution.

### Step 2 — Git snapshot

`git branch dynos/task-{id}-snapshot` if not already created.

### Step 3 — Inline vs spawn branch

**New in current main:** fast-track single-segment tasks can execute *inline* (no subagent spawn) to save the ~30K token overhead of agent context setup. Even inline, the skill must still call the router and apply learned agent rules.

### Step 4 — Build executor plan

```bash
python3 hooks/router.py executor-plan --root . --task-type {type} --graph .dynos/task-{id}/execution-graph.json
```

#### router.build_executor_plan

```python
def build_executor_plan(root: Path, task_type: str, segments: list[dict]) -> dict:
    """Build a complete, deterministic execution spawn plan."""
    all_rules = load_prevention_rules(root)
    plan = {"generated_at": now_iso(), "task_type": task_type, "segments": []}

    for seg in segments:
        executor = seg.get("executor", "")
        seg_id = seg.get("id", "")

        model_decision = resolve_model(root, executor, task_type)
        route_decision = resolve_route(root, executor, task_type)

        # Filter prevention rules relevant to this executor
        executor_rules = [
            r["rule"] for r in all_rules
            if isinstance(r, dict) and r.get("rule")
            and (not r.get("executor") or r.get("executor") == executor)
        ]

        plan["segments"].append({
            "segment_id": seg_id, "executor": executor,
            "model": model_decision["model"],
            "model_source": model_decision["source"],
            "route_mode": route_decision["mode"],
            "route_source": route_decision["source"],
            "agent_path": route_decision["agent_path"],
            "agent_name": route_decision["agent_name"],
            "composite_score": route_decision["composite_score"],
            "prevention_rules": executor_rules,
        })

    log_event(root, "router_executor_plan", ...)
    return plan
```


**What it does:** for each segment, ask router (1) which model? (2) generic, learned, or alongside? (3) which prevention rules apply? Returns flat list ready for spawning.
**When:** Step 4 of /execute.
**Why:** centralizes routing, and is now crash-safe against bad prevention-rules data.
**Hands off to:** skill spawns executor subagents per entry.

#### router.resolve_model — **UCB1 BANDIT**

Five priority tiers, first match wins:
1. **Explicit policy override** — `policy.json model_overrides`. No algorithm — user config.
2. **UCB1 (Upper Confidence Bound)** — `_ucb_select_model` reads effectiveness scores from `dynos_patterns.md`. For each model candidate, computes `ucb_score = composite + C * sqrt(ln(N) / n_i)` where `composite = 0.5*quality + 0.3*cost + 0.2*efficiency`, `N` = total observations across all models, `n_i` = observations for this model, `C` = exploration constant (default 0.5). Picks the model with highest UCB score. This naturally explores under-observed models while exploiting known-good ones.
3. **Benchmark model performance** — `_benchmark_model_for_agent` groups learned-agent benchmark runs by model. Uses mean composite from benchmark history.
4. **`model-policy.json`** — historical EMA winner (see memory/patterns.py).
5. **Patterns markdown table fallback**; else default (caller's frontmatter model).

`SECURITY_FLOOR_MODEL = "opus"` applies at every tier: security-auditor never below opus.

#### router.resolve_skip — **ADAPTIVE SKIP THRESHOLD**

For each auditor, if `role in {security-auditor, spec-completion-auditor, code-quality-auditor}`: never skip (exempt). Otherwise compare `auditor_zero_finding_streaks[auditor]` (from latest retrospective) against threshold from `skip-policy.json` or `dynos_patterns.md`. Skip if streak ≥ threshold. The threshold is derived by the EMA engine: `threshold = clamp(round(3 + 2*(1 - avg_quality_ema)), 1, 10)`. High-quality auditors skip after 3 consecutive zero-finding tasks; low-quality after up to 10.

### Step 5 — Prompt injection per segment

```bash
echo "{base prompt}" | python3 hooks/router.py inject-prompt \
  --root . --task-type {type} --graph .dynos/task-{id}/execution-graph.json \
  --segment-id {seg-id}
```

#### router.build_executor_prompt

```python
def build_executor_prompt(root, segment, plan_entry, base_prompt) -> str:
    """Build the complete executor prompt with learned agent rules injected."""
    agent_path = plan_entry.get("agent_path")
    route_mode = plan_entry.get("route_mode", "generic")
    agent_name = plan_entry.get("agent_name")
    prevention_rules = plan_entry.get("prevention_rules", [])
    parts = [base_prompt]

    if route_mode in ("replace", "alongside") and agent_path:
        p = Path(agent_path) if Path(agent_path).is_absolute() else root / agent_path
        if p.exists():
            agent_content = p.read_text().strip()
            if agent_content.startswith("---"):
                end = agent_content.find("---", 3)
                if end != -1:
                    agent_content = agent_content[end + 3:].strip()
            parts.append(
                f"\n\n## Learned Agent Instructions ({agent_name})\n"
                f"You are running as the **{agent_name}** learned agent (mode={route_mode}). "
                f"Follow these project-specific rules derived from past task analysis:\n\n"
                f"{agent_content}")
            log_event(root, "learned_agent_injected", ...)

    if prevention_rules:
        rules_text = "\n".join(f"- {r}" for r in prevention_rules)
        parts.append(
            f"\n\n## Prevention Rules\n"
            f"These patterns have caused audit findings in past tasks. Avoid them:\n{rules_text}")

    return "\n".join(parts)
```

**What it does:** takes the skill's generic executor prompt, appends the learned agent's content (if route is replace/alongside and file exists), appends prevention rules. Frontmatter is stripped — only the body goes in.
**When:** once per segment in /execute.
**Why:** this is how the learning layer actually affects behavior.

### Step 6 — Ownership check + receipts

Before an executor's edits are accepted:

```bash
python3 hooks/ctl.py check-ownership .dynos/task-{id} {seg-id} path/a.py path/b.py …
```

#### ctl.cmd_check_ownership + lib_validate.check_segment_ownership

```python
def check_segment_ownership(task_dir: Path, segment_id: str, files: Iterable[str]) -> list[str]:
    """Check that files are owned by the specified segment."""
    graph = load_json(task_dir / "execution-graph.json")
    for segment in graph.get("segments", []):
        if segment.get("id") == segment_id:
            allowed = set(segment.get("files_expected", []))
            return [file_path for file_path in files if file_path not in allowed]
    raise ValueError(f"Unknown segment id: {segment_id}")
```

**What it does:** reads the execution graph's `files_expected` for the segment; returns every edited file not in the list.
**When:** after each executor completes, before accepting edits.
**Why:** the graph validator enforces "every file belongs to exactly one segment". Ownership check is runtime enforcement — catches LLM drift and prevents parallel executors from stepping on each other.

#### lib_receipts.receipt_executor_routing + receipt_executor_done

```python
def receipt_executor_routing(task_dir: Path, segments: list[dict]) -> Path:
    return write_receipt(task_dir, "executor-routing", segments=segments)


def receipt_executor_done(task_dir, segment_id, executor_type, model_used,
                          learned_agent_injected, agent_name, evidence_path, tokens_used):
    """Also records tokens via _record_tokens → lib_tokens.record_tokens."""
    if tokens_used and tokens_used > 0:
        _record_tokens(task_dir, f"{executor_type}-{segment_id}", model_used or "default", tokens_used)
    return write_receipt(task_dir, f"executor-{segment_id}",
        segment_id=segment_id, executor_type=executor_type, model_used=model_used,
        learned_agent_injected=learned_agent_injected, agent_name=agent_name,
        evidence_path=evidence_path, tokens_used=tokens_used)
```

**What they do:** `receipt_executor_routing` (one receipt summarizing all segments' routing) and `receipt_executor_done` (one-per-segment completion receipt with per-segment token attribution).
**Why:** `CHECKPOINT_AUDIT` gate reads `executor-routing`. Per-segment receipts enable per-segment effectiveness scoring.

### Step 7 — Tests + transitions

Claude runs test suite, writes `test-results.json`:
```bash
python3 hooks/ctl.py transition .dynos/task-{id} TEST_EXECUTION
python3 hooks/ctl.py transition .dynos/task-{id} CHECKPOINT_AUDIT
```

---

## PART 3 — USER TYPES `/dynos-work:audit`

### Step 1 — Build audit plan

```bash
python3 hooks/router.py audit-plan --root . --task-type {type} --domains ui,backend [--fast-track]
```

#### router.build_audit_plan

```python
AUDITOR_ROLES = [
    "spec-completion-auditor", "security-auditor", "code-quality-auditor",
    "dead-code-auditor", "ui-auditor", "db-schema-auditor",
]

ENSEMBLE_AUDITORS = {"security-auditor", "db-schema-auditor"}
ENSEMBLE_VOTING_MODELS = ["haiku", "sonnet"]
ENSEMBLE_ESCALATION_MODEL = "opus"


def build_audit_plan(root, task_type, domains, fast_track=False) -> dict:
    plan = {"generated_at": now_iso(), "task_type": task_type,
            "domains": domains, "fast_track": fast_track, "auditors": []}

    if fast_track:
        eligible = ["spec-completion-auditor", "security-auditor"]
    else:
        eligible = ["spec-completion-auditor", "security-auditor",
                    "code-quality-auditor", "dead-code-auditor"]
        if "ui" in domains: eligible.append("ui-auditor")
        if "db" in domains: eligible.append("db-schema-auditor")
        if "backend" in domains or "db" in domains:
            eligible.append("performance-auditor")

    for auditor in eligible:
        skip_decision = resolve_skip(root, auditor, task_type)
        if skip_decision["skip"]:
            plan["auditors"].append({"name": auditor, "action": "skip", ...})
            continue

        model_decision = resolve_model(root, auditor, task_type)
        if fast_track and auditor == "spec-completion-auditor" and model_decision["source"] == "default":
            model_decision = {"model": "haiku", "source": "fast_track_override"}
        route_decision = resolve_route(root, auditor, task_type)

        entry = {"name": auditor, "action": "spawn", ...}
        if auditor in ENSEMBLE_AUDITORS and not fast_track:
            entry["ensemble"] = True
            entry["ensemble_voting_models"] = list(ENSEMBLE_VOTING_MODELS)
            entry["ensemble_escalation_model"] = ENSEMBLE_ESCALATION_MODEL
        else:
            entry["ensemble"] = False
        plan["auditors"].append(entry)

    log_event(root, "router_audit_plan", ...)
    return plan
```


**What it does:** computes audit spawn plan deterministically. Per eligible auditor: resolve skip, resolve model, resolve route. Turn on ensemble voting for security + db-schema auditors.
**When:** first thing /audit does.
**Why:** gives skill a concrete list: "spawn these, skip these, ensemble this one". No skill judgment required.

### Step 2 — Audit receipts

```python
def receipt_audit_routing(task_dir: Path, auditors: list[dict]) -> Path:
    return write_receipt(task_dir, "audit-routing", auditors=auditors)


def receipt_audit_done(task_dir, auditor_name, model_used, finding_count,
                       blocking_count, report_path, tokens_used):
    if tokens_used and tokens_used > 0:
        _record_tokens(task_dir, auditor_name, model_used or "default", tokens_used)
    return write_receipt(task_dir, f"audit-{auditor_name}",
        auditor_name=auditor_name, model_used=model_used,
        finding_count=finding_count, blocking_count=blocking_count,
        report_path=report_path, tokens_used=tokens_used)
```

### Step 3 — Repair planning

If any finding has `blocking: true`, spawn `repair-coordinator`. It produces a repair plan via:

```bash
echo '{"findings": [...]}' | python3 hooks/ctl.py repair-plan --root . --task-type {type}
```

#### ctl.cmd_repair_plan + cmd_repair_update

```python
def cmd_repair_plan(args):
    from lib_qlearn import build_repair_plan
    findings = json.load(sys.stdin)
    if isinstance(findings, dict):
        findings = findings.get("findings", [])
    result = build_repair_plan(Path(args.root).resolve(), findings, args.task_type)
    print(json.dumps(result, indent=2))
    return 0


def cmd_repair_update(args):
    from lib_qlearn import update_from_outcomes
    data = json.load(sys.stdin)
    outcomes = data.get("outcomes", []) if isinstance(data, dict) else data
    result = update_from_outcomes(Path(args.root).resolve(), outcomes, args.task_type)
    print(json.dumps(result, indent=2))
    return 0
```

**What they do:** `build_repair_plan` groups findings into parallel-safe batches, assigns executors, picks per-finding model overrides (opus floor for security-auditor findings, opus for `retry_count >= 2`, else learned policy). **`update_from_outcomes` feeds the TABULAR Q-LEARNING table** — it encodes the repair state as `{finding_category}:{severity}:{task_type}:{retry_count}`, computes a reward from the outcome (resolved=+1.0, partial=+0.3, failed=-0.5, new findings penalized at -0.1 each), and runs a **Bellman update: `Q(s,a) += α * (r + γ * max Q(s',a') - Q(s,a))`** with α=0.2, γ=0.9.

**`build_repair_plan` uses EPSILON-GREEDY** action selection from the Q-table: with probability ε=0.1, pick a random executor; otherwise pick the executor with the highest Q-value for that state. This balances exploiting known-good executor assignments with exploring alternatives.

**When:** after `repair-coordinator` categorizes findings; after each repair batch.
**Why:** ownership batching in code (can't run two executors on the same file simultaneously). The Q-table learns which executor+model combinations work best for each finding type over time.

### Step 4 — Compute reward + transition to DONE

```bash
python3 hooks/ctl.py compute-reward .dynos/task-{id} --write
```

#### ctl.cmd_compute_reward + lib_validate.compute_reward

```python
def cmd_compute_reward(args):
    from lib_validate import compute_reward
    task_dir = Path(args.task_dir).resolve()
    result = compute_reward(task_dir)
    if args.write:
        write_json(task_dir / "task-retrospective.json", result)
        try:
            from lib_receipts import receipt_retrospective
            receipt_retrospective(task_dir,
                quality_score=result.get("quality_score", 0),
                cost_score=result.get("cost_score", 0),
                efficiency_score=result.get("efficiency_score", 0),
                total_tokens=result.get("total_token_usage", 0))
        except Exception as exc:
            print(f"[warn] retrospective receipt failed: {exc}", file=sys.stderr)
    else:
        print(json.dumps(result, indent=2))
    return 0
```

`lib_validate.compute_reward` walks the task directory and produces the **REWARD VECTOR** — the ground truth signal that drives all adaptive algorithms. Seven phases:

1. **Scan audit reports** — count findings by auditor + category. Run hallucination sanitizer: findings marked `blocking` but saying "no action required" get downgraded to non-blocking minor.
2. **Repair scan** — count repair cycles, which executors had to fix things (feeds `executor_repair_frequency` → calibration uses this).
3. **Spec iterations** — grep `execution-log.md` for `[HUMAN] SPEC_REVIEW` lines.
4. **Classification + DORA** — task_type, domains, risk_level from manifest; lead_time from created_at→completed_at; change_failure if stage reached `REPAIR_FAILED`.
5. **Token scan** — `lib_tokens.get_summary` reads `token-usage.json`.
6. **Spawn/waste** — count `[SPAWN]` lines; count auditors with zero findings (wasted spawns).
7. **Reward vector:**
   - `quality_score = 1 - surviving_blocking/total_blocking` (perfect if every blocking finding repaired)
   - `cost_score = 1 / (1 + avg_tokens_per_spawn/risk_budget)` where `RISK_BUDGETS = {low:8k, medium:12k, high:18k, critical:25k}`
   - `efficiency_score = 1 - repair_cycles/3 - 0.1*(spec_iters-1)`

**Why:** this reward vector is the **ground truth signal** that the UCB1 bandit, EMA effectiveness scores, Q-learning table, and skip thresholds all optimize against. Every adaptive algorithm in the system traces back to these three numbers.
**Hands off to:** `task-retrospective.json` + `retrospective` receipt.

#### Transition to DONE

```bash
python3 hooks/ctl.py transition .dynos/task-{id} DONE
```

`transition_task` verifies: retrospective file exists, audit reports exist, `retrospective` receipt exists. **Post-completion no longer required at this gate**.**

Gate passes → stage flips to DONE → `_fire_task_completed()` runs → event bus drains → `post-completion` receipt gets written by the drain (for the *next* time this task is referenced).

---

## PART 4 — `TaskCompleted` hook + event bus

### hooks/task-completed (bash)

```bash
#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"

# --- Step 1: Find the most recently completed task and emit with identity ---
TASK_DIR=$(PYTHONPATH="${SCRIPT_DIR}:${PYTHONPATH:-}" python3 -c "
import json, sys
from pathlib import Path
dynos = Path('${PROJECT_ROOT}') / '.dynos'
best = None
for mp in sorted(dynos.glob('task-*/manifest.json'), reverse=True):
    try:
        m = json.loads(mp.read_text())
        if m.get('stage') == 'DONE':
            print(str(mp.parent)); sys.exit(0)
    except: pass
" 2>/dev/null)

PAYLOAD="{}"
if [ -n "${TASK_DIR:-}" ]; then
  TASK_ID=$(basename "$TASK_DIR")
  PAYLOAD="{\"task_id\": \"${TASK_ID}\", \"task_dir\": \"${TASK_DIR}\"}"
fi

PYTHONPATH="${SCRIPT_DIR}:${PYTHONPATH:-}" python3 "${SCRIPT_DIR}/lib_events.py" emit \
  --root "${PROJECT_ROOT}" \
  --type task-completed \
  --source task \
  --payload "$PAYLOAD" || true

PYTHONPATH="${SCRIPT_DIR}:${PYTHONPATH:-}" python3 "${SCRIPT_DIR}/eventbus.py" drain \
  --root "${PROJECT_ROOT}" || true

# --- Optional auto-commit ---
# ... (unchanged)
```


**When:** fires on every Claude Code task completion (parallel with the in-process fire from `transition_task(DONE)` — both paths carry the same payload now).
**Why:** identity-correlated events mean the `post-completion` receipt can no longer attach to the wrong task under concurrency.

### lib_events.emit_event (unchanged)

```python
def emit_event(root, event_type, source_pipeline, payload=None) -> Path:
    """Emit an event by writing a JSON file to .dynos/events/."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type: {event_type}. Valid: {sorted(EVENT_TYPES)}")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    event = {"event_type": event_type, "emitted_at": now_iso(),
             "source_pipeline": source_pipeline, "payload": payload or {},
             "processed_by": []}
    event_path = _events_dir(root) / f"{ts}-{event_type}.json"
    write_json(event_path, event)
    return event_path
```

### eventbus.drain — the central fan-out

```python
HANDLERS: dict[str, list[HandlerEntry]] = {
    "task-completed":        [("memory", run_memory), ("trajectory", run_trajectory)],
    "memory-completed":      [("calibration", run_calibration), ("patterns", run_patterns)],
    "calibration-completed": [("postmortem", run_postmortem), ("improve", run_improve), ("benchmark", run_benchmark)],
    "benchmark-completed":   [("dashboard", run_dashboard), ("register", run_register)],
}

FOLLOW_ON: dict[str, str] = {
    "task-completed":        "memory-completed",
    "memory-completed":      "calibration-completed",
    "calibration-completed": "benchmark-completed",
}


def drain(root: Path, max_iterations: int = 10) -> dict:
    """Process all pending events until the queue is drained."""
    summary: dict[str, list[str]] = {}
    iteration = 0
    emitted_follow_ons: set[str] = set()   # tracked ACROSS iterations
    completed_task_dirs: list[str] = []    # ALL task dirs from task-completed events

    from lib_core import is_learning_enabled
    learning = is_learning_enabled(root)
    _LEARNING_HANDLERS = {"memory", "trajectory", "calibration", "patterns", "improve", "benchmark"}

    while iteration < max_iterations:
        iteration += 1
        processed_any = False
        # Track per-event-type: whether ALL handlers succeeded across ALL events
        handler_all_ok: dict[str, dict[str, bool]] = {}

        for event_type, handlers in HANDLERS.items():
            for consumer_name, handler_fn in handlers:
                skip_execution = not learning and consumer_name in _LEARNING_HANDLERS
                try:
                    events = consume_events(root, event_type, consumer_name)
                except Exception as e:
                    print(f"  [warn] consume_events({event_type}, {consumer_name}): {e}", file=sys.stderr)
                    continue
                for event_path, event_data in events:
                    processed_any = True
                    payload = event_data.get("payload", {})

                    # Capture task identity from task-completed events
                    if event_type == "task-completed" and isinstance(payload, dict):
                        td = payload.get("task_dir")
                        if td and td not in completed_task_dirs:
                            completed_task_dirs.append(td)

                    if skip_execution:
                        success = True
                        err_msg = None
                    else:
                        err_msg = None
                        t0 = time.monotonic()
                        try:
                            success = handler_fn(root, payload)
                        except Exception as e:
                            err_msg = str(e)
                            success = False
                        log_event(root, "eventbus_handler", handler=consumer_name,
                                  trigger_event=event_type, success=success,
                                  duration_s=round(time.monotonic() - t0, 3),
                                  error=err_msg if not success else None)

                    # AND semantics: any failure sticks
                    prev = handler_all_ok.setdefault(event_type, {}).get(consumer_name, True)
                    handler_all_ok[event_type][consumer_name] = prev and success

                    # Only mark processed on success — failed events stay for retry
                    if success:
                        mark_processed(event_path, consumer_name)

                    status = "ok" if success else "failed"
                    summary.setdefault(event_type, []).append(f"{consumer_name}:{status}")

            # Emit follow-on only when ALL active handlers for this event type succeeded.
            if event_type in handler_all_ok and event_type in FOLLOW_ON:
                results = handler_all_ok[event_type]
                all_succeeded = all(results.values())
                if all_succeeded:
                    follow_on = FOLLOW_ON[event_type]
                    if follow_on not in emitted_follow_ons:
                        emit_event(root, follow_on, "eventbus")
                        emitted_follow_ons.add(follow_on)
                else:
                    failed = [k for k, v in results.items() if not v]
                    print(f"  [gate] {event_type} follow-on blocked — failed: {', '.join(failed)}", file=sys.stderr)

        if not processed_any:
            break

    cleanup_old_events(root)

    # Write post-completion receipt for EACH completed task 
    if "task-completed" in summary and completed_task_dirs:
        handlers_run = [...]
        postmortem_ok = any(r.startswith("postmortem:ok") for r in summary.get("calibration-completed", []))
        patterns_ok = any(r.startswith("patterns:ok") for r in summary.get("memory-completed", []))
        for td in completed_task_dirs:
            try:
                from lib_receipts import receipt_post_completion
                task_dir = Path(td)
                if task_dir.exists():
                    receipt_post_completion(task_dir,
                        handlers_run=handlers_run,
                        postmortem_written=postmortem_ok,
                        patterns_updated=patterns_ok)
            except Exception as exc:
                print(f"  [warn] post-completion receipt failed for {td}: {exc}", file=sys.stderr)

    return summary
```





**Also notable —** handler function names tracked the event-name rename:
- `run_memory` (was `run_learn`)
- `run_calibration` (was `run_evolve`)
- `run_trajectory`, `run_patterns`, `run_postmortem`, `run_improve`, `run_benchmark`, `run_dashboard`, `run_register` — unchanged behavior

The Python scripts they call are unchanged (`patterns.py`, `evolve.py`, `trajectory.py`, etc); only the event and handler names were renamed for clarity.

**When:** the full drain runs after every task completion. Each handler has a 5-minute timeout and swallows subprocess failures independently.
**Why:** this is what turns a task completion into persistent learning. Patterns → trajectory → calibration → postmortem → benchmark → dashboard. The fixes make the chain exactly-once, retry-safe on failure, and correctly correlated to task identity.
**Hands off to:** `post-completion` receipt per completed task directory.

### Event-bus handler wrappers

```python
def run_memory(root, _payload):
    """Aggregate retrospectives into project memory."""
    return _run(["python3", str(SCRIPT_DIR / "patterns.py"), "--root", str(root)], root)

def run_trajectory(root, _payload):
    return _run(["python3", str(SCRIPT_DIR / "trajectory.py"), "rebuild", "--root", str(root)], root)

def run_calibration(root, _payload):
    """Deterministic project-specific agent generation."""
    return _run(["python3", str(SCRIPT_DIR / "evolve.py"), "auto", "--root", str(root)], root)

def run_patterns(root, _payload):
    return _run(["python3", str(SCRIPT_DIR / "patterns.py"), "--root", str(root)], root)

def run_postmortem(root, _payload):
    return _run(["python3", str(SCRIPT_DIR / "postmortem.py"), "generate", "--root", str(root)], root)

def run_improve(root, _payload):
    return _run(["python3", str(SCRIPT_DIR / "postmortem.py"), "improve", "--root", str(root)], root)

def run_benchmark(root, _payload):
    return _run(["python3", str(SCRIPT_DIR / "auto.py"), "run", "--root", str(root)], root)

def run_dashboard(root, _payload):
    return _run(["python3", str(SCRIPT_DIR / "dashboard.py"), "generate", "--root", str(root)], root)

def run_register(root, _payload):
    return _run(["python3", str(SCRIPT_DIR / "registry.py"), "set-active", str(root)], root)
```

### lib_receipts.receipt_post_completion

```python
def receipt_post_completion(task_dir, handlers_run, postmortem_written, patterns_updated):
    """Write receipt proving post-completion pipeline ran."""
    return write_receipt(task_dir, "post-completion",
        handlers_run=handlers_run,
        postmortem_written=postmortem_written,
        patterns_updated=patterns_updated)
```

**What it does:** proves the event bus actually ran. This receipt is *observational* now (not gate-blocking) but still part of the audit trail.

---

## PART 5 — `memory` handler → patterns.py (the **EMA + POLICY ENGINE**)

When the drain runs the `task-completed → memory` handler, it invokes `patterns.py --root .`, which calls `write_patterns(root)`. This is where the system "remembers" — replaying all task outcomes through statistical accumulators to produce the policies that the UCB1 bandit, skip threshold, and model selection all read.

### patterns.write_patterns

Six steps:

1. **Collect retrospectives** — scan `.dynos/task-*/task-retrospective.json`. Derive observed task_types, executor_roles, auditor_roles.
2. **Compute effectiveness scores via EMA** — per-(role, model, task_type, source) quality/cost/efficiency. **This is the core adaptive signal.**
3. **Derive policies from EMA scores** — `ema_model_policy` (which model wins for each role × task_type via **weighted composite scoring**) and `ema_skip_policy` (**adaptive skip threshold** per auditor).
4. **Merge EMA into JSON** — EMA wins when both EMA and legacy sources have a value.
5. **Migrate manual overrides** — move any `model_overrides` from `policy.json` into `model-policy.json`.
6. **Write to disk** — `model-policy.json`, `skip-policy.json`, `route-policy.json`, then render `dynos_patterns.md` and write to TWO locations:
   - `~/.dynos/projects/{slug}/dynos_patterns.md` (canonical local)
   - `~/.claude/projects/{slug}/memory/dynos_patterns.md` (Claude Code auto-loads on session start)

### patterns.compute_effectiveness_scores — **EMA (Exponential Moving Average)**

```python
EMA_ALPHA = 0.3
QuadKey = tuple[str, str, str, str]  # (role, model, task_type, source)

def compute_effectiveness_scores(retrospectives, baseline=None) -> list[dict]:
    quads = _extract_quads(retrospectives)   # sorted by task_id for deterministic replay

    state: dict[QuadKey, dict] = {}
    prev_quality: dict[QuadKey, float] = {}
    drop_streak: dict[QuadKey, int] = {}

    for key, q, c, e in quads:
        if key not in state:
            # Cold-start: alpha = 1.0
            state[key] = {"quality_ema": q, "cost_ema": c, "efficiency_ema": e, "sample_count": 1}
            prev_quality[key] = q
            drop_streak[key] = 0
        else:
            s = state[key]
            s["quality_ema"]    = EMA_ALPHA * q + (1 - EMA_ALPHA) * s["quality_ema"]
            s["cost_ema"]       = EMA_ALPHA * c + (1 - EMA_ALPHA) * s["cost_ema"]
            s["efficiency_ema"] = EMA_ALPHA * e + (1 - EMA_ALPHA) * s["efficiency_ema"]
            s["sample_count"] += 1

            # Regression detection: 2+ consecutive quality drops pulls toward baseline
            if q < prev_quality.get(key, q):
                drop_streak[key] = drop_streak.get(key, 0) + 1
            else:
                drop_streak[key] = 0
            prev_quality[key] = q

            if drop_streak[key] >= 2 and baseline:
                bq, bc, be = baseline.get(key, (s["quality_ema"], s["cost_ema"], s["efficiency_ema"]))
                s["quality_ema"]    = EMA_ALPHA * bq + (1 - EMA_ALPHA) * s["quality_ema"]
                s["cost_ema"]       = EMA_ALPHA * bc + (1 - EMA_ALPHA) * s["cost_ema"]
                s["efficiency_ema"] = EMA_ALPHA * be + (1 - EMA_ALPHA) * s["efficiency_ema"]

            # Clamp to [0, 1]
            s["quality_ema"]    = max(0.0, min(1.0, s["quality_ema"]))
            s["cost_ema"]       = max(0.0, min(1.0, s["cost_ema"]))
            s["efficiency_ema"] = max(0.0, min(1.0, s["efficiency_ema"]))

    rows = [{"role": role, "model": model, "task_type": task_type, "source": source,
             "quality_ema": round(s["quality_ema"], 4),
             "cost_ema": round(s["cost_ema"], 4),
             "efficiency_ema": round(s["efficiency_ema"], 4),
             "sample_count": s["sample_count"], "updated": now_iso()}
            for (role, model, task_type, source), s in state.items()]
    rows.sort(key=lambda r: (-r["sample_count"], r["role"], r["model"]))
    return rows[:MAX_EFFECTIVENESS_ROWS]
```

**What it does:** the **EMA engine**. Replays every retrospective's `(role, model, task_type, source) → (quality, cost, efficiency)` in task-id order. Maintains per-quad EMA with **α=0.3** (new observation contributes 30%, recent tasks dominate). Cold start: first observation sets the EMA directly (α=1.0). **Regression detection via EMA baseline blend-back:** on 2+ consecutive quality drops, pulls EMA toward the stored baseline — a safety net that prevents learned agents from dragging scores down permanently.

**When:** inside `write_patterns`, replays ALL retrospectives every time (deterministic replay, not incremental).
**Why:** EMA gives recency bias — a learned agent that just started regressing pulls its score down within a few tasks. The baseline blend-back ensures the system recovers even if a learned agent causes a string of bad outcomes.
**Hands off to:** `derive_model_policy` and `derive_skip_policy`.

### patterns.derive_model_policy — **WEIGHTED COMPOSITE SCORING**

For each `(role, task_type)`, weighted-average across source. **Composite = `0.5*quality + 0.3*cost + 0.2*efficiency`**. Rank by composite (tie-break: higher quality within 0.03). Security-auditor forced to opus regardless. Writes `{role:task_type → {model, confidence, updated}}`. This is the data the **UCB1 bandit** in `router.resolve_model` reads.

### patterns.derive_skip_policy — **ADAPTIVE SKIP THRESHOLD**

For each non-exempt auditor (security, spec-completion, code-quality are always-run), average quality_ema across rows. **Threshold = `clamp(round(3 + 2*(1 - avg_quality)), 1, 10)`**. High-quality auditors (avg ≥ 0.9) skip after 3 consecutive zero-finding tasks; low-quality auditors (avg ≤ 0.5) skip after 5. This is read by `router.resolve_skip`.

---

## PART 6 — `trajectory` handler

```python
def cmd_rebuild(args):
    store = rebuild_trajectory_store(Path(args.root).resolve())
    print(json.dumps({
        "version": store["version"],
        "updated_at": store["updated_at"],
        "trajectory_count": len(store.get("trajectories", [])),
    }, indent=2))
    return 0
```

Delegates to `lib_trajectory.rebuild_trajectory_store` which scans retrospectives + execution graphs, produces compact `(state_signature, sequence, outcome)` traces in `~/.dynos/projects/{slug}/trajectories.json`.

**When:** after every task completion (parallel with `run_memory`); also in every `daemon.maintenance_cycle`.
**Why:** `/start` reads this during discovery via `hooks/trajectory.py search` to surface similar past tasks and their common failure patterns. Advisory only — human approval still decides.

---

## PART 7 — `calibration` handler

Skill file: `skills/calibration/SKILL.md`. Drain handler is `run_calibration` which calls `evolve.py auto`.

### evolve.cmd_auto

Three phases:

1. **Gate check** — need ≥5 retrospectives to generate anything.
2. **Discover uncovered slots** — scan every `execution-graph.json` across `.dynos/task-*`. Count `(executor_role, task_type)` occurrences. Find combinations that ran ≥3 times but don't have a learned agent.
3. **Generate** — for each uncovered slot, write `~/.dynos/projects/{slug}/learned-agents/executors/auto-{short-role}-{task_type}.md`. Body assembled by `_build_agent_content` from finding categories, repair frequency, and matching prevention rules. Register in the registry in `shadow` mode.

```python
def cmd_auto(args):
    root = Path(args.root).resolve()
    retrospectives = collect_retrospectives(root)
    registry = ensure_learned_registry(root)
    min_tasks = int(args.min_tasks)

    if len(retrospectives) < min_tasks:
        # skip — insufficient data
        return 0

    # Scan execution graphs to find uncovered (role, task_type) slots
    role_type_counts: dict[tuple[str, str], int] = {}
    for task_dir in sorted((root / ".dynos").iterdir()):
        if not task_dir.name.startswith("task-"): continue
        graph_path = task_dir / "execution-graph.json"
        retro_path = task_dir / "task-retrospective.json"
        if not (graph_path.exists() and retro_path.exists()): continue
        try:
            retro = load_json(retro_path)
            graph = load_json(graph_path)
            task_type = retro.get("task_type", "")
            if not task_type: continue
            for seg in graph.get("segments", []):
                role = seg.get("executor", "")
                if role and isinstance(role, str):
                    role_type_counts[(role, task_type)] = role_type_counts.get((role, task_type), 0) + 1
        except (json.JSONDecodeError, OSError):
            continue

    existing = {(a.get("role"), a.get("task_type")) for a in registry.get("agents", [])}
    candidates = [(r, t, c) for (r, t), c in role_type_counts.items()
                  if c >= 3 and (r, t) not in existing]

    for role, task_type, count in candidates:
        agent_name = f"auto-{role.replace('-executor', '')}-{task_type}"
        agent_dir = _persistent_project_dir(root) / "learned-agents" / "executors"
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent_path = agent_dir / f"{agent_name}.md"
        latest_task = retrospectives[-1].get("task_id", "unknown")
        if not agent_path.exists():
            matched_retros = _matching_retrospectives(retrospectives, role, task_type)
            content = _build_agent_content(agent_name, role, task_type, matched_retros, latest_task, root)
            agent_path.write_text(content)
        rel_path = str(agent_path.relative_to(root))
        register_learned_agent(root,
            agent_name=agent_name, role=role, task_type=task_type,
            path=rel_path, generated_from=latest_task)
    return 0
```

**When:** on `memory-completed` event; also in every `daemon.maintenance_cycle`.
**Why:** project-specific specialists emerge from observed patterns. After seeing 3 backend-executor-feature tasks, the system synthesizes an agent capturing their common finding categories and repair locations. Starts in `shadow` mode — doesn't affect routing until benchmarks prove it out.
**Hands off to:** `register_learned_agent` writes to the registry; subsequent `patterns.write_patterns` computes its effectiveness; `router.resolve_route` may promote it.

### evolve._build_agent_content

Synthesizes a learned agent's prompt content from past observations. Three sections: Context (role, task_type, observation count, generation date), Finding Categories (sorted desc by count), Repair Frequency (stats), Prevention Rules (filtered to matching categories).

**Why:** richer file → more project-specific executor once promoted to `replace`.

---

## PART 8 — `maintain` (the background daemon)

Separate from the event bus. Runs periodically (default hourly) for bulk maintenance.

### daemon.cmd_run_loop

```python
def cmd_run_loop(args):
    root = Path(args.root).resolve()
    maintenance_dir(root).mkdir(parents=True, exist_ok=True)
    poll_seconds = int(args.poll_seconds or maintainer_policy(root)["maintainer_poll_seconds"])
    pid_path(root).write_text(f"{os.getpid()}\n")
    if stop_path(root).exists():
        stop_path(root).unlink()
    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)
    try:
        while not _SHOULD_STOP and not stop_path(root).exists():
            cycle = maintenance_cycle(root)
            # ... write status
            for _ in range(poll_seconds):
                if _SHOULD_STOP or stop_path(root).exists():
                    break
                time.sleep(1)
    finally:
        if pid_path(root).exists(): pid_path(root).unlink()
        if stop_path(root).exists(): stop_path(root).unlink()
        write_status(root, {"updated_at": now_iso(), "running": False, "pid": None})
    return 0
```

**What it does:** daemon main loop. Writes PID file. Installs SIGTERM/SIGINT handlers. Sleeps in 1s increments (SIGTERM-responsive) up to poll_seconds. Each iteration runs `maintenance_cycle`. On exit, cleans up PID and stop files.

### daemon.maintenance_cycle

```python
def maintenance_cycle(root: Path) -> dict:
    cycle_start = time.monotonic()
    lock_file = maintenance_dir(root) / "cycle.lock"
    lock_fd = open(lock_file, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fd.close()
        return {"executed_at": now_iso(), "ok": True, "skipped": True,
                "reason": "cycle lock held by another process", "actions": []}
    try:
        actions: list[dict] = []
        for script_name, args in (
            ("trajectory.py",  ("rebuild",      "--root", str(root))),
            ("patterns.py",    ("--root",        str(root))),
            ("evolve.py",      ("auto",         "--root", str(root))),
            ("postmortem.py",  ("generate-all", "--root", str(root))),
            ("postmortem.py",  ("improve",      "--root", str(root))),
            ("fixture.py",     ("sync",         "--root", str(root))),
            ("auto.py",        ("run",          "--root", str(root))),
            ("dashboard.py",   ("generate",     "--root", str(root))),
            ("report.py",      ("--root",        str(root))),
        ):
            completed, payload = run_python(root, script_name, *args)
            action = {"name": script_name, "returncode": completed.returncode}
            if payload is not None: action["result"] = payload
            if completed.stderr.strip(): action["stderr"] = completed.stderr.strip()
            actions.append(action)
        # ... write cycle log + status
        return cycle
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
```

**What it does:** one cycle = nine subprocess calls in sequence. `fcntl.LOCK_EX | LOCK_NB` ensures only one cycle runs at a time. Each script has 300s timeout. Logs every cycle to `.dynos/maintenance/cycles.jsonl`. Updates `.dynos/maintenance/status.json`.

**When:** every `poll_seconds` (default 3600) while daemon runs. Also triggered manually via `daemon.py run-once`.
**Why:** idempotent. All nine scripts are safe to run repeatedly. Running doesn't require any prior state.

---

## PART 9 — `SubagentStop` hook

Fires every time a subagent completes. Bash wrapper parses hook JSON from stdin (transcript path, agent type, cwd), invokes `lib_tokens_hook.py`.

### lib_tokens_hook.main

```python
def _parse_transcript(transcript_path: Path) -> dict:
    """Parse a subagent transcript JSONL and sum token usage."""
    total_input = 0
    total_output = 0
    model = "unknown"
    agent_id = ""

    with open(transcript_path) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try: entry = json.loads(line)
            except json.JSONDecodeError: continue

            if not agent_id and entry.get("agentId"):
                agent_id = entry["agentId"]

            msg = entry.get("message", {})
            if isinstance(msg, dict):
                if msg.get("model"):
                    model = msg["model"]
                usage = msg.get("usage", {})
                if isinstance(usage, dict):
                    total_input += usage.get("input_tokens", 0)
                    total_input += usage.get("cache_read_input_tokens", 0)
                    total_input += usage.get("cache_creation_input_tokens", 0)
                    total_output += usage.get("output_tokens", 0)

    if "haiku" in model: model = "haiku"
    elif "sonnet" in model: model = "sonnet"
    elif "opus" in model:  model = "opus"

    return {"input_tokens": total_input, "output_tokens": total_output,
            "model": model, "agent_id": agent_id}
```

**What it does:** reads the transcript JSONL Claude Code writes. Sums `input_tokens` + both `cache_read_input_tokens` and `cache_creation_input_tokens` (all count as input), and `output_tokens`. Normalizes model name. Infers phase (`auditor`→audit, `executor`→execution, etc). Extracts `seg-N` from agent type/description. Calls `record_tokens`.

**When:** every subagent completion.
**Why:** belt-and-suspenders. Skill-side receipt writers also record tokens; this hook catches anything skills forget to record.
**Hands off to:** `lib_tokens.record_tokens`.

### lib_tokens.record_tokens (unchanged)

Append-only event log + aggregates. Every event (spawn, deterministic, stage-transition, receipt-record) adds one entry to `events[]`. Non-deterministic, non-zero-token events also update the `by_agent` map. `_recompute_totals` rebuilds `by_model` totals from `by_agent` so the two are always consistent.

---

## PART 10 — New auxiliary hooks in current main

A handful of helper scripts that support specific auditors or executors:

| Script | What it does |
|---|---|
| `hooks/planner.py` | Deterministic `start-plan` / `planning-mode` / `task-policy` decisions — centralizes what the start skill would otherwise derive in prose |
| `hooks/typecheck_lint.py` | Runs language-native type-check + lint (mypy/pyright, tsc, go vet + ruff/eslint/gopls). Used by code-quality-auditor |
| `hooks/verify_behavior_preserved.py` | Clones repo into tmp worktrees for pre/post refs, runs framework-detected tests in both, diffs pass/fail sets. Used by refactor-executor + refactor audit |
| `hooks/performance_check.py` | Runs deterministic query-pattern / complexity / resource heuristics over changed files. Used by performance-auditor |
| `hooks/validate_docs_accuracy.py` | Checks that docs reference real symbols / paths / CLIs. Used by docs-executor audit feedback |
| `hooks/compliance_check.py` | License + SBOM + privacy dep scan for compliance findings |

Each is invoked directly by the relevant agent or skill and writes a structured JSON report (no side effects beyond its own output file). Failures are non-blocking for the pipeline — the auditor consumes the report.

---

## Appendix — updated pipeline map

```
SessionStart hook
  ├─ registry.cmd_set_active          → ~/.dynos/registry.json bump
  ├─ dashboard.py generate            → .dynos/dashboard-{data.json,html}
  ├─ daemon.cmd_ensure              → conditionally starts daemon
  └─ inline: router.load_prevention_rules + lib_core.project_policy
                                      → .dynos/routing-context.json

/dynos-work:start
  ├─ registry.cmd_register            → register the project
  ├─ daemon.cmd_start               → guaranteed daemon running
  ├─ ctl.cmd_validate_task            → lib_validate.validate_task_artifacts
  ├─ router.resolve_route             → learned-skill check
  ├─ Agent(planning)                  → spec.md, classification
  ├─ ctl.cmd_transition               → lib_core.transition_task (→ SPEC_NORMALIZATION)
  ├─ lib_validate.apply_fast_track    → fast-track gate
  ├─ (planner.py decides mode)        → standard vs hierarchical planning
  ├─ Agent(planning)                  → plan.md, execution-graph.json
  ├─ validate_task_artifacts.main     → plan_gap_analysis.run_gap_analysis,
  │                                     lib_receipts.receipt_plan_validated
  ├─ Agent(spec-completion-auditor)   → plan audit
  ├─ Agent(testing-executor TDD)      → tdd-tests.md
  └─ ctl.cmd_transition               → PRE_EXECUTION_SNAPSHOT

/dynos-work:execute
  ├─ ctl.cmd_validate_contract        → lib_contracts.validate_inputs/outputs
  ├─ router.cmd_executor_plan         → build_executor_plan
  │                                      → resolve_model (UCB1), resolve_route
  │                                      → prevention-rule per-executor filter
  ├─ lib_receipts.receipt_executor_routing
  ├─ per segment:
  │    ├─ router.cmd_inject_prompt    → build_executor_prompt
  │    ├─ Agent(executor)             → ui/backend/db/ml/refactor/testing/integration/docs
  │    ├─ ctl.cmd_check_ownership     → lib_validate.check_segment_ownership
  │    └─ lib_receipts.receipt_executor_done → lib_tokens.record_tokens
  ├─ test runner
  └─ ctl.cmd_transition               → CHECKPOINT_AUDIT

/dynos-work:audit
  ├─ router.cmd_audit_plan            → build_audit_plan
  │                                      → resolve_skip, resolve_model, resolve_route
  │                                      → adds performance-auditor for backend/db (new)
  ├─ lib_receipts.receipt_audit_routing
  ├─ per auditor:
  │    ├─ Agent(auditor)              → audit-reports/{auditor}-{ts}.json
  │    └─ lib_receipts.receipt_audit_done → lib_tokens.record_tokens
  ├─ if blocking findings:
  │    ├─ Agent(repair-coordinator)   → repair-log.json
  │    ├─ ctl.cmd_repair_plan         → lib_qlearn.build_repair_plan
  │    ├─ Agent(executors)            → apply fixes
  │    └─ ctl.cmd_repair_update       → lib_qlearn.update_from_outcomes
  ├─ loop back to audit until clean
  ├─ ctl.cmd_compute_reward           → lib_validate.compute_reward → task-retrospective.json,
  │                                     lib_receipts.receipt_retrospective
  └─ ctl.cmd_transition               → lib_core.transition_task (→ DONE)
                                        [no more post-completion gate]
                                        ↓
TaskCompleted hook (also fired from transition_task DONE via _fire_task_completed)
  ├─ lib_events.emit_event("task-completed", payload={task_id, task_dir})
  └─ eventbus.drain
        ├─ task-completed → memory          (patterns.write_patterns)
        ├─ task-completed → trajectory      (trajectory.cmd_rebuild)
        ├─ emits memory-completed (once per drain, only if all handlers OK)
        ├─ memory-completed → calibration   (evolve.cmd_auto)
        ├─ memory-completed → patterns      (patterns.write_patterns again)
        ├─ emits calibration-completed
        ├─ calibration-completed → postmortem / improve / benchmark
        ├─ emits benchmark-completed
        ├─ benchmark-completed → dashboard / register
        └─ lib_receipts.receipt_post_completion (one per task_dir from payload)

SubagentStop hook (every subagent)
  └─ lib_tokens_hook.main
        ├─ _parse_transcript              → {input_tokens, output_tokens, model}
        └─ lib_tokens.record_tokens       → append event to token-usage.json

Background (maintain daemon, poll_seconds interval)
  └─ daemon.maintenance_cycle
        └─ 9 scripts: trajectory, patterns, evolve, postmortem×2, fixture, auto, dashboard, report
```

### Summary of behavioral changes since prior trace

| Area | Before | After |
|---|---|---|
| DONE gate | required `post-completion` receipt (impossible on first try) | requires only retrospective artifacts + `retrospective` receipt |
| Event fan-out | one `task-completed` could emit up to N duplicate `memory-completed` events | follow-on emitted at most once per drain lifetime |
| Event identity | `task-completed` was bare; receipt attached to "most recent task" by scan | payload carries `task_id`+`task_dir`; receipt writes per-task from payload |
| Failed handlers | silently consumed, cascade continued | stay unconsumed for retry; follow-on blocked on AND-failure |
| Prevention rules | dict-only assumption crashed on strings; every rule went to every executor | shape-validated; per-rule `executor` filter |
| `policy.json` | both helpers self-healed with partial defaults, wiping each other | merge defaults without clobbering; project_policy only writes on first create |
| Event names | `learn` / `evolve` / `observe` | `memory` / `calibration` / `telemetry` |
| New auditor | — | `performance-auditor` for backend/db domains |
| New executor | — | `docs-executor` for documentation segments |
| Planning | skill derived hierarchical/standard in prose | `hooks/planner.py` centralizes decision |
| Inline fast-track | all segments spawned a subagent | fast-track single-segment can execute inline (saves ~30K tokens) |
