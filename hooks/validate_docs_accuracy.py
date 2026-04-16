#!/usr/bin/env python3
"""Verify that file paths cited in markdown docs actually exist on disk.

Catches a common LLM hallucination class: docs that reference files,
commands, or paths that don't exist. Examples:
  - README says "see src/auth.py" but src/auth.py was never created.
  - Setup guide says "run npm run dev" but there's no package.json.
  - Migration doc references "scripts/migrate.sh" — script doesn't exist.

The check is language-agnostic — paths are paths regardless of stack.
We only flag paths that look DISTINCTLY like project-relative file/dir
references, ignoring URLs, env vars, globs, and command names.

Usage:
    python3 hooks/validate_docs_accuracy.py --doc README.md
    python3 hooks/validate_docs_accuracy.py --doc README.md --root .
    python3 hooks/validate_docs_accuracy.py --doc README.md --json
    python3 hooks/validate_docs_accuracy.py --root . --recursive

Exit code:
    0 — all referenced paths exist
    1 — at least one broken reference found
    2 — could not run (no doc found, etc.)

Output (--json mode): structured report with broken refs + line numbers.
Default: human-readable summary.

This module ships standalone in PR #7. Skill wiring (invoke from
code-quality-auditor on any task that touched .md files) lands later.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Path extraction
# ---------------------------------------------------------------------------

# Fenced code block boundaries: ```lang ... ``` or ~~~lang ... ~~~
FENCE_RE = re.compile(r"^(```|~~~)([\w+-]*)\s*$")

# Patterns we treat as "looks like a project-relative path":
#  - Contains at least one `/` (filters out bare command names)
#  - Has a recognizable shape (alphanumeric segments + slashes)
#  - May end in a file extension, may be a directory
PATH_CANDIDATE_RE = re.compile(
    r"""
    (?<![\w/.-])               # left boundary: not preceded by word char or path char
    (?P<path>
        \.{1,2}/                       # ./ or ../
        [\w./@-]+                       # path body
      |
        [\w][\w@-]*                     # first segment (no leading dot)
        (?:/[\w@.-]+)+                  # one or more / segments
    )
    (?![\w/.])                  # right boundary: not followed by more path chars
    """,
    re.VERBOSE,
)

# Things to filter out — these LOOK like paths but aren't real ones to check:
URL_PREFIXES = ("http://", "https://", "ftp://", "ftps://", "ssh://", "git://", "mailto:", "tel:")
ENV_VAR_RE = re.compile(r"\$\{?[A-Z_][A-Z0-9_]*\}?")
TEMPLATE_VAR_RE = re.compile(r"\{[\w.-]+\}|\{\{[\w.-]+\}\}")  # {var} or {{var}}
GLOB_CHARS = set("*?[]")


@dataclass
class PathRef:
    path: str          # the raw path as cited
    line: int          # line number in the doc
    in_code_block: bool  # was this inside a fenced code block?
    code_lang: str | None  # the language tag if known (python, bash, etc.)


def extract_path_candidates(doc_path: Path) -> list[PathRef]:
    """Walk the doc, extract every string that looks distinctly like a
    project-relative file or directory path.

    Conservative: false-negative > false-positive. We'd rather miss a real
    broken ref than spam warnings on prose that isn't actually a path.
    """
    text = doc_path.read_text()
    lines = text.splitlines()

    refs: list[PathRef] = []
    in_block = False
    block_lang: str | None = None

    for lineno, line in enumerate(lines, start=1):
        # Track fenced code block boundaries
        m = FENCE_RE.match(line.strip())
        if m:
            if in_block:
                in_block = False
                block_lang = None
            else:
                in_block = True
                block_lang = m.group(2) or None
            continue

        # Mine the line for path candidates
        for cand_match in PATH_CANDIDATE_RE.finditer(line):
            raw = cand_match.group("path")
            if not _looks_like_real_path(raw, line, cand_match.start()):
                continue
            refs.append(PathRef(
                path=raw, line=lineno,
                in_code_block=in_block, code_lang=block_lang,
            ))

    return _dedup(refs)


# File extensions that signal "this is a real file path, please check it."
# Conservative list — anything missing here just gets caught by the 3+ segment
# rule or the explicit relative-prefix rule below.
KNOWN_FILE_EXTENSIONS = frozenset({
    "py", "ts", "tsx", "js", "jsx", "mjs", "cjs",
    "md", "mdx", "rst", "txt",
    "json", "yml", "yaml", "toml", "ini", "cfg", "env",
    "sh", "bash", "zsh", "fish", "ps1", "bat", "cmd",
    "html", "css", "scss", "sass", "less", "xml", "svg",
    "go", "rs", "java", "kt", "swift", "dart", "rb", "php", "lua", "pl",
    "c", "h", "cpp", "hpp", "cc", "hh", "cs",
    "sql", "csv", "tsv", "log",
    "lock", "gitignore", "dockerfile", "dockerignore",
    "png", "jpg", "jpeg", "gif", "webp", "ico", "pdf",
    "proto", "graphql", "prisma",
})


def _has_file_extension(candidate: str) -> bool:
    """Does the candidate end with a recognized file extension?"""
    last_seg = candidate.rsplit("/", 1)[-1]
    if "." not in last_seg:
        return False
    ext = last_seg.rsplit(".", 1)[-1].lower()
    return ext in KNOWN_FILE_EXTENSIONS


def _looks_like_real_path(candidate: str, full_line: str, start_offset: int) -> bool:
    """Filter rules: returns False for things that LOOK pathy but aren't.

    Strict mode: a candidate is a "real path to check" only if at least one
    strong signal applies (explicit relative prefix, recognized file extension,
    or 3+ segments with no all-caps segments). Two-segment 'org/repo' or
    'STATE/CONSTANT' patterns are filtered out as ambiguous.
    """
    # URL-shaped or URL-suffixed tokens
    if any(candidate.startswith(p) for p in URL_PREFIXES):
        return False
    line_before = full_line[:start_offset]
    if line_before.endswith(("http://", "https://", "ftp://", "ssh://", "git://", "://")):
        return False

    # Env vars and template placeholders
    if ENV_VAR_RE.search(candidate) or TEMPLATE_VAR_RE.search(candidate):
        return False

    # Tilde / home paths — outside the project boundary
    if candidate.startswith("~"):
        return False

    # Glob patterns — not literal paths
    if any(c in candidate for c in GLOB_CHARS):
        return False

    # Domain-shaped first segment (`github.com/...`, `example.com/...`)
    # Same heuristic as before but only filters when the candidate doesn't
    # also have a known file extension at the end (so `mod.foo/bar.py` survives).
    first_seg = candidate.split("/", 1)[0]
    if "." in first_seg and first_seg not in ("..", ".") and not first_seg.startswith("."):
        suffix = first_seg.rsplit(".", 1)[-1]
        if suffix.isalpha() and 2 <= len(suffix) <= 6 and not suffix.isupper():
            if not _has_file_extension(candidate):
                return False

    # Length sanity
    if "/" not in candidate or len(candidate) > 256:
        return False

    # ---- Strong-signal gate ----
    # Only flag candidates that are unambiguously file-path-like:
    #   1. Explicit relative prefix (./foo or ../bar) — author meant a path
    #   2. Recognized file extension at the end (foo.py, scripts/run.sh)
    #
    # Bare directory references like 'src/components' get skipped (false
    # negative) — but this avoids the larger noise class of slash-separated
    # concept lists ('Yes/No/Modify', 'DONE/FAILED'), marketplace IDs
    # ('org/repo'), branch names, and fractions. Any real broken file ref
    # underneath a missed directory will still be caught (the file path
    # itself has the extension).
    if candidate.startswith(("./", "../")):
        return True
    if _has_file_extension(candidate):
        return True
    return False


def _dedup(refs: list[PathRef]) -> list[PathRef]:
    """Same path on the same line is a duplicate; keep first occurrence."""
    seen: set[tuple[str, int]] = set()
    out: list[PathRef] = []
    for r in refs:
        key = (r.path, r.line)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Existence check
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    ref: PathRef
    exists: bool
    resolved_at: str | None  # the actual resolved path that was found, or None


def check_one(ref: PathRef, repo_root: Path, doc_dir: Path) -> CheckResult:
    """Check if a path exists. Try repo-root-relative first, then doc-dir-relative."""
    raw = ref.path

    # Strip leading ./ for clarity
    candidate = raw.lstrip("./") if raw.startswith("./") else raw

    # Try repo-root-relative
    p1 = (repo_root / candidate).resolve()
    if _safe_exists(p1, repo_root):
        return CheckResult(ref=ref, exists=True, resolved_at=str(p1.relative_to(repo_root)))

    # Try doc-dir-relative (for paths like ../config/foo)
    if doc_dir != repo_root:
        p2 = (doc_dir / raw).resolve()
        if _safe_exists(p2, repo_root):
            try:
                rel = p2.relative_to(repo_root)
                return CheckResult(ref=ref, exists=True, resolved_at=str(rel))
            except ValueError:
                # Path is outside repo root — accept but note absolute
                return CheckResult(ref=ref, exists=True, resolved_at=str(p2))

    return CheckResult(ref=ref, exists=False, resolved_at=None)


def _safe_exists(path: Path, repo_root: Path) -> bool:
    """Existence check with a guard against escaping the repo root.

    A doc could reference '../../etc/passwd' — we treat references to outside
    the repo as 'not real project files' regardless of whether they exist on
    the host filesystem.
    """
    try:
        path.relative_to(repo_root)
    except ValueError:
        # Outside repo root — treat as non-existent for our purposes
        return False
    return path.exists()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def render_human(doc: Path, results: list[CheckResult]) -> str:
    if not results:
        return f"OK: no path references found in {doc.name}"
    broken = [r for r in results if not r.exists]
    if not broken:
        return f"OK: all {len(results)} path reference(s) in {doc.name} exist"
    lines = [f"BROKEN PATH REFERENCES in {doc.name} ({len(broken)} of {len(results)} total):"]
    for r in broken:
        loc = "code block" if r.ref.in_code_block else "prose"
        lang = f" [{r.ref.code_lang}]" if r.ref.code_lang else ""
        lines.append(f"  - line {r.ref.line:>4}: {r.ref.path:<40} ({loc}{lang})")
    return "\n".join(lines)


def render_json(doc: Path, results: list[CheckResult]) -> str:
    out = {
        "doc": str(doc),
        "total": len(results),
        "broken": [
            {
                "path": r.ref.path,
                "line": r.ref.line,
                "in_code_block": r.ref.in_code_block,
                "code_lang": r.ref.code_lang,
            }
            for r in results if not r.exists
        ],
        "verified": [
            {"path": r.ref.path, "line": r.ref.line, "resolved_at": r.resolved_at}
            for r in results if r.exists
        ],
    }
    return json.dumps(out, indent=2)


def discover_docs(root: Path) -> list[Path]:
    """Recursive doc discovery: every .md file under root, excluding common
    noise dirs (.git, node_modules, dist, __pycache__, etc.)."""
    EXCLUDE_DIRS = {
        ".git", "node_modules", "__pycache__", "dist", "build",
        ".venv", "venv", ".pytest_cache",
        ".dynos",  # task state artifacts; doc-like but ephemeral and may reference
                   # files that exist only on the branch the task was run from
    }
    docs: list[Path] = []
    for path in root.rglob("*.md"):
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        docs.append(path)
    return sorted(docs)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Verify file paths cited in markdown docs actually exist."
    )
    ap.add_argument("--doc", type=Path, help="Path to a single markdown file")
    ap.add_argument("--root", type=Path, default=Path.cwd(),
                    help="Repo root used to resolve relative paths (default: cwd)")
    ap.add_argument("--recursive", action="store_true",
                    help="Walk --root recursively for *.md files instead of --doc")
    ap.add_argument("--json", action="store_true",
                    help="Emit machine-readable JSON instead of human report.")
    args = ap.parse_args()

    if not args.recursive and not args.doc:
        print("must pass either --doc <file> or --recursive", file=sys.stderr)
        return 2

    repo_root = args.root.resolve()

    if args.recursive:
        docs = discover_docs(repo_root)
    else:
        if not args.doc.exists():
            print(f"doc not found: {args.doc}", file=sys.stderr)
            return 2
        docs = [args.doc]

    any_broken = False
    all_results: dict[str, list[CheckResult]] = {}
    for doc in docs:
        refs = extract_path_candidates(doc)
        results = [check_one(r, repo_root, doc.parent.resolve()) for r in refs]
        all_results[str(doc)] = results
        if any(not r.exists for r in results):
            any_broken = True
        if not args.json:
            print(render_human(doc, results))

    if args.json:
        if args.recursive:
            payload = [
                json.loads(render_json(Path(d), results))
                for d, results in all_results.items()
            ]
            print(json.dumps(payload, indent=2))
        else:
            print(render_json(docs[0], all_results[str(docs[0])]))

    return 1 if any_broken else 0


if __name__ == "__main__":
    sys.exit(main())
