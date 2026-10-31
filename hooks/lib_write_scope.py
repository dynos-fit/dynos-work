"""Git-based write-scope backstop (7.6.0 field report, bug 2).

The PreToolUse hook can only parse what a Bash command *says*; an
interpreter or subprocess that writes a file is invisible to it. This
module checks what actually changed instead: every repo file that differs
from the task's snapshot (``manifest.snapshot.head_sha``; committed,
uncommitted and untracked changes, deletions included) must be covered by the task's declared write scope:

* every execution-graph segment's ``files_expected`` and ``files_optional``,
* every repair cycle's ``files_to_modify`` (the live ``repair-log.json``
  plus every cycle in ``repair-log-history.json``, because each cycle
  overwrites the repair log),
* the TDD receipt's ``test_file_paths``,
* files a human acknowledged with ``ctl acknowledge-out-of-scope``.

Anything else is an ``out_of_scope_file``. ``run-execution-segment-done``
and ``run-audit-findings-gate`` report them (early warning); the DONE gate
refuses while any remain unacknowledged.

Acknowledgments live in ``write-scope.json``, which is ctl-owned
(wrapper-required in write_policy): an agent cannot acknowledge its own
stray writes. The scope inputs (execution-graph, repair logs, receipts) are
ctl-owned too.

Scope of the guarantee: a task without a snapshot is legacy and skipped. A
snapshot recorded before this check existed carries no ``baseline_dirty``
map, so files already dirty when the task started cannot be told apart
from task work; such tasks are reported but never refused. Untracked files
the shared scope helper treats as noise (caches, build output, binaries,
or more than ``UNTRACKED_SCOPE_CAP`` of them) are not seen.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

_GIT: str | None = shutil.which("git")

WRITE_SCOPE_FILE = "write-scope.json"
ACKNOWLEDGE_COMMAND = (
    "ctl acknowledge-out-of-scope <task_dir> --file <path> [--file <path> ...] "
    "--reason \"<why this change belongs to the task>\""
)
# Paths the plugin itself maintains in the project during normal flow.
_PLUGIN_OWNED_PREFIXES: tuple[str, ...] = (".dynos/",)
_PLUGIN_OWNED_EXACT: frozenset[str] = frozenset({".dynos"})
# Cap on baseline entries so a huge dirty tree cannot bloat the manifest.
BASELINE_DIRTY_CAP = 2000


def _project_root(task_dir: Path) -> Path:
    return Path(task_dir).resolve().parent.parent


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None  # deleted (or unreadable) at baseline time


def _is_plugin_owned(rel: str) -> bool:
    return rel in _PLUGIN_OWNED_EXACT or rel.startswith(_PLUGIN_OWNED_PREFIXES)


def baseline_dirty_files(root: Path) -> dict[str, str | None]:
    """Map every file dirty at snapshot time to its content digest.

    Recorded by ``ctl record-snapshot`` so files the user had already
    changed before the task started are not blamed on the task — unless
    they change again afterwards. Returns {} on any git failure.
    """
    try:
        result = subprocess.run(
            [_GIT or "git", "status", "--porcelain", "-z", "--untracked-files=all", "--", "."],
            cwd=str(root), capture_output=True, text=True, check=False, timeout=30,
        )
        prefix_run = subprocess.run(
            [_GIT or "git", "rev-parse", "--show-prefix"],
            cwd=str(root), capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0 or prefix_run.returncode != 0:
        return {}
    # Porcelain paths are relative to the repository top level; make them
    # relative to ``root`` like files_expected (a project may be nested).
    prefix = prefix_run.stdout.strip()
    out: dict[str, str | None] = {}
    entries = result.stdout.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, rel = entry[:2], entry[3:]
        if status[0] in "RC":
            i += 1  # -z rename/copy: the next entry is the source path
        if prefix:
            if not rel.startswith(prefix):
                continue
            rel = rel[len(prefix):]
        if _is_plugin_owned(rel):
            continue
        out[rel] = _file_digest(root / rel)
        if len(out) >= BASELINE_DIRTY_CAP:
            break
    return out


def changed_files_since(root: Path, base: str) -> tuple[list[str], str | None, str | None]:
    """(changed, error, warning): files differing from ``base`` plus
    untracked files, all relative to ``root`` (``--relative`` keeps a
    project nested inside a larger repository consistent with its
    root-relative files_expected). Untracked caches, build output and
    binaries are skipped like the audit scope does; above
    ``UNTRACKED_SCOPE_CAP`` untracked files none are checked and
    ``warning`` says so.
    """
    from lib_core import UNTRACKED_SCOPE_CAP, _untracked_is_noise  # noqa: PLC0415

    outputs: list[list[str]] = []
    for cmd in (
        [_GIT or "git", "diff", "--name-only", "--no-renames", "--relative", base],
        [_GIT or "git", "ls-files", "--others", "--exclude-standard"],
    ):
        try:
            result = subprocess.run(
                cmd, cwd=str(root), capture_output=True, text=True, check=False, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [], f"{' '.join(cmd[:3])} failed: {exc}", None
        if result.returncode != 0:
            return [], (result.stderr or result.stdout or f"{' '.join(cmd[:3])} failed").strip(), None
        outputs.append([line.strip() for line in result.stdout.splitlines() if line.strip()])
    tracked, untracked_raw = outputs
    untracked = [
        p for p in untracked_raw
        if not _is_plugin_owned(p) and not _untracked_is_noise(root, p)
    ]
    warning = None
    if len(untracked) > UNTRACKED_SCOPE_CAP:
        warning = (
            f"{len(untracked)} untracked files (cap {UNTRACKED_SCOPE_CAP}); untracked "
            f"files were not checked against the write scope"
        )
        untracked = []
    seen: set[str] = set()
    changed = [p for p in tracked + untracked if not (p in seen or seen.add(p))]
    return changed, None, warning


def load_write_scope(task_dir: Path) -> dict:
    data = _load(Path(task_dir) / WRITE_SCOPE_FILE)
    if not isinstance(data, dict):
        data = {}
    acked = data.get("acknowledged")
    return {
        "acknowledged": [
            dict(e) for e in acked
            if isinstance(e, dict) and isinstance(e.get("file"), str) and e.get("file")
        ] if isinstance(acked, list) else [],
    }


def _repair_log_files(task_dir: Path) -> list[str]:
    """files_to_modify of the live repair log and of every cycle recorded in
    ``repair-log-history.json`` (each cycle overwrites repair-log.json)."""
    files: list[str] = []
    logs: list[object] = [_load(Path(task_dir) / "repair-log.json")]
    history = _load(Path(task_dir) / "repair-log-history.json")
    if isinstance(history, dict):
        logs.extend(
            c.get("repair_log") for c in history.get("cycles") or [] if isinstance(c, dict)
        )
    for data in logs:
        _collect_repair_files(data, files)
    return files


def _collect_repair_files(data: object, files: list[str]) -> None:
    if not isinstance(data, dict):
        return
    for batch in data.get("batches") or []:
        if not isinstance(batch, dict):
            continue
        for task in batch.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            for key in ("files_to_modify", "affected_files"):
                for f in task.get(key) or []:
                    if isinstance(f, str) and f.strip() and f.strip() not in files:
                        files.append(f.strip())


def declared_scope(task_dir: Path) -> list[str]:
    """files_expected-style entries (exact, dir/ or glob) the task may write."""
    task_dir = Path(task_dir)
    entries: list[str] = []

    def _add(value: object) -> None:
        if isinstance(value, str) and value.strip() and value.strip() not in entries:
            entries.append(value.strip())

    graph = _load(task_dir / "execution-graph.json")
    if isinstance(graph, dict):
        for seg in graph.get("segments") or []:
            if isinstance(seg, dict):
                for key in ("files_expected", "files_optional"):
                    for f in seg.get(key) or []:
                        _add(f)
    for f in _repair_log_files(task_dir):
        _add(f)
    tdd = _load(task_dir / "receipts" / "tdd-tests.json")
    if isinstance(tdd, dict):
        for f in tdd.get("test_file_paths") or []:
            _add(f)
    return entries


def _normalize(entry: str, root: Path) -> str:
    """Make an absolute in-repo entry relative; strip a leading ``./``."""
    p = entry
    if p.startswith("/"):
        try:
            p = str(Path(p).resolve().relative_to(root.resolve()))
        except (ValueError, OSError):
            return p
    while p.startswith("./"):
        p = p[2:]
    return p


def _covered(rel: str, entries: list[str]) -> bool:
    from lib_validate import files_expected_entry_matches  # noqa: PLC0415

    return any(files_expected_entry_matches(e, rel) for e in entries)


def write_scope_report(task_dir: Path) -> dict:
    """Compare files changed since the snapshot with the declared scope.

    Returns a dict with ``status``: ``"skipped"`` (no snapshot / legacy),
    ``"unavailable"`` (git failed; ``error`` set) or ``"checked"``, plus
    ``out_of_scope_files`` (unacknowledged), ``acknowledged_files``,
    ``enforced`` (whether the DONE gate may refuse) and ``warning``.
    Never raises.
    """
    task_dir = Path(task_dir)
    report: dict = {
        "status": "skipped",
        "out_of_scope_files": [],
        "acknowledged_files": [],
        "enforced": False,
        "warning": None,
    }
    try:
        manifest = _load(task_dir / "manifest.json")
        snapshot = manifest.get("snapshot") if isinstance(manifest, dict) else None
        sha = snapshot.get("head_sha") if isinstance(snapshot, dict) else None
        if not isinstance(sha, str) or not sha.strip():
            report["reason"] = "no snapshot recorded (legacy task)"
            return report
        baseline = snapshot.get("baseline_dirty")
        report["enforced"] = isinstance(baseline, dict)
        if not report["enforced"]:
            report["reason"] = (
                "snapshot predates the write-scope baseline; reported, not enforced"
            )
        root = _project_root(task_dir)
        changed, error, warning = changed_files_since(root, sha.strip())
        if error is not None:
            report["status"] = "unavailable"
            report["error"] = error
            return report
        report["warning"] = warning
        entries = [_normalize(e, root) for e in declared_scope(task_dir)]
        acked = {
            _normalize(str(e["file"]), root)
            for e in load_write_scope(task_dir)["acknowledged"]
        }
        baseline = baseline if isinstance(baseline, dict) else {}
        out: list[str] = []
        acknowledged: list[str] = []
        for rel in changed:
            if _is_plugin_owned(rel) or _covered(rel, entries):
                continue
            if rel in baseline and baseline.get(rel) == _file_digest(root / rel):
                continue  # dirty before the task started and untouched since
            if rel in acked:
                acknowledged.append(rel)
            else:
                out.append(rel)
        report["status"] = "checked"
        report["out_of_scope_files"] = sorted(out)
        report["acknowledged_files"] = sorted(acknowledged)
        return report
    except Exception as exc:  # noqa: BLE001 — a reporting helper must not raise
        report["status"] = "unavailable"
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report


def done_gate_error(task_dir: Path, current_stage: str | None, next_stage: str) -> str | None:
    """Why the write-scope backstop refuses DONE, or None."""
    report = write_scope_report(task_dir)
    if not report.get("enforced"):
        return None
    if report["status"] == "unavailable":
        return (
            f"Cannot transition {current_stage} -> {next_stage}: write-scope check "
            f"unavailable ({report.get('error')}); git must be able to diff the "
            f"task snapshot"
        )
    files = report.get("out_of_scope_files") or []
    if not files:
        return None
    shown = ", ".join(files[:20]) + (f" (+{len(files) - 20} more)" if len(files) > 20 else "")
    return (
        f"Cannot transition {current_stage} -> {next_stage}: {len(files)} file(s) "
        f"changed since the task snapshot are outside the declared write scope "
        f"(execution-graph files_expected/files_optional, repair files_to_modify): "
        f"{shown}. Revert them, or, if a human confirms they belong to this task, "
        f"run `{ACKNOWLEDGE_COMMAND}`"
    )
