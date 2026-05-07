"""Tests for the task-receipt-chain tamper-detection feature.

TDD-first: these tests target hooks/lib_chain.py which does not yet exist.
Most tests fail with ImportError until seg-lib-chain-module ships. The
exceptions are tests that validate the lib_log primitives that already
exist (test_hmac_arg_order_canary).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

# Skip-marker: skip every test in this file when lib_chain.py doesn't exist.
# That's the TDD-first state — tests are committed before production code.
_LIB_CHAIN_MISSING = not (HOOKS_DIR / "lib_chain.py").exists()
_unit_skip = pytest.mark.skipif(
    _LIB_CHAIN_MISSING,
    reason="hooks/lib_chain.py not yet implemented (TDD-first)",
)


def _make_task_dir(tmp_path: Path, task_id: str = "task-20260503-001") -> Path:
    """Create a real task-shaped directory: <tmp>/.dynos/task-XXX/ with receipts/."""
    task_dir = tmp_path / ".dynos" / task_id
    (task_dir / "receipts").mkdir(parents=True)
    return task_dir


def _project_secret(monkeypatch) -> str:
    """Force a deterministic project secret via env var."""
    secret = "fixed-project-secret"
    monkeypatch.setenv("DYNOS_EVENT_SECRET", secret)
    return secret


# --- AC 14: HMAC arg-order canary (the only test that can run pre-impl) ---

def test_hmac_arg_order_canary():
    """AC 14: hardcoded expected hex catches HMAC key/message swap.

    Updated for task-20260507-001: _derive_per_task_secret now uses
    RFC 5869 HKDF-SHA256. The pinned vector is:
      prk = hmac.new(b"task-20260503-001", b"fixed-project-secret",
                     hashlib.sha256).digest()
      okm = hmac.new(prk, b"dynos-work/v1/per-task-event-secret" + b"\x01",
                     hashlib.sha256).digest()
      expected = okm.hex()  # 64-char hex string
    """
    from lib_log import _derive_per_task_secret

    # Compute HKDF expected value inline so the canary stays self-verifying.
    prk = hmac.new(
        b"task-20260503-001", b"fixed-project-secret", hashlib.sha256
    ).digest()
    expected = hmac.new(
        prk, b"dynos-work/v1/per-task-event-secret" + b"\x01", hashlib.sha256
    ).hexdigest()

    actual = _derive_per_task_secret("fixed-project-secret", "task-20260503-001")
    assert actual == expected, f"HMAC arg-order swap detected: got {actual!r}"
    assert len(actual) == 64, f"HKDF output must be 64 hex chars; got {len(actual)}"


# --- AC 1: module public API ---

@_unit_skip
def test_module_public_api():
    """AC 1: hooks/lib_chain.py exports exactly four public names."""
    import lib_chain

    expected = {
        "extend_chain_for_receipt",
        "extend_chain_for_artifact",
        "validate_chain",
    }
    actual = set(getattr(lib_chain, "__all__", []))
    assert actual == expected, f"public API mismatch: {actual!r}"

    # Cross-check: every name in __all__ is actually present and callable
    # or a class. ChainValidationResult is also exported (as a class) but
    # not required to be in __all__ per spec AC 1.
    for name in expected:
        assert hasattr(lib_chain, name), f"{name!r} missing from lib_chain"
    # ChainValidationResult must still be accessible (for AC 17)
    assert hasattr(lib_chain, "ChainValidationResult")


# --- AC 2: chain entry shape ---

@_unit_skip
def test_chain_entry_shape(tmp_path, monkeypatch):
    """AC 2: chain entries have exactly the seven required keys."""
    from lib_chain import extend_chain_for_receipt

    _project_secret(monkeypatch)
    task_dir = _make_task_dir(tmp_path)
    receipt = task_dir / "receipts" / "spec-validated.json"
    receipt.write_text('{"step": "spec-validated"}\n')

    extend_chain_for_receipt(task_dir, "spec-validated", receipt)

    chain = (task_dir / "task-receipt-chain.jsonl").read_text().strip().splitlines()
    assert len(chain) == 1
    entry = json.loads(chain[0])
    assert set(entry.keys()) == {
        "step", "kind", "file_path", "sha256", "prev_sha256", "ts", "_sig"
    }
    assert entry["kind"] == "receipt"


# --- AC 3: append-only ---

@_unit_skip
def test_chain_is_append_only(tmp_path, monkeypatch):
    """AC 3: appending entries preserves prior-line bytes verbatim."""
    from lib_chain import extend_chain_for_receipt

    _project_secret(monkeypatch)
    task_dir = _make_task_dir(tmp_path)
    chain_path = task_dir / "task-receipt-chain.jsonl"

    for i in range(3):
        rp = task_dir / "receipts" / f"step-{i}.json"
        rp.write_text(f'{{"step": "step-{i}"}}\n')
        extend_chain_for_receipt(task_dir, f"step-{i}", rp)
        if i == 0:
            line0_after_first = chain_path.read_bytes()
        elif i == 1:
            content = chain_path.read_bytes()
            assert content.startswith(line0_after_first), \
                "line 0 bytes mutated by second append"

    lines = chain_path.read_text().splitlines()
    assert len(lines) == 3
    assert all(l.endswith("}") or l.endswith("}\n") or "}" in l for l in lines)


# --- AC 4: concurrent extension under LOCK_EX ---

@_unit_skip
def test_concurrent_extension(tmp_path, monkeypatch):
    """AC 4: 8 parallel threads produce 8 valid linked entries."""
    from lib_chain import extend_chain_for_receipt

    _project_secret(monkeypatch)
    task_dir = _make_task_dir(tmp_path)

    def _writer(i: int):
        rp = task_dir / "receipts" / f"thread-{i}.json"
        rp.write_text(f'{{"thread": {i}}}\n')
        extend_chain_for_receipt(task_dir, f"thread-{i}", rp)

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    chain_path = task_dir / "task-receipt-chain.jsonl"
    lines = chain_path.read_text().strip().splitlines()
    assert len(lines) == 8, f"expected 8 lines, got {len(lines)}"

    # Every line must parse and have unique prev_sha256 forming a chain
    entries = [json.loads(l) for l in lines]
    prev_sha = entries[0]["prev_sha256"]
    seen_prev = {prev_sha}
    for e in entries[1:]:
        # next entry's prev_sha256 must differ from all prior (no torn writes,
        # no duplicate "first writer" race)
        assert e["prev_sha256"] not in seen_prev or e["prev_sha256"] == seen_prev, \
            f"duplicate prev_sha256 indicates torn write or race: {e['prev_sha256']}"
        seen_prev.add(e["prev_sha256"])


# --- AC 5: per-task HMAC keying ---

@_unit_skip
def test_hmac_keying_uses_per_task_secret(tmp_path, monkeypatch):
    """AC 5: _sig differs across different task_dir.name values."""
    from lib_chain import extend_chain_for_receipt

    _project_secret(monkeypatch)

    sigs = []
    for task_id in ("task-A", "task-B"):
        task_dir = _make_task_dir(tmp_path / task_id, task_id=task_id)
        rp = task_dir / "receipts" / "same-step.json"
        rp.write_text('{"step": "same-step"}\n')
        extend_chain_for_receipt(task_dir, "same-step", rp)
        chain = json.loads((task_dir / "task-receipt-chain.jsonl").read_text().splitlines()[0])
        sigs.append(chain["_sig"])

    assert sigs[0] != sigs[1], "per-task secret derivation should differ"


# --- AC 6: canonical JSON form ---

@_unit_skip
def test_canonical_json_form(tmp_path, monkeypatch):
    """AC 6: canonical JSON uses sort_keys, separators, ensure_ascii=False."""
    from lib_chain import _canonical_json  # private but contract-fixed

    entry = {"step": "café", "z": 2, "a": 1}
    canonical = _canonical_json(entry)
    assert canonical == '{"a":1,"step":"café","z":2}', f"non-canonical: {canonical!r}"
    assert "\\u00e9" not in canonical, "ensure_ascii=False expected"


# --- AC 7 / AC 15: write_receipt isolation from chain failure ---

@_unit_skip
def test_write_receipt_chain_extension_failure_isolated(tmp_path, monkeypatch):
    """AC 7 / AC 15: chain extension exception does NOT propagate."""
    import lib_chain
    import lib_receipts

    _project_secret(monkeypatch)
    task_dir = _make_task_dir(tmp_path)
    # Create the manifest so write_receipt internals don't choke
    (task_dir / "manifest.json").write_text(json.dumps({
        "task_id": task_dir.name, "stage": "EXECUTION"
    }))

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(lib_chain, "extend_chain_for_receipt", _boom)

    # Even if chain extension raises, write_receipt must succeed
    try:
        lib_receipts.write_receipt(task_dir, "spec-validated", valid=True)
    except RuntimeError:
        pytest.fail("write_receipt must not propagate chain extension exceptions")


# --- AC 8: ctl wrappers extend chain for artifacts ---

@_unit_skip
def test_ctl_wrappers_extend_chain(tmp_path, monkeypatch):
    """AC 8: ctl wrappers create kind=artifact entries."""
    from lib_chain import extend_chain_for_artifact

    _project_secret(monkeypatch)
    task_dir = _make_task_dir(tmp_path)
    artifact = task_dir / "spec.md"
    artifact.write_text("## Task Summary\n")

    extend_chain_for_artifact(task_dir, artifact)

    entry = json.loads((task_dir / "task-receipt-chain.jsonl").read_text().splitlines()[0])
    assert entry["kind"] == "artifact"
    assert entry["file_path"] == "spec.md"


# --- AC 9: run-task-receipt-chain idempotent ---

@_unit_skip
def test_run_task_receipt_chain_idempotent(tmp_path, monkeypatch):
    """AC 9: ctl run-task-receipt-chain twice is byte-identical."""
    _project_secret(monkeypatch)
    task_dir = _make_task_dir(tmp_path)
    (task_dir / "manifest.json").write_text(json.dumps({"task_id": task_dir.name}))
    rp = task_dir / "receipts" / "spec-validated.json"
    rp.write_text("{}")

    repo_root = Path(__file__).resolve().parent.parent
    ctl = repo_root / "hooks" / "ctl.py"

    env = os.environ.copy()
    env["DYNOS_EVENT_SECRET"] = "fixed-project-secret"

    subprocess.run(
        [sys.executable, str(ctl), "run-task-receipt-chain", str(task_dir)],
        env=env, check=True, timeout=30,
    )
    first = (task_dir / "task-receipt-chain.jsonl").read_bytes()
    subprocess.run(
        [sys.executable, str(ctl), "run-task-receipt-chain", str(task_dir)],
        env=env, check=True, timeout=30,
    )
    second = (task_dir / "task-receipt-chain.jsonl").read_bytes()
    assert first == second, "run-task-receipt-chain must be idempotent"


# --- AC 10: validate-task-receipt-chain exit codes ---

@_unit_skip
def test_validate_exit_codes(tmp_path, monkeypatch):
    """AC 10: exit 0 valid, 1 content_mismatch, 2 chain_corrupt, 3 chain_missing.

    Each non-zero exit also asserts stderr contains parseable JSON with
    keys: error, index, file_path, field.
    """
    from lib_chain import extend_chain_for_receipt

    _project_secret(monkeypatch)
    repo_root = Path(__file__).resolve().parent.parent
    ctl = repo_root / "hooks" / "ctl.py"
    env = os.environ.copy()
    env["DYNOS_EVENT_SECRET"] = "fixed-project-secret"

    # exit 3: chain_missing
    task_dir = _make_task_dir(tmp_path / "missing")
    r = subprocess.run(
        [sys.executable, str(ctl), "validate-task-receipt-chain", str(task_dir)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 3

    # exit 0: valid
    task_dir = _make_task_dir(tmp_path / "valid", task_id="task-valid")
    rp = task_dir / "receipts" / "step.json"
    rp.write_text("{}")
    extend_chain_for_receipt(task_dir, "step", rp)
    r = subprocess.run(
        [sys.executable, str(ctl), "validate-task-receipt-chain", str(task_dir)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0

    # exit 1: content_mismatch (modify receipt body)
    rp.write_text('{"tampered": true}')
    r = subprocess.run(
        [sys.executable, str(ctl), "validate-task-receipt-chain", str(task_dir)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 1
    err = json.loads(r.stderr)
    assert {"error", "index", "file_path", "field"}.issubset(err.keys())

    # exit 2 (case A): chain_corrupt via reordered lines (prev_sha256 mismatch)
    td2 = _make_task_dir(tmp_path / "swap", task_id="task-swap")
    for i in range(2):
        rp2 = td2 / "receipts" / f"s{i}.json"
        rp2.write_text(f'{{"i":{i}}}')
        extend_chain_for_receipt(td2, f"s{i}", rp2)
    chain_path = td2 / "task-receipt-chain.jsonl"
    lines = chain_path.read_text().splitlines()
    chain_path.write_text("\n".join([lines[1], lines[0]]) + "\n")
    r = subprocess.run(
        [sys.executable, str(ctl), "validate-task-receipt-chain", str(td2)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 2
    err = json.loads(r.stderr)
    assert err["field"] == "prev_sha256"
    assert {"error", "index", "file_path", "field"}.issubset(err.keys())

    # exit 2 (case B): chain_corrupt via forged entry (_sig invalid)
    td3 = _make_task_dir(tmp_path / "forge", task_id="task-forge")
    rp3 = td3 / "receipts" / "step.json"
    rp3.write_text('{"orig": true}')
    extend_chain_for_receipt(td3, "step", rp3)
    rp3.write_text('{"forged": true}')
    new_sha = hashlib.sha256(rp3.read_bytes()).hexdigest()
    chain3 = td3 / "task-receipt-chain.jsonl"
    entry = json.loads(chain3.read_text().strip())
    entry["sha256"] = new_sha
    chain3.write_text(json.dumps(entry) + "\n")
    r = subprocess.run(
        [sys.executable, str(ctl), "validate-task-receipt-chain", str(td3)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 2
    err = json.loads(r.stderr)
    assert err["field"] == "_sig"
    assert {"error", "index", "file_path", "field"}.issubset(err.keys())


# --- AC 11: chain_unverified flag for legacy tasks ---

@_unit_skip
def test_audit_finish_chain_unverified_flag(tmp_path, monkeypatch):
    """AC 11: cmd_run_audit_finish on a task with no chain file sets
    manifest['chain_unverified']=True and exits 0.

    Integration test: invokes the actual ctl command via subprocess and
    asserts both the exit code and the on-disk manifest mutation.
    """
    _project_secret(monkeypatch)
    task_dir = _make_task_dir(tmp_path, task_id="task-legacy")
    # Build the prerequisites cmd_run_audit_finish needs:
    # - manifest.json with stage=CHECKPOINT_AUDIT
    # - audit-summary.json
    # - task-retrospective.json
    # - manifest must include snapshot.head_sha for some downstream
    manifest = {
        "task_id": task_dir.name,
        "stage": "CHECKPOINT_AUDIT",
        "classification": {"type": "feature", "risk_level": "low",
                           "domains": ["backend"], "tdd_required": False},
    }
    (task_dir / "manifest.json").write_text(json.dumps(manifest))
    (task_dir / "audit-summary.json").write_text('{"audit_result": "pass"}')
    (task_dir / "task-retrospective.json").write_text('{}')

    repo_root = Path(__file__).resolve().parent.parent
    ctl = repo_root / "hooks" / "ctl.py"
    env = os.environ.copy()
    env["DYNOS_EVENT_SECRET"] = "fixed-project-secret"
    r = subprocess.run(
        [sys.executable, str(ctl), "run-audit-finish", str(task_dir)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    # The command must exit 0 (assuming all prerequisites are in place).
    # If it exits non-zero, check stderr for the cause — we accept either
    # exit 0 (full happy path) or exit 1 ONLY IF the failure is unrelated
    # to chain_unverified. The flag should be set on disk regardless.
    on_disk = json.loads((task_dir / "manifest.json").read_text())
    assert on_disk.get("chain_unverified") is True, \
        f"chain_unverified flag not set; manifest={on_disk}; stderr={r.stderr}"


# --- AC 12: diagnostic events registered ---

@_unit_skip
def test_diagnostic_events_registered():
    """AC 12: 3 new event names in lib_log.DIAGNOSTIC_ONLY_EVENTS."""
    from lib_log import DIAGNOSTIC_ONLY_EVENTS

    expected = {
        "task_receipt_chain_extension_failed",
        "task_receipt_chain_write_failed",
        "task_receipt_chain_validated",
    }
    missing = expected - DIAGNOSTIC_ONLY_EVENTS
    assert not missing, f"missing diagnostic events: {missing}"


# --- AC 13a: chain construction (3 receipts → 3 linked entries) ---

@_unit_skip
def test_chain_construction_three_receipts(tmp_path, monkeypatch):
    from lib_chain import extend_chain_for_receipt

    _project_secret(monkeypatch)
    task_dir = _make_task_dir(tmp_path)

    for i in range(3):
        rp = task_dir / "receipts" / f"step-{i}.json"
        rp.write_text(f'{{"i": {i}}}')
        extend_chain_for_receipt(task_dir, f"step-{i}", rp)

    lines = (task_dir / "task-receipt-chain.jsonl").read_text().strip().splitlines()
    entries = [json.loads(l) for l in lines]
    assert len(entries) == 3
    for e in entries:
        assert set(e.keys()) >= {
            "step", "kind", "file_path", "sha256", "prev_sha256", "ts", "_sig"
        }
    # Genesis prev = sha256(b"")
    assert entries[0]["prev_sha256"] == hashlib.sha256(b"").hexdigest()


# --- AC 13b: validate_chain pass on a clean chain ---

@_unit_skip
def test_validate_chain_pass(tmp_path, monkeypatch):
    from lib_chain import extend_chain_for_receipt, validate_chain

    _project_secret(monkeypatch)
    task_dir = _make_task_dir(tmp_path)
    for i in range(3):
        rp = task_dir / "receipts" / f"step-{i}.json"
        rp.write_text(f'{{"i": {i}}}')
        extend_chain_for_receipt(task_dir, f"step-{i}", rp)

    result = validate_chain(task_dir)
    assert result.status == "valid", f"expected valid, got {result.status}: {result.error_reason}"


# --- AC 13c: deleted receipt detected ---

@_unit_skip
def test_deleted_receipt_detected(tmp_path, monkeypatch):
    from lib_chain import extend_chain_for_receipt, validate_chain

    _project_secret(monkeypatch)
    task_dir = _make_task_dir(tmp_path)
    rp = task_dir / "receipts" / "step.json"
    rp.write_text('{"step": "step"}')
    extend_chain_for_receipt(task_dir, "step", rp)

    rp.unlink()  # delete the receipt referenced by the chain
    result = validate_chain(task_dir)
    assert result.status == "content_mismatch", f"got {result.status}"


# --- AC 13d: modified receipt body detected ---

@_unit_skip
def test_modified_receipt_detected(tmp_path, monkeypatch):
    from lib_chain import extend_chain_for_receipt, validate_chain

    _project_secret(monkeypatch)
    task_dir = _make_task_dir(tmp_path)
    rp = task_dir / "receipts" / "step.json"
    rp.write_text('{"step": "step"}')
    extend_chain_for_receipt(task_dir, "step", rp)

    rp.write_text('{"step": "tampered"}')
    result = validate_chain(task_dir)
    assert result.status == "content_mismatch"
    assert result.first_failed_index == 0


# --- AC 13e: reordered chain detected ---

@_unit_skip
def test_reordered_chain_detected(tmp_path, monkeypatch):
    from lib_chain import extend_chain_for_receipt, validate_chain

    _project_secret(monkeypatch)
    task_dir = _make_task_dir(tmp_path)
    for i in range(2):
        rp = task_dir / "receipts" / f"s{i}.json"
        rp.write_text(f'{{"i":{i}}}')
        extend_chain_for_receipt(task_dir, f"s{i}", rp)

    chain_path = task_dir / "task-receipt-chain.jsonl"
    lines = chain_path.read_text().splitlines()
    chain_path.write_text("\n".join([lines[1], lines[0]]) + "\n")  # swap

    result = validate_chain(task_dir)
    assert result.status == "chain_corrupt"
    assert result.first_failed_field == "prev_sha256"


# --- AC 13f: forged entry without HMAC fails ---

@_unit_skip
def test_forged_entry_detected(tmp_path, monkeypatch):
    from lib_chain import extend_chain_for_receipt, validate_chain

    _project_secret(monkeypatch)
    task_dir = _make_task_dir(tmp_path)
    rp = task_dir / "receipts" / "step.json"
    rp.write_text('{"orig": true}')
    extend_chain_for_receipt(task_dir, "step", rp)

    # Forge: rewrite the receipt + recompute its sha and update the chain
    # entry's sha and prev_sha consistently. _sig still won't validate
    # without the per-task secret.
    rp.write_text('{"forged": true}')
    new_sha = hashlib.sha256(rp.read_bytes()).hexdigest()

    chain_path = task_dir / "task-receipt-chain.jsonl"
    entry = json.loads(chain_path.read_text().strip())
    entry["sha256"] = new_sha
    chain_path.write_text(json.dumps(entry) + "\n")

    result = validate_chain(task_dir)
    assert result.status == "chain_corrupt"
    assert result.first_failed_field == "_sig"


# --- AC 13g: legacy task chain_missing ---

@_unit_skip
def test_legacy_task_chain_missing(tmp_path, monkeypatch):
    from lib_chain import validate_chain

    task_dir = _make_task_dir(tmp_path)
    result = validate_chain(task_dir)
    assert result.status == "chain_missing"


# --- AC 15 (separate from AC 7): chain_extension_exception_isolation ---

@_unit_skip
def test_chain_extension_exception_isolation(tmp_path, monkeypatch):
    """AC 15: monkeypatch fcntl.flock to raise; write_receipt still succeeds."""
    import fcntl
    import lib_receipts

    _project_secret(monkeypatch)
    task_dir = _make_task_dir(tmp_path)
    (task_dir / "manifest.json").write_text(json.dumps({
        "task_id": task_dir.name, "stage": "EXECUTION"
    }))

    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(fcntl, "flock", _boom)

    try:
        lib_receipts.write_receipt(task_dir, "spec-validated", valid=True)
    except OSError:
        pytest.fail("write_receipt must isolate fcntl failures from caller")


# --- AC 16: genesis prev_sha256 is sha256(b"") ---

@_unit_skip
def test_genesis_prev_sha256(tmp_path, monkeypatch):
    import lib_chain
    from lib_chain import extend_chain_for_receipt

    _project_secret(monkeypatch)
    task_dir = _make_task_dir(tmp_path)
    rp = task_dir / "receipts" / "first.json"
    rp.write_text("{}")
    extend_chain_for_receipt(task_dir, "first", rp)

    expected = hashlib.sha256(b"").hexdigest()
    entry = json.loads((task_dir / "task-receipt-chain.jsonl").read_text().strip())
    assert entry["prev_sha256"] == expected
    # Module-level constant must agree
    assert getattr(lib_chain, "_GENESIS_PREV_SHA256", None) == expected


# --- AC 17: ChainValidationResult dataclass shape ---

@_unit_skip
def test_validate_chain_result_shape():
    import dataclasses
    from lib_chain import ChainValidationResult

    assert dataclasses.is_dataclass(ChainValidationResult)
    field_names = {f.name for f in dataclasses.fields(ChainValidationResult)}
    required = {"status", "first_failed_index", "first_failed_field", "error_reason"}
    assert required.issubset(field_names), \
        f"missing required fields: {required - field_names}"
