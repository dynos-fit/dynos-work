"""Deterministic knowledge of what the codebase already defines.

Three things agents cannot work out cheaply inside their read budgets, all
computed without a model:

- `build_map`        top-level definitions per folder, inside a character
                     budget, so "reuse what exists" has something to draw on.
- `build_slice`      the same for the folders one segment works in, plus the
                     project's shared code, for an executor prompt.
- `analyze_changes`  what a task's diff did to that picture: definitions it
                     created, existing ones it reused, and definitions it
                     removed or renamed that are still referenced somewhere
                     (open callers).

The approach is adapted from Ponytail's codebase map and caller tracking
(MIT; see THIRD_PARTY_NOTICES.md). Definitions are found with one regex per
language, not a parser: nested and dynamically created definitions are
missed, so every result is evidence for an agent to verify, never a gate.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

MAX_FILES = 4000
MAX_FILE_BYTES = 65536
DEFAULT_MAP_CHARS = 6000
DEFAULT_SLICE_CHARS = 1500
MAX_OPEN_CALLERS = 20
MAX_CALL_SITES = 5

_GIT: str | None = shutil.which("git")
_GIT_TIMEOUT_S = 10

_SRC = re.compile(r"\.(py|js|jsx|mjs|cjs|ts|tsx|go|rs|rb|java|kt|php|swift|cs)$")
# Tests, vendored and generated code are not something to reuse.
_SKIP_DIR = re.compile(
    r"(^|/)(node_modules|\.git|\.dynos|dist|build|out|vendor|venv|\.venv|__pycache__|coverage|\.next|target"
    r"|migrations|versions|fixtures|tests?|__tests__|spec)(/|$)"
)
_NOT_OURS = re.compile(r"(^|/)(node_modules|\.git|\.dynos|dist|build|out|vendor|venv|\.venv|__pycache__|\.next|target)(/|$)")
_SKIP_FILE = re.compile(r"(^|/)(test_[^/]*|[^/]*(_test|\.test|\.spec|\.gen|\.min|\.d)\.[a-z]+)$")
_SHARED = re.compile(r"(util|helper|lib|common|shared|service|component|hook|core|model|schema|api)", re.I)
_SAFE_PATH = re.compile(r"^[\w.@+$()\[\]/ -]+$")
_EXPORT_NAME = re.compile(r"^(?:type\s+)?(?:[\w$]+\s+as\s+)?([A-Za-z_$][\w$]*)$")

_M = re.MULTILINE
_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "py": [re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)", _M), re.compile(r"^class\s+([A-Za-z_]\w*)", _M)],
    "js": [
        re.compile(
            r"^export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|class|const|let|var|interface|type|enum)"
            r"\s+([A-Za-z_$][\w$]*)", _M,
        ),
        re.compile(r"^(?:async\s+)?function\*?\s+([A-Za-z_$][\w$]*)", _M),
        re.compile(r"^export\s*\{([^}]*)\}", _M),
    ],
    "go": [re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z]\w*)", _M), re.compile(r"^type\s+([A-Za-z]\w*)", _M)],
    "rs": [re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait|type)\s+(\w+)", _M)],
    "rb": [re.compile(r"^\s*(?:def|class|module)\s+(?:self\.)?([A-Za-z][\w]*[?!]?)", _M)],
    "jvm": [
        re.compile(
            r"^\s*(?:(?:public|internal|open|final|abstract|static|sealed|data)\s+)*"
            r"(?:class|interface|enum|record|struct|object)\s+(\w+)", _M,
        )
    ],
}
_LANG = {
    "py": "py", "js": "js", "jsx": "js", "mjs": "js", "cjs": "js", "ts": "js", "tsx": "js", "go": "go",
    "rs": "rs", "rb": "rb", "java": "jvm", "kt": "jvm", "swift": "jvm", "cs": "jvm", "php": "jvm",
}

# Names too common to say anything about reuse or callers.
_GENERIC = frozenset({
    "main", "init", "run", "get", "set", "test", "setup", "open", "close", "read", "write", "start", "stop",
    "load", "save", "update", "create", "delete", "handle", "render", "index", "new", "call", "apply",
    "name", "type", "value", "data", "config", "default", "props", "state", "error", "result",
})


def is_source(path: str) -> bool:
    """A source file whose definitions count: not a test, vendored or generated."""
    return bool(_SRC.search(path)) and not _SKIP_DIR.search(path) and not _SKIP_FILE.search(path)


def definitions(path: str, text: str, *, include_private: bool = True) -> list[str]:
    """Top-level definition names in *text*, in order of appearance."""
    lang = _LANG.get(PurePosixPath(path).suffix.lstrip("."))
    found: list[tuple[int, str]] = []
    for pattern in _PATTERNS.get(lang or "", []):
        for match in pattern.finditer(text):
            for part in match.group(1).split(","):
                exported = _EXPORT_NAME.match(part.strip())
                if exported:
                    found.append((match.start(), exported.group(1)))
    names: list[str] = []
    for _, name in sorted(found, key=lambda item: item[0]):
        if name in names or (not include_private and name.startswith("_")):
            continue
        names.append(name)
    return names


def _git(root: Path, *args: str) -> str | None:
    if _GIT is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603
            [_GIT, *args], cwd=str(root), capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode in (0, 1) else None


def list_files(root: Path) -> list[str]:
    """Repo-relative files: tracked plus untracked-but-not-ignored, else a short walk."""
    out = _git(root, "ls-files", "--cached", "--others", "--exclude-standard")
    if out:
        files = [line for line in out.split("\n") if line]
        if files:
            return files[: MAX_FILES * 5]
    found: list[str] = []

    def walk(directory: Path, depth: int) -> None:
        if depth > 6 or len(found) > MAX_FILES * 5:
            return
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            rel = entry.relative_to(root).as_posix()
            if entry.is_dir() and not entry.is_symlink():
                if not _SKIP_DIR.search(rel + "/"):
                    walk(entry, depth + 1)
            elif entry.is_file():
                found.append(rel)

    walk(root, 0)
    return found


def _read(root: Path, rel: str) -> str | None:
    path = root / rel
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as handle:
            return handle.read(MAX_FILE_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None


def _source_files(root: Path) -> list[str]:
    files = [f for f in list_files(root) if _SAFE_PATH.match(f) and is_source(f)]
    return files[:MAX_FILES]


def _folder(rel: str) -> str:
    parent = PurePosixPath(rel).parent.as_posix()
    return "." if parent in ("", ".") else parent


def _render(per_folder: dict[str, list[list[str]]], order: list[str], max_chars: int) -> tuple[list[str], int]:
    lines: list[str] = []
    used = 0
    skipped = 0
    for folder in order:
        per_file = per_folder[folder]
        # The first names say what a file is; in a folder of many files one name each.
        take = 1 if len(per_file) > 8 else 3
        names: list[str] = []
        for file_names in per_file:
            for name in file_names[:take]:
                if name not in names:
                    names.append(name)
        line = f"{folder}/: {', '.join(names[:30])}{', ...' if len(names) > 30 else ''}"
        if used + len(line) > max_chars:
            skipped += 1
            continue
        lines.append(line)
        used += len(line) + 1
    return lines, skipped


def _collect(root: Path, files: list[str]) -> dict[str, list[list[str]]]:
    per_folder: dict[str, list[list[str]]] = {}
    for rel in files:
        text = _read(root, rel)
        if text is None:
            continue
        names = definitions(rel, text, include_private=False)
        if names:
            per_folder.setdefault(_folder(rel), []).append(names)
    return per_folder


def build_map(root: Path, max_chars: int = DEFAULT_MAP_CHARS) -> str:
    """One line per folder of what the project defines; shared code first. "" when nothing is found."""
    per_folder = _collect(root, _source_files(root))
    order = sorted(per_folder, key=lambda folder: (not _SHARED.search(folder), folder))
    lines, skipped = _render(per_folder, order, max_chars)
    if not lines:
        return ""
    tail = f"\n({skipped} more folders not listed)" if skipped else ""
    return (
        "Codebase map (top-level definitions that already exist, by folder; reuse them, and read a file "
        "only when you need its details):\n" + "\n".join(lines) + tail
    )


def build_slice(root: Path, files_expected: list[str], max_chars: int = DEFAULT_SLICE_CHARS) -> str:
    """What exists near a segment: its own folders file by file, then shared folders.

    Files in *files_expected* are left out; the executor already has their
    contents. Within a folder, files that look shared (lib, util, helper...)
    come first, since those are the ones worth reusing.
    """
    expected = {f for f in files_expected if isinstance(f, str) and f}
    own = {_folder(f) for f in expected}
    if not own:
        return ""
    near = own | {PurePosixPath(folder).parent.as_posix() for folder in own if folder != "."}
    near.discard("")
    sources = _source_files(root)

    lines: list[str] = []
    used = 0
    skipped = 0
    own_files = sorted(
        (f for f in sources if _folder(f) in own and f not in expected),
        key=lambda f: (not _SHARED.search(PurePosixPath(f).name), f),
    )
    for rel in own_files[:200]:
        text = _read(root, rel)
        names = definitions(rel, text, include_private=False) if text is not None else []
        if not names:
            continue
        line = f"{rel}: {', '.join(names[:6])}{', ...' if len(names) > 6 else ''}"
        if used + len(line) > max_chars * 2 // 3:
            skipped += 1
            continue
        lines.append(line)
        used += len(line) + 1

    others = [f for f in sources if _folder(f) not in own and (_folder(f) in near or _SHARED.search(_folder(f)))]
    per_folder = _collect(root, others[:400])
    order = sorted(per_folder, key=lambda folder: (folder not in near, folder))
    folder_lines, folder_skipped = _render(per_folder, order, max(0, max_chars - used))
    lines.extend(folder_lines)
    skipped += folder_skipped
    if not lines:
        return ""
    tail = f"\n({skipped} more files or folders not listed)" if skipped else ""
    return "\n".join(lines) + tail


def _changed_files(root: Path, base: str) -> list[str] | None:
    tracked = _git(root, "diff", "--name-only", base, "--")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard")
    if tracked is None:
        return None
    names = [line for line in (tracked + "\n" + (untracked or "")).split("\n") if line]
    return sorted(set(names))


def _show(root: Path, base: str, rel: str) -> str:
    out = _git(root, "show", f"{base}:{rel}")
    return (out or "")[:MAX_FILE_BYTES]


def _added_lines(root: Path, base: str, rel: str, new_text: str, existed: bool) -> str:
    if not existed:
        return new_text
    diff = _git(root, "diff", "--unified=0", base, "--", rel) or ""
    return "\n".join(
        line[1:] for line in diff.split("\n") if line.startswith("+") and not line.startswith("+++")
    )


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")


def _use(name: str) -> re.Pattern[str]:
    """A call, a JSX element or an import of *name*; a mention in a comment is not a use."""
    escaped = re.escape(name)
    return re.compile(
        rf"(?<![\w$]){escaped}\s*\(|<{escaped}[\s/>]|^\s*(?:from|import|export)\b.*(?<![\w$]){escaped}(?![\w$])",
        re.MULTILINE,
    )


def _is_definition_line(rel: str, line: str, name: str) -> bool:
    return name in definitions(rel, line)


def analyze_changes(root: Path, base: str) -> dict | None:
    """What the diff since *base* did to the project's definitions.

    Returns None when git cannot answer. Otherwise:
      created       definitions added to non-test source files
      reused        existing project definitions the added lines refer to
      open_callers  definitions removed (or renamed) and defined nowhere now,
                    that other code still refers to, with each call site and
                    the text of its line (an auditor cannot open files outside
                    the diff, so the line itself is the evidence)
    """
    changed = _changed_files(root, base)
    if changed is None:
        return None
    code = [f for f in changed if _SAFE_PATH.match(f) and _SRC.search(f) and not _SKIP_DIR.search(f)]

    created: list[str] = []
    removed: dict[str, str] = {}
    added_text: list[str] = []
    new_defs_all: set[str] = set()
    for rel in code:
        old_text = _show(root, base, rel)
        new_text = _read(root, rel) or ""
        old_defs = definitions(rel, old_text)
        new_defs = definitions(rel, new_text)
        new_defs_all.update(new_defs)
        for name in old_defs:
            if name not in new_defs:
                removed.setdefault(name, rel)
        if is_source(rel):
            for name in new_defs:
                if name not in old_defs and name not in created:
                    created.append(name)
            added_text.append(_added_lines(root, base, rel, new_text, bool(old_text)))

    # Existing definitions = everything the project defines now, minus what this diff created.
    known: set[str] = set()
    for rel in _source_files(root):
        text = _read(root, rel)
        if text is not None:
            known.update(definitions(rel, text))
    known -= set(created)
    added = "\n".join(added_text)
    reused = sorted(
        name for name in known
        if len(name) > 3 and name not in _GENERIC and name in added and _use(name).search(added)
    )

    open_callers: list[dict] = []
    for name, removed_from in sorted(removed.items()):
        if len(open_callers) >= MAX_OPEN_CALLERS:
            break
        if len(name) <= 3 or name in _GENERIC or name in new_defs_all:
            continue
        hits = _git(root, "grep", "-n", "-w", "-F", "--untracked", "-e", name, "--", ".") or ""
        sites: list[dict] = []
        redefined = False
        pattern = _word(name)
        for row in hits.split("\n"):
            match = re.match(r"^(.+?):(\d+):(.*)$", row)
            if not match:
                continue
            rel, line_no, text = match.group(1), match.group(2), match.group(3)
            # Tests count as callers; vendored and build output do not.
            if not _SRC.search(rel) or _NOT_OURS.search(rel):
                continue
            if not pattern.search(text):
                continue
            stripped = text.lstrip()
            if stripped.startswith(("#", "//", "*", "/*")):
                continue
            if _is_definition_line(rel, text, name):
                redefined = True
                break
            if len(sites) < MAX_CALL_SITES:
                sites.append({"site": f"{rel}:{line_no}", "text": " ".join(text.split())[:160]})
        if redefined or not sites:
            continue
        open_callers.append({"name": name, "removed_from": removed_from, "call_sites": sites})

    return {
        "base": base,
        "created": created,
        "reused": reused,
        "open_callers": open_callers,
        "reuse_ratio": round(len(reused) / (len(reused) + len(created)), 2) if (reused or created) else None,
    }
