#!/usr/bin/env python3
"""
Pre-load file contents into agent prompt context.

Reads each file and emits a formatted markdown block for injection into
a base prompt before inject-prompt. The agent receives file contents
upfront and does not need to call Read/Grep/Glob for them.

Usage:
  python3 hooks/build_prompt_context.py file1 file2 ...
  python3 hooks/build_prompt_context.py --root /path/to/repo file1 file2 ...
  python3 hooks/build_prompt_context.py --diff <snapshot_sha> [--root .]
  python3 hooks/build_prompt_context.py --task-dir .dynos/task-{id} [--root .]

Output is printed to stdout for inclusion in a base prompt. The
``--task-dir`` form reads ``manifest.snapshot.head_sha`` and is
preferred over ``--diff`` because it eliminates the SHA-handling
failure mode (an abbreviated/wrong SHA passed to ``--diff`` produced
a 1-byte sidecar silently).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# PRO-007: pin python3/git binaries to absolute paths resolved at import time
# so PATH-shadowing cannot substitute a malicious binary. Mirrors hooks/worktree.py
# and hooks/daemon.py.
_PYTHON3: str = shutil.which("python3") or sys.executable
_GIT: str | None = shutil.which("git")

# Hard caps to prevent the pre-load itself from blowing up context
_MAX_FILE_CHARS = 40_000
_MAX_TOTAL_CHARS = 150_000

# Byte budgets (UTF-8) for the whole emitted block. Executor pre-loads
# (file mode) are appended to every executor base prompt verbatim, so they
# get a tighter default than the audit diff sidecar. Past the budget a file
# degrades to a head excerpt, then to a path-only listing the agent reads on
# demand. Override with --max-bytes or DYNOS_PROMPT_CONTEXT_MAX_BYTES.
_DEFAULT_FILE_MODE_MAX_BYTES = 48_000
_DEFAULT_DIFF_MODE_MAX_BYTES = 150_000
_EXCERPT_MAX_BYTES = 4_000
_MIN_EXCERPT_BYTES = 1_000
_BUDGET_ENV = "DYNOS_PROMPT_CONTEXT_MAX_BYTES"

_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".md": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sh": "bash",
    ".bash": "bash",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".toml": "toml",
    ".txt": "",
}


def _lang(suffix: str) -> str:
    return _LANG.get(suffix.lower(), "")


def _read_file(path: Path) -> str:
    if not path.exists():
        return "*(file does not exist yet — create it)*"
    try:
        content = path.read_text(errors="replace")
    except OSError as e:
        return f"*(could not read: {e})*"
    if len(content) > _MAX_FILE_CHARS:
        content = content[:_MAX_FILE_CHARS] + f"\n\n... [truncated at {_MAX_FILE_CHARS} chars]"
    return content


def _nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _excerpt(content: str, max_bytes: int) -> tuple[str, int, int]:
    """Return (excerpt, lines_kept, total_lines): whole leading lines that fit."""
    lines = content.splitlines(keepends=True)
    kept: list[str] = []
    used = 0
    for line in lines:
        size = _nbytes(line)
        if used + size > max_bytes:
            break
        kept.append(line)
        used += size
    return "".join(kept), len(kept), len(lines)


def _resolve_budget(explicit: "int | None", default: int) -> int:
    if explicit is not None and explicit > 0:
        return explicit
    import os  # noqa: PLC0415
    raw = os.environ.get(_BUDGET_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return default


def _pack_blocks(
    entries: list[tuple[str, Path, str]], budget: int, used: int
) -> tuple[list[str], dict[str, list[str]], int]:
    """Pack (label, path, heading_suffix) file entries into a byte budget.

    Each file is emitted in full when it fits, else as a head excerpt when at
    least _MIN_EXCERPT_BYTES remain, else as a path-only line. Returns
    (blocks, {"full", "excerpt", "path_only"} -> labels, bytes_used).
    """
    blocks: list[str] = []
    tally: dict[str, list[str]] = {"full": [], "excerpt": [], "path_only": []}
    path_only_lines: list[str] = []
    # One oversized file must not starve the rest: with several files, a
    # single file may take at most half the budget in full.
    per_file_cap = budget if len(entries) <= 1 else max(budget // 2, _MIN_EXCERPT_BYTES)
    for label, path, suffix in entries:
        content = _read_file(path)
        lang = _lang(path.suffix)
        block = f"### `{label}`{suffix}\n\n```{lang}\n{content}\n```\n"
        size = _nbytes(block)
        if used + size <= budget and size <= per_file_cap:
            blocks.append(block)
            # _read_file's own per-file char cap already cut this one.
            truncated = content.endswith(f"[truncated at {_MAX_FILE_CHARS} chars]")
            tally["excerpt" if truncated else "full"].append(label)
            used += size
            continue
        remaining = budget - used
        if remaining >= _MIN_EXCERPT_BYTES:
            excerpt, kept, total = _excerpt(
                content, min(max(_EXCERPT_MAX_BYTES, per_file_cap // 4), remaining - 400)
            )
            if kept:
                try:
                    with path.open("rb") as fh:
                        total = sum(1 for _ in fh)
                except OSError:
                    pass
                block = (
                    f"### `{label}`{suffix} (excerpt: first {kept} of {total} lines)\n\n"
                    f"```{lang}\n{excerpt}\n```\n"
                    f"*(excerpt — over the prompt-context byte budget; Read `{label}` for the rest)*\n"
                )
                blocks.append(block)
                tally["excerpt"].append(label)
                used += _nbytes(block)
                continue
        try:
            size_note = f"{path.stat().st_size} bytes" if path.exists() else "does not exist yet"
        except OSError:
            size_note = "unreadable"
        path_only_lines.append(f"- `{label}` ({size_note})")
        tally["path_only"].append(label)
    if path_only_lines:
        listing = (
            "### Not pre-loaded (byte budget reached — Read these on demand)\n\n"
            + "\n".join(path_only_lines) + "\n"
        )
        blocks.append(listing)
        used += _nbytes(listing)
    return blocks, tally, used


def _budget_line(budget: int, used: int, tally: dict[str, list[str]]) -> str:
    return (
        f"Prompt-context budget: max_bytes={budget} used_bytes={used} "
        f"full={len(tally['full'])} excerpt={len(tally['excerpt'])} "
        f"path_only={len(tally['path_only'])}\n\n"
    )


def build_file_context(files: list[str], root: Path, max_bytes: "int | None" = None) -> str:
    if not files:
        return ""

    budget = _resolve_budget(max_bytes, _DEFAULT_FILE_MODE_MAX_BYTES)
    entries: list[tuple[str, Path, str]] = []
    for filepath in files:
        p = Path(filepath)
        if not p.is_absolute():
            p = (root / filepath).resolve()
        entries.append((filepath, p, ""))
    blocks, tally, used = _pack_blocks(entries, budget, 0)

    degraded = tally["excerpt"] or tally["path_only"]
    header = (
        "## Pre-loaded File Contents\n\n"
        + _budget_line(budget, used, tally)
        + (
            "Files shown in full below are pre-loaded. "
            "Do NOT call Read, Grep, or Glob for those files — use the content below directly. "
            if not degraded else
            "Files shown in full below are pre-loaded — do NOT call Read for those. "
            "Excerpted and not-pre-loaded files exceeded the byte budget: Read them "
            "(or the parts you need) when you get to them. "
        )
        + "This eliminates unnecessary tool calls and keeps your context small.\n\n"
    )
    return header + "\n".join(blocks)


def build_diff_context(snapshot_sha: str, root: Path, max_bytes: "int | None" = None) -> str:
    # Validate the SHA resolves to a commit before passing it to git diff.
    # An abbreviated/wrong SHA that fails to resolve made `git diff` produce
    # an empty diff silently, leading to a 1-byte sidecar. Surface the bad
    # SHA via stderr + non-empty return so the caller notices.
    verify = subprocess.run(
        [_GIT or "git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{snapshot_sha}^{{commit}}"],
        capture_output=True, text=True,
    )
    if verify.returncode != 0:
        print(
            f"build_prompt_context: snapshot_sha {snapshot_sha!r} does not "
            "resolve to a commit (use the full 40-char SHA from "
            "manifest.snapshot.head_sha)",
            file=sys.stderr,
        )
        return ""
    try:
        result = subprocess.run(
            [_GIT or "git", "-C", str(root), "diff", "--name-only", "--diff-filter=AMRD", snapshot_sha],
            capture_output=True, text=True, check=True,
        )
        changed = [f.strip() for f in result.stdout.splitlines() if f.strip()]
    except subprocess.CalledProcessError:
        return ""

    if not changed:
        return ""

    try:
        diff_result = subprocess.run(
            [_GIT or "git", "-C", str(root), "diff", snapshot_sha, "--", *changed],
            capture_output=True, text=True, check=True,
        )
        diff_text = diff_result.stdout
    except subprocess.CalledProcessError:
        diff_text = ""

    budget = _resolve_budget(max_bytes, _DEFAULT_DIFF_MODE_MAX_BYTES)
    blocks: list[str] = []

    # Include the unified diff if not too large
    if diff_text and _nbytes(diff_text) <= budget // 2:
        blocks.append("### Unified Diff\n\n```diff\n" + diff_text[:_MAX_FILE_CHARS] + "\n```\n")

    # Include current content of each changed file, degrading past the budget
    used = sum(_nbytes(b) for b in blocks)
    entries = [
        (filepath, (root / filepath).resolve(), " (current)")
        for filepath in changed
        if (root / filepath).resolve().exists()
    ]
    file_blocks, tally, used = _pack_blocks(entries, budget, used)
    blocks.extend(file_blocks)

    if not blocks:
        return ""

    header = (
        "## Pre-loaded Diff Context\n\n"
        f"Changed files since snapshot `{snapshot_sha[:12]}`: "
        + ", ".join(f"`{f}`" for f in changed)
        + ".\n\n"
        + _budget_line(budget, used, tally)
        + "Files shown in full below are pre-loaded. "
        "Do NOT call Read, Grep, or Glob for those — use the content here directly. "
        "Excerpted or not-pre-loaded files (if any) exceeded the byte budget: Read them on demand.\n\n"
    )
    return header + "\n".join(blocks)


def _resolve_task_dir_snapshot(task_dir: Path) -> str:
    """Read manifest.snapshot.head_sha from a task dir and return it.

    Raises ValueError when the manifest is absent / unreadable / lacks a
    snapshot. The error message names the exact path so the operator can
    fix the inputs without spelunking. Eliminates the abbreviated-SHA
    failure mode of --diff because the SHA is read directly from the
    state-machine's authoritative record rather than typed by hand.
    """
    import json as _json
    manifest_path = task_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"manifest.json not found at {manifest_path}")
    try:
        manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"failed to read {manifest_path}: {exc}") from exc
    snapshot = manifest.get("snapshot") if isinstance(manifest, dict) else None
    if not isinstance(snapshot, dict):
        raise ValueError(
            f"{manifest_path} has no `snapshot` field — run "
            "`ctl record-snapshot` before invoking --task-dir"
        )
    head_sha = snapshot.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha.strip():
        raise ValueError(
            f"{manifest_path}.snapshot.head_sha is empty or missing"
        )
    return head_sha.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build pre-loaded file context for agent prompts")
    parser.add_argument("files", nargs="*", help="File paths to pre-load")
    parser.add_argument("--root", default=".", help="Repo root (default: cwd)")
    parser.add_argument("--diff", metavar="SHA", help="Pre-load files changed since this git SHA")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=None,
        help=(
            "Byte budget for the emitted context (default "
            f"{_DEFAULT_FILE_MODE_MAX_BYTES} for file lists, "
            f"{_DEFAULT_DIFF_MODE_MAX_BYTES} for --diff/--task-dir; env "
            f"{_BUDGET_ENV} overrides the default). Past the budget files "
            "degrade to head excerpts, then to a path-only listing."
        ),
    )
    parser.add_argument(
        "--task-dir",
        metavar="PATH",
        default=None,
        help=(
            "Pre-load files changed since the snapshot recorded in "
            "PATH/manifest.json. Reads `snapshot.head_sha` from the "
            "manifest — preferred over --diff for residual-drain "
            "workflows because it removes the SHA-handling failure "
            "mode (abbreviated/wrong SHAs passed to --diff produced "
            "a 1-byte sidecar silently)."
        ),
    )
    parser.add_argument(
        "--sidecar",
        metavar="PATH",
        default=None,
        help=(
            "Write context to PATH instead of stdout. Stdout receives a "
            "short pointer line referencing the path. The audit-skill "
            "orchestrator should use this so the auditor's base prompt "
            "carries the path (~80 bytes) instead of the full 150K-char "
            "blob, and each auditor reads the file with one Read call. "
            "Stops the (auditors x cascade-models x context-size) "
            "input-token multiplication that the 2026-04-30 latency "
            "investigation flagged."
        ),
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()

    # --task-dir and --diff are mutually exclusive. If both are passed,
    # --task-dir wins (it's the authoritative source of the snapshot
    # SHA) and --diff is silently ignored.
    if args.task_dir:
        task_dir_path = Path(args.task_dir)
        if not task_dir_path.is_absolute():
            task_dir_path = (root / task_dir_path).resolve()
        try:
            sha_from_manifest = _resolve_task_dir_snapshot(task_dir_path)
        except ValueError as exc:
            print(f"build_prompt_context: {exc}", file=sys.stderr)
            sys.exit(1)
        content = build_diff_context(sha_from_manifest, root, args.max_bytes)
    elif args.diff:
        content = build_diff_context(args.diff, root, args.max_bytes)
    elif args.files:
        content = build_file_context(args.files, root, args.max_bytes)
    else:
        parser.print_help()
        sys.exit(1)

    if args.sidecar:
        sidecar_path = Path(args.sidecar)
        try:
            sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            # Match stdout-mode trailing newline so sidecar content is
            # byte-identical to what `python3 build_prompt_context.py ...`
            # would have printed without --sidecar.
            sidecar_path.write_text(content + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"build_prompt_context: failed to write sidecar {sidecar_path}: {exc}", file=sys.stderr)
            sys.exit(1)
        # Emit a minimal pointer so callers can pipe stdout into a prompt
        # without dragging the full content along.
        print(f"Context written to {sidecar_path}. Read it once at the start of your work.")
    else:
        print(content)


if __name__ == "__main__":
    main()
