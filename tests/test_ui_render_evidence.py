"""Tests for the UI render-evidence step.

A UI plan declares `ui_render` targets in the execution graph; `ctl
run-ui-render-evidence` renders them at fixed widths and captures
screenshots plus measurements into ctl-owned `evidence/ui-render/`, which
the UI auditors read instead of estimating rendered behavior from a diff.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from lib_receipts import hash_file, read_receipt  # noqa: E402
from lib_ui_render import UI_RENDER_WIDTHS, capture, normalize_ui_render, summarize  # noqa: E402
from write_policy import WriteAttempt, decide_write, is_executor_evidence  # noqa: E402

from test_ctl import _run_ctl, _setup_task_dir  # noqa: E402


def _browser_available() -> bool:
    node = shutil.which("node")
    if node is None:
        return False
    script = (
        "const {createRequire}=require('node:module');"
        "for (const n of ['playwright','@playwright/test','playwright-core'])"
        "{try{if(createRequire(process.cwd()+'/package.json')(n).chromium){process.exit(0)}}catch{}}"
        "process.exit(1)"
    )
    return subprocess.run([node, "-e", script], capture_output=True, check=False).returncode == 0


needs_browser = pytest.mark.skipif(not _browser_available(), reason="no node + Playwright driver on this machine")

BAD_PAGE = """<!doctype html><html><head><title>t</title><style>
body { margin: 0; background: #ffffff; font: 16px sans-serif; }
.wide { width: 900px; height: 40px; background: #dddddd; }
.faint { color: #bbbbbb; }
.tiny { display: inline-block; width: 12px; height: 12px; padding: 0; border: 0; }
</style></head><body>
<h1>Settings</h1>
<p class="faint">Barely readable helper text</p>
<div class="wide">fixed width block</div>
<button class="tiny"></button><button class="tiny"></button>
<p style="margin-top:80px"><button class="tiny" aria-label="Alone"></button></p>
<button disabled style="color:#cccccc;background:#ffffff;min-width:60px;min-height:48px">Disabled faint</button>
<img src="missing.png">
</body></html>
"""

GOOD_PAGE = """<!doctype html><html><head><title>t</title><style>
body { margin: 0; background: #ffffff; color: #111111; font: 16px sans-serif; }
button { min-width: 48px; min-height: 48px; }
</style></head><body><main><h1>Settings</h1><p>Readable text</p><button>Save</button></main></body></html>
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _declare(task_dir: Path, ui_render: dict) -> None:
    graph = json.loads((task_dir / "execution-graph.json").read_text())
    graph["ui_render"] = ui_render
    (task_dir / "execution-graph.json").write_text(json.dumps(graph, indent=2))


# --- declaration validation -------------------------------------------------


def test_normalize_accepts_url_and_file_targets() -> None:
    out = normalize_ui_render({
        "start_command": " npm run dev ",
        "ready_timeout": 30,
        "targets": [
            {"id": "settings", "url": "http://localhost:5173/settings", "wait_for": "main"},
            {"id": "report", "file": "out/report.html"},
        ],
    })
    assert out["start_command"] == "npm run dev"
    assert out["ready_url"] == "http://localhost:5173/settings"
    assert out["ready_timeout"] == 30
    assert out["targets"] == [
        {"id": "settings", "url": "http://localhost:5173/settings", "wait_for": "main"},
        {"id": "report", "file": "out/report.html"},
    ]
    assert normalize_ui_render(None) is None


@pytest.mark.parametrize("bad, message", [
    ("nope", "must be an object"),
    ({"targets": []}, "non-empty array"),
    ({"targets": [{"id": "a", "url": "https://example.com/"}]}, "must point at this machine"),
    ({"targets": [{"id": "a", "url": "http://localhost.evil.com/"}]}, "must point at this machine"),
    ({"targets": [{"id": "a", "url": "http://user:pw@localhost/"}]}, "credentials"),
    ({"targets": [{"id": "a", "url": "file:///etc/passwd"}]}, "http(s) URL"),
    ({"targets": [{"id": "a", "file": "../outside.html"}]}, "inside the workspace"),
    ({"targets": [{"id": "a", "file": "/etc/passwd"}]}, "inside the workspace"),
    ({"targets": [{"id": "a"}]}, "exactly one of"),
    ({"targets": [{"id": "a", "url": "http://localhost/", "file": "x.html"}]}, "exactly one of"),
    ({"targets": [{"id": "Bad Id", "url": "http://localhost/"}]}, "lowercase"),
    ({"targets": [{"id": "../x", "url": "http://localhost/"}]}, "lowercase"),
    ({"targets": [{"id": "a", "url": "http://localhost/"}, {"id": "a", "url": "http://localhost/b"}]}, "duplicate"),
    ({"targets": [{"id": f"t{i}", "url": "http://localhost/"} for i in range(13)]}, "limited to"),
    ({"start_command": "x", "targets": [{"id": "a", "file": "a.html"}]}, "needs a ready_url"),
    ({"ready_timeout": 0, "targets": [{"id": "a", "url": "http://localhost/"}]}, "ready_timeout"),
    ({"ready_url": "http://10.0.0.5/", "targets": [{"id": "a", "url": "http://localhost/"}]}, "must point at this machine"),
])
def test_normalize_refuses_malformed_declarations(bad: object, message: str) -> None:
    with pytest.raises(ValueError) as exc:
        normalize_ui_render(bad)
    assert message in str(exc.value)


def test_write_execution_graph_keeps_valid_and_refuses_invalid_ui_render(tmp_path: Path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    graph = json.loads((task_dir / "execution-graph.json").read_text())

    graph["ui_render"] = {"targets": [{"id": "home", "url": "http://127.0.0.1:3000/"}]}
    payload = tmp_path / "graph.json"
    payload.write_text(json.dumps(graph))
    ok = _run_ctl("write-execution-graph", str(task_dir), "--from", str(payload))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    written = json.loads((task_dir / "execution-graph.json").read_text())
    assert written["ui_render"] == {"targets": [{"id": "home", "url": "http://127.0.0.1:3000/"}]}

    graph["ui_render"] = {"targets": [{"id": "home", "url": "https://example.com/"}]}
    payload.write_text(json.dumps(graph))
    refused = _run_ctl("write-execution-graph", str(task_dir), "--from", str(payload))
    assert refused.returncode == 1
    assert "must point at this machine" in refused.stderr
    # The refused payload did not replace the approved graph.
    assert json.loads((task_dir / "execution-graph.json").read_text()) == written


def test_rewriting_the_graph_keeps_ui_render_unless_removed(tmp_path: Path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    graph = json.loads((task_dir / "execution-graph.json").read_text())
    declared = {"targets": [{"id": "home", "url": "http://localhost:3000/"}]}
    payload = tmp_path / "graph.json"

    payload.write_text(json.dumps({**graph, "ui_render": declared}))
    assert _run_ctl("write-execution-graph", str(task_dir), "--from", str(payload)).returncode == 0
    # A repaired graph that does not mention ui_render keeps the declaration.
    payload.write_text(json.dumps(graph))
    assert _run_ctl("write-execution-graph", str(task_dir), "--from", str(payload)).returncode == 0
    assert json.loads((task_dir / "execution-graph.json").read_text())["ui_render"] == declared
    # An explicit null removes it.
    payload.write_text(json.dumps({**graph, "ui_render": None}))
    assert _run_ctl("write-execution-graph", str(task_dir), "--from", str(payload)).returncode == 0
    assert "ui_render" not in json.loads((task_dir / "execution-graph.json").read_text())


def test_graph_without_ui_render_is_unchanged(tmp_path: Path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    graph = json.loads((task_dir / "execution-graph.json").read_text())
    payload = tmp_path / "graph.json"
    payload.write_text(json.dumps(graph))
    assert _run_ctl("write-execution-graph", str(task_dir), "--from", str(payload)).returncode == 0
    assert "ui_render" not in json.loads((task_dir / "execution-graph.json").read_text())


# --- skip / fail paths (no browser needed) -----------------------------------


def test_no_declaration_records_a_skip_and_exits_zero(tmp_path: Path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    result = _run_ctl("run-ui-render-evidence", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "ui_render_skipped"

    report_path = task_dir / "evidence" / "ui-render" / "report.json"
    report = json.loads(report_path.read_text())
    assert report["status"] == "skipped"
    assert "declares no ui_render" in report["reason"]
    receipt = read_receipt(task_dir, "ui-render-evidence")
    assert receipt["artifact_sha256"] == hash_file(report_path)
    assert receipt["status"] == "skipped"


def test_missing_file_target_fails_and_exits_one(tmp_path: Path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    _declare(task_dir, {"targets": [{"id": "report", "file": "out/missing.html"}]})
    result = _run_ctl("run-ui-render-evidence", str(task_dir))
    if shutil.which("node") is None:
        assert result.returncode == 0
        return
    assert result.returncode == 1
    report = json.loads((task_dir / "evidence" / "ui-render" / "report.json").read_text())
    assert report["status"] == "failed"
    assert "file not found" in report["reason"]


def test_hand_edited_invalid_declaration_is_refused(tmp_path: Path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    _declare(task_dir, {"targets": [{"id": "x", "url": "https://example.com/"}]})
    result = _run_ctl("run-ui-render-evidence", str(task_dir))
    assert result.returncode == 1
    assert "invalid ui_render declaration" in result.stderr
    # A failed report replaces whatever an earlier capture left behind.
    report = json.loads((task_dir / "evidence" / "ui-render" / "report.json").read_text())
    assert report["status"] == "failed" and "invalid ui_render declaration" in report["reason"]


def test_capture_skips_without_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import lib_ui_render

    monkeypatch.setattr(lib_ui_render.shutil, "which", lambda _name: None)
    report = capture(tmp_path, tmp_path / "out", {"targets": [{"id": "a", "url": "http://localhost:1/"}]})
    assert report["status"] == "skipped"
    assert "node is not installed" in report["reason"]


def test_server_that_exits_early_is_reported_with_its_output(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    port = _free_port()
    declaration = normalize_ui_render({
        "start_command": "echo boom-from-server; exit 7",
        "ready_timeout": 20,
        "targets": [{"id": "home", "url": f"http://127.0.0.1:{port}/"}],
    })
    report = capture(tmp_path, tmp_path / "out", declaration)
    assert report["status"] == "failed"
    assert "exited with code 7" in report["reason"]
    assert "boom-from-server" in report["server_output_tail"]


def test_summarize_counts_across_viewports() -> None:
    totals = summarize([{"viewports": [
        {"load_error": None, "horizontal_overflow": {"overflows": True}, "low_contrast": [{}, {}],
         "small_targets": [{}], "contrast_unmeasured": 3, "targets_under_44": 2},
        {"load_error": "HTTP 500", "horizontal_overflow": {"overflows": False}, "console_errors": ["x"]},
    ]}])
    assert totals["viewports"] == 2
    assert totals["viewports_with_overflow"] == 1
    assert totals["load_errors"] == 1
    assert totals["low_contrast"] == 2
    assert totals["small_targets"] == 1
    assert totals["console_errors"] == 1
    assert totals["contrast_unmeasured"] == 3
    assert totals["targets_under_44"] == 2
    # Lists are capped per viewport; the *_total field carries the real count.
    capped = summarize([{"viewports": [{"low_contrast": [{}] * 20, "low_contrast_total": 57}]}])
    assert capped["low_contrast"] == 57


# --- write boundary ----------------------------------------------------------


def test_agent_roles_cannot_write_ui_render_evidence(tmp_path: Path) -> None:
    task_dir = tmp_path / ".dynos" / "task-20261116-001"
    task_dir.mkdir(parents=True)
    for name in ("report.json", "home-320.png"):
        target = task_dir / "evidence" / "ui-render" / name
        for role in ("ui-executor", "execute-inline", "testing-executor", "orchestrator",
                     "planning", "audit-ui", "audit-accessibility"):
            decision = decide_write(
                WriteAttempt(role=role, task_dir=task_dir, path=target, operation="create", source="agent")
            )
            assert decision.allowed is False, f"{role} could doctor render evidence ({name})"
        ctl_decision = decide_write(
            WriteAttempt(role="ctl", task_dir=task_dir, path=target, operation="create", source="ctl")
        )
        assert ctl_decision.allowed is True
    assert is_executor_evidence("evidence/seg-1.md") is True
    assert is_executor_evidence("evidence/ui-render/report.json") is False
    assert is_executor_evidence("evidence/verification/seg-1.json") is False


# --- real capture ------------------------------------------------------------


@needs_browser
def test_capture_measures_a_broken_static_page(tmp_path: Path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    root = task_dir.parent.parent
    (root / "out").mkdir()
    (root / "out" / "bad.html").write_text(BAD_PAGE)
    _declare(task_dir, {"targets": [{"id": "bad", "file": "out/bad.html"}]})
    stale = task_dir / "evidence" / "ui-render" / "old-320.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")

    result = _run_ctl("run-ui-render-evidence", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "ui_render_captured"

    report = json.loads((task_dir / "evidence" / "ui-render" / "report.json").read_text())
    assert report["status"] == "captured"
    assert report["widths"] == list(UI_RENDER_WIDTHS)
    (target,) = report["targets"]
    assert target["id"] == "bad" and target["file"] == "out/bad.html" and "url" not in target
    by_width = {vp["width"]: vp for vp in target["viewports"]}
    assert set(by_width) == set(UI_RENDER_WIDTHS)

    # A 900px block overflows the two narrow viewports and fits the wide one.
    assert by_width[320]["horizontal_overflow"]["overflows"] is True
    assert by_width[768]["horizontal_overflow"]["overflows"] is True
    assert by_width[1440]["horizontal_overflow"]["overflows"] is False
    assert any("div.wide" in o["element"] for o in by_width[320]["horizontal_overflow"]["offenders"])

    narrow = by_width[320]
    # Two adjacent 12px buttons crowd each other; the isolated one passes
    # the WCAG 2.5.8 spacing exception and is not reported.
    assert [(i["width"], i["height"]) for i in narrow["small_targets"]] == [(12, 12), (12, 12)]
    assert narrow["small_targets_total"] == 2
    assert narrow["small_targets_passing_by_spacing"] == 1
    assert not any("Alone" in item["element"] for item in narrow["small_targets"])
    # Disabled controls are exempt from the contrast requirement.
    assert not any("Disabled faint" in item["element"] for item in narrow["low_contrast"])
    assert any("button.tiny" in item["element"] for item in narrow["unnamed_controls"])
    assert len(narrow["images_missing_alt"]) == 1
    assert len(narrow["broken_images"]) == 1
    (faint,) = [item for item in narrow["low_contrast"] if "p.faint" in item["element"]]
    assert faint["required"] == 4.5 and 1.8 < faint["ratio"] < 2.0  # #bbb on #fff is 1.92:1
    assert not any("h1" in item["element"] for item in narrow["low_contrast"])

    for vp in target["viewports"]:
        shot = task_dir / vp["screenshot"]
        assert shot.is_file() and shot.read_bytes()[:4] == b"\x89PNG"
    assert not stale.exists(), "a screenshot from an earlier capture survived"
    assert report["totals"]["viewports_with_overflow"] == 2


@needs_browser
def test_one_missing_file_does_not_discard_the_other_targets(tmp_path: Path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    root = task_dir.parent.parent
    (root / "out").mkdir()
    (root / "out" / "good.html").write_text(GOOD_PAGE)
    _declare(task_dir, {"targets": [
        {"id": "good", "file": "out/good.html"},
        {"id": "gone", "file": "out/gone.html"},
    ]})
    result = _run_ctl("run-ui-render-evidence", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((task_dir / "evidence" / "ui-render" / "report.json").read_text())
    assert report["status"] == "captured"
    by_id = {t["id"]: t for t in report["targets"]}
    assert len(by_id["good"]["viewports"]) == 3
    assert by_id["gone"]["viewports"] == [] and "file not found" in by_id["gone"]["load_error"]
    assert report["totals"]["load_errors"] == 1


@needs_browser
def test_capture_starts_the_declared_server_and_stops_it(tmp_path: Path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    root = task_dir.parent.parent
    (root / "site").mkdir()
    (root / "site" / "index.html").write_text(GOOD_PAGE)
    port = _free_port()
    _declare(task_dir, {
        "start_command": f"{sys.executable} -m http.server {port} --bind 127.0.0.1 --directory site",
        "ready_timeout": 60,
        "targets": [{"id": "home", "url": f"http://127.0.0.1:{port}/", "wait_for": "main"}],
    })

    result = _run_ctl("run-ui-render-evidence", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((task_dir / "evidence" / "ui-render" / "report.json").read_text())
    assert report["status"] == "captured"
    assert report["server"] == "started by start_command"
    totals = report["totals"]
    assert totals["viewports"] == 3 and totals["load_errors"] == 0
    assert totals["viewports_with_overflow"] == 0
    assert totals["low_contrast"] == 0 and totals["small_targets"] == 0 and totals["unnamed_controls"] == 0

    # The server ctl started is gone once the capture returns.
    with socket.socket() as sock:
        sock.settimeout(1)
        assert sock.connect_ex(("127.0.0.1", port)) != 0


# --- auditor prompt injection ------------------------------------------------


def _inject(tmp_path: Path, auditor: str, *, auditors: list[dict], report: dict | None) -> str:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20261116-002"
    td.mkdir(parents=True)
    plan_path = td / "audit-plan.json"
    plan_path.write_text(json.dumps({"auditors": auditors}))
    if report is not None:
        out = td / "evidence" / "ui-render"
        out.mkdir(parents=True)
        (out / "report.json").write_text(json.dumps(report))
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "router.py"), "audit-inject-prompt",
         "--root", str(project), "--task-type", "feature", "--audit-plan", str(plan_path),
         "--auditor-name", auditor],
        input="base prompt body", text=True, capture_output=True, check=False, cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


_BOTH = [
    {"name": "ui-auditor", "action": "spawn", "route_mode": "generic", "agent_path": None},
    {"name": "accessibility-auditor", "action": "spawn", "route_mode": "generic", "agent_path": None},
    {"name": "security-auditor", "action": "spawn", "route_mode": "generic", "agent_path": None},
]


def test_auditors_are_pointed_at_a_captured_report(tmp_path: Path) -> None:
    report = {
        "status": "captured", "widths": [320, 768, 1440],
        "targets": [{"id": "settings", "viewports": []}],
        "totals": {"viewports_with_overflow": 2, "low_contrast": 3, "small_targets": 1},
    }
    for auditor in ("ui-auditor", "accessibility-auditor"):
        prompt = _inject(tmp_path / auditor, auditor, auditors=_BOTH, report=report)
        assert "## Rendered Evidence (injected)" in prompt
        assert ".dynos/task-20261116-002/evidence/ui-render/report.json" in prompt
        assert "(settings) at 320, 768, 1440 px" in prompt
        assert "2 viewport(s) with horizontal overflow" in prompt
        assert "3 low-contrast text element(s)" in prompt
        assert "No rendered evidence" not in prompt
        assert "is not running in this audit" not in prompt


@pytest.mark.parametrize("report, expected", [
    (None, "has not been run for this task"),
    ({"status": "skipped", "reason": "node is not installed"}, "capture skipped: node is not installed"),
    ({"status": "failed", "reason": "server exited"}, "capture failed: server exited"),
])
def test_auditors_are_told_when_no_evidence_exists(tmp_path: Path, report: dict | None, expected: str) -> None:
    prompt = _inject(tmp_path, "accessibility-auditor", auditors=_BOTH, report=report)
    assert "No rendered evidence is available" in prompt
    assert expected in prompt
    assert "never record it as passing" in prompt
    assert "evidence/ui-render/report.json" not in prompt


def test_ui_auditor_covers_accessibility_when_its_partner_is_not_spawned(tmp_path: Path) -> None:
    skipped = [dict(entry, action="skip") if entry["name"] == "accessibility-auditor" else entry for entry in _BOTH]
    absent = [entry for entry in _BOTH if entry["name"] != "accessibility-auditor"]
    for case, auditors in (("skipped", skipped), ("absent", absent)):
        prompt = _inject(tmp_path / case, "ui-auditor", auditors=auditors, report=None)
        assert "`accessibility-auditor` is not running in this audit" in prompt


def test_other_auditors_get_no_render_block(tmp_path: Path) -> None:
    prompt = _inject(tmp_path, "security-auditor", auditors=_BOTH, report={"status": "captured", "targets": []})
    assert "Rendered Evidence" not in prompt


def test_a_capture_older_than_the_latest_repair_is_not_presented(tmp_path: Path) -> None:
    import os

    report = {"status": "captured", "widths": [320], "targets": [{"id": "home"}], "totals": {}}
    prompt = _inject(tmp_path / "fresh", "ui-auditor", auditors=_BOTH, report=report)
    assert "evidence/ui-render/report.json" in prompt

    project = tmp_path / "stale" / "project"
    td = project / ".dynos" / "task-20261116-002"
    (td / "evidence" / "ui-render").mkdir(parents=True)
    report_path = td / "evidence" / "ui-render" / "report.json"
    report_path.write_text(json.dumps(report))
    (td / "repair-log.json").write_text(json.dumps({"batches": []}))
    os.utime(report_path, (1_000_000, 1_000_000))
    (td / "audit-plan.json").write_text(json.dumps({"auditors": _BOTH}))
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "router.py"), "audit-inject-prompt",
         "--root", str(project), "--task-type", "feature", "--audit-plan", str(td / "audit-plan.json"),
         "--auditor-name", "accessibility-auditor"],
        input="base", text=True, capture_output=True, check=False, cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "predates the latest repair" in result.stdout
    assert "evidence/ui-render/report.json" not in result.stdout


def test_reaudit_tells_ui_auditor_when_its_partner_is_not_rerun(tmp_path: Path) -> None:
    def run(case: str, a11y_finding_id: str) -> str:
        project = tmp_path / case / "project"
        td = project / ".dynos" / "task-20261116-002"
        (td / "audit-reports").mkdir(parents=True)
        (td / "audit-plan.json").write_text(json.dumps({"auditors": _BOTH}))
        (td / "repair-log.json").write_text(json.dumps(
            {"batches": [{"tasks": [{"finding_id": "ui-003", "merged_findings": [{"id": "a11y-009"}]}]}]}
        ))
        (td / "audit-reports" / "ui-auditor-x-attempt-1.json").write_text(json.dumps(
            {"auditor_name": "ui-auditor", "findings": [{"id": "ui-003"}]}
        ))
        (td / "audit-reports" / "accessibility-auditor-x-attempt-1.json").write_text(json.dumps(
            {"auditor_name": "accessibility-auditor", "findings": [{"id": a11y_finding_id}]}
        ))
        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "router.py"), "audit-inject-prompt",
             "--root", str(project), "--task-type", "feature", "--audit-plan", str(td / "audit-plan.json"),
             "--auditor-name", "ui-auditor"],
            input="base", text=True, capture_output=True, check=False, cwd=str(ROOT),
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    # Its findings were not repaired, so it is not re-run: ui-auditor covers.
    assert "`accessibility-auditor` is not running in this audit" in run("not-rerun", "a11y-001")
    # One of its findings was repaired, so it is re-run: no cover needed.
    assert "is not running in this audit" not in run("rerun", "a11y-009")


def test_malformed_report_does_not_break_prompt_injection(tmp_path: Path) -> None:
    report = {"status": "captured", "widths": 320, "targets": "nope", "totals": []}
    prompt = _inject(tmp_path / "shape", "ui-auditor", auditors=_BOTH, report=report)
    assert "## Rendered Evidence (injected)" in prompt
    long_reason = {"status": "failed", "reason": "boom\n" + "x" * 5000}
    prompt = _inject(tmp_path / "reason", "ui-auditor", auditors=_BOTH, report=long_reason)
    assert "capture failed: boom x" in prompt and "x" * 400 not in prompt
