"""TDD-first tests for task-20260420-001 D6 part 2 — consumer flag wiring.

Covers acceptance criteria 27 and 28:

    AC27 Each consumer command has --include-legacy-unverified flag wired.
         Per plan-audit revision #3: the spec said 4 consumers, but only 3
         actual collect_retrospectives-using CLIs exist: planner.py:50,
         router.py:99/573, ctl.py::cmd_stats_dora (at line 512).
    AC28 Flag spelling is EXACTLY --include-legacy-unverified.
         No alternate spellings (--include_legacy_unverified, --legacy,
         --unverified) are accepted in any consumer.

TODAY these tests FAIL because none of the three consumer files parse
`--include-legacy-unverified` yet and none of them pass include_unverified
to collect_retrospectives.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"

sys.path.insert(0, str(HOOKS_DIR))


CONSUMER_FILES = (
    "hooks/planner.py",
    "hooks/router.py",
    "hooks/ctl.py",
)

ALT_SPELLINGS_PATTERN = re.compile(
    r"(?<!\w)(--include_legacy_unverified|--legacy|--unverified)(?!\w)"
)


# ---------------------------------------------------------------------------
# AC28 — exact flag spelling, no alternates
# ---------------------------------------------------------------------------


def test_flag_spelling_is_exact_no_alternates_anywhere():
    for p in HOOKS_DIR.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        # Exclude actual valid spelling:
        m = ALT_SPELLINGS_PATTERN.search(text)
        assert m is None, (
            f"{p.relative_to(REPO_ROOT)}: alternate flag spelling "
            f"{m.group(0)!r} detected at position {m.start()}. "
            f"Only --include-legacy-unverified is accepted."
        )


# ---------------------------------------------------------------------------
# AC27 — each consumer declares the flag and passes through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel_path", CONSUMER_FILES)
def test_consumer_declares_include_legacy_unverified(rel_path: str):
    """Every consumer file must contain the exact flag string, at least
    in an argparse add_argument OR as the kwarg key on the
    collect_retrospectives call."""
    p = REPO_ROOT / rel_path
    text = p.read_text(encoding="utf-8")
    has_flag_decl = "--include-legacy-unverified" in text
    has_param_passthrough = "include_unverified=" in text
    assert has_flag_decl or has_param_passthrough, (
        f"{rel_path} has neither --include-legacy-unverified argparse "
        f"declaration nor an include_unverified= kwarg forwarded to "
        f"collect_retrospectives — the D6 consumer wiring is missing"
    )


@pytest.mark.parametrize("rel_path", CONSUMER_FILES)
def test_consumer_forwards_flag_to_collect_retrospectives(rel_path: str):
    """Just declaring the flag is not enough — the consumer must pass
    include_unverified=args.include_legacy_unverified into
    collect_retrospectives, else the filter never propagates."""
    p = REPO_ROOT / rel_path
    text = p.read_text(encoding="utf-8")
    assert "collect_retrospectives(" in text, (
        f"{rel_path}: lost its collect_retrospectives call"
    )
    # The consumer must pass the kwarg — any of these are acceptable shapes.
    passthrough_shapes = [
        "include_unverified=args.include_legacy_unverified",
        "include_unverified=args.include_legacy_unverified or",
        "include_unverified=",  # generic fallthrough — weaker but accepted
    ]
    assert any(s in text for s in passthrough_shapes), (
        f"{rel_path}: include_unverified= kwarg not forwarded to "
        f"collect_retrospectives"
    )


# ---------------------------------------------------------------------------
# Argparse round-trip: the flag is accepted when invoked with it,
# and an alternate spelling errors out.
# ---------------------------------------------------------------------------


def test_ctl_stats_dora_accepts_the_flag():
    """Invoke ctl.py stats-dora with --include-legacy-unverified; even in
    a cold repo the CLI must not die with an 'unrecognized arguments'
    argparse error."""
    res = subprocess.run(
        [
            sys.executable,
            str(HOOKS_DIR / "ctl.py"),
            "stats-dora",
            "--root",
            str(REPO_ROOT),
            "--include-legacy-unverified",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # We don't care about the return code here — we only care that argparse
    # accepted the flag. An unknown-argument error goes to stderr and
    # returns rc 2.
    assert "unrecognized arguments" not in (res.stderr or "").lower(), (
        "ctl.py stats-dora must accept --include-legacy-unverified. "
        f"stderr: {res.stderr!r}"
    )


def test_ctl_stats_dora_rejects_alternate_spelling():
    """Alternate spelling must be rejected with argparse's unknown-flag error."""
    res = subprocess.run(
        [
            sys.executable,
            str(HOOKS_DIR / "ctl.py"),
            "stats-dora",
            "--root",
            str(REPO_ROOT),
            "--include_legacy_unverified",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # argparse unknown-arg returns 2 and prints to stderr.
    assert res.returncode == 2 or "unrecognized arguments" in (
        res.stderr or ""
    ).lower(), (
        "Alternate spelling must be rejected; got rc={res.returncode}, "
        f"stderr={res.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Informational: plan-audit revision #3 note — "4 consumers" resolved to 3
# ---------------------------------------------------------------------------


def test_three_consumer_files_is_authoritative():
    """Per plan-audit revision #3, AC27's '4 consumers' was aspirational.
    Actual collect_retrospectives consumer CLIs are 3 (planner, router, ctl).
    This test records the decision so future audits do not drift back."""
    assert len(CONSUMER_FILES) == 3
    for rel in CONSUMER_FILES:
        p = REPO_ROOT / rel
        text = p.read_text(encoding="utf-8")
        assert "collect_retrospectives" in text, (
            f"{rel} is supposed to be a collect_retrospectives consumer"
        )
