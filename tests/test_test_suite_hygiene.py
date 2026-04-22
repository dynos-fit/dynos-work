from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

_BANNED_SNIPPETS = (
    "TODAY these tests FAIL",
    "This import MUST fail today",
    "MUST fail today",
    "collection itself will error",
)


def test_no_stale_intentional_red_state_scaffolding_in_default_tests() -> None:
    offenders: list[str] = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == "test_test_suite_hygiene.py":
            continue
        text = path.read_text(encoding="utf-8")
        for snippet in _BANNED_SNIPPETS:
            if snippet in text:
                offenders.append(f"{path.relative_to(ROOT)}: {snippet}")
    assert not offenders, (
        "Default test suite contains stale intentional-red scaffolding:\n"
        + "\n".join(offenders)
    )
