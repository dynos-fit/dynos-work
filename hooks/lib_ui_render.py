"""Deterministic UI render evidence.

A UI task's plan declares what to render (`ui_render` in the execution
graph). `ctl run-ui-render-evidence` starts the declared dev server (if any),
renders each target at fixed viewport widths through `ui_render_probe.mjs`
and stores screenshots plus measured results under the ctl-owned
`evidence/ui-render/`. The UI auditors read those measurements instead of
estimating contrast, target sizes and narrow-width layout from a diff.

This module holds the parts with no ctl dependency: validating the
declaration and running the capture. It never installs anything; when no
browser driver is available the capture is recorded as skipped.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

UI_RENDER_WIDTHS = (320, 768, 1440)
UI_RENDER_HEIGHT = 900
UI_RENDER_REL_DIR = "evidence/ui-render"
UI_RENDER_REPORT = "report.json"
MAX_TARGETS = 12
DEFAULT_READY_TIMEOUT_S = 120
MAX_READY_TIMEOUT_S = 900
DEFAULT_NAV_TIMEOUT_MS = 30000

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_TAIL_BYTES = 4096

# Measurement keys that hold a list of offending elements, per viewport.
_LIST_KEYS = ("small_targets", "unnamed_controls", "images_missing_alt", "broken_images", "low_contrast", "console_errors")


def _loopback_url(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"ui_render {field} must be a non-empty string")
    url = value.strip()
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError(f"ui_render {field} must be an http(s) URL, got {url!r}")
    if (parts.hostname or "").lower() not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"ui_render {field} must point at this machine (localhost, 127.0.0.1 or [::1]), got {url!r}"
        )
    if parts.username or parts.password:
        raise ValueError(f"ui_render {field} must not carry credentials")
    return url


def _repo_file(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"ui_render {field} must be a non-empty string")
    rel = value.strip()
    if any(ord(ch) < 32 for ch in rel):
        raise ValueError(f"ui_render {field} must not contain control characters")
    path = Path(rel)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"ui_render {field} must be a repo-relative path inside the workspace, got {rel!r}")
    if path.suffix.lower() not in {".html", ".htm"}:
        raise ValueError(f"ui_render {field} must be an .html or .htm file, got {rel!r}")
    if path.parts and path.parts[0] == ".dynos":
        raise ValueError(f"ui_render {field} must not point into .dynos/")
    return path.as_posix()


def normalize_ui_render(raw: object) -> dict | None:
    """Validate a plan's `ui_render` declaration; return its canonical form.

    Returns None when nothing is declared. Raises ValueError on a malformed
    declaration so `write-execution-graph` refuses it at plan time instead of
    the audit discovering it.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("ui_render must be an object")
    raw_targets = raw.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("ui_render.targets must be a non-empty array")
    if len(raw_targets) > MAX_TARGETS:
        raise ValueError(f"ui_render.targets is limited to {MAX_TARGETS} entries")

    targets: list[dict] = []
    seen: set[str] = set()
    for idx, entry in enumerate(raw_targets):
        if not isinstance(entry, dict):
            raise ValueError(f"ui_render.targets[{idx}] must be an object")
        target_id = str(entry.get("id", "")).strip()
        if not _ID_RE.match(target_id):
            raise ValueError(
                f"ui_render.targets[{idx}].id must be lowercase letters, digits and hyphens (max 40), got {target_id!r}"
            )
        if target_id in seen:
            raise ValueError(f"duplicate ui_render target id: {target_id}")
        seen.add(target_id)
        has_url = entry.get("url") not in (None, "")
        has_file = entry.get("file") not in (None, "")
        if has_url == has_file:
            raise ValueError(f"ui_render target {target_id}: give exactly one of 'url' or 'file'")
        target: dict = {"id": target_id}
        if has_url:
            target["url"] = _loopback_url(entry.get("url"), f"target {target_id} url")
        else:
            target["file"] = _repo_file(entry.get("file"), f"target {target_id} file")
        wait_for = entry.get("wait_for")
        if wait_for not in (None, ""):
            if not isinstance(wait_for, str):
                raise ValueError(f"ui_render target {target_id}: wait_for must be a CSS selector string")
            target["wait_for"] = wait_for.strip()
        targets.append(target)

    normalized: dict = {"targets": targets}
    start_command = raw.get("start_command")
    if start_command not in (None, ""):
        if not isinstance(start_command, str):
            raise ValueError("ui_render.start_command must be a string")
        normalized["start_command"] = start_command.strip()
    ready_url = raw.get("ready_url")
    if ready_url not in (None, ""):
        normalized["ready_url"] = _loopback_url(ready_url, "ready_url")
    ready_timeout = raw.get("ready_timeout")
    if ready_timeout is not None:
        try:
            ready_timeout_s = int(ready_timeout)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("ui_render.ready_timeout must be an integer of seconds") from None
        if isinstance(ready_timeout, bool) or not 0 < ready_timeout_s <= MAX_READY_TIMEOUT_S:
            raise ValueError(f"ui_render.ready_timeout must be between 1 and {MAX_READY_TIMEOUT_S} seconds")
        normalized["ready_timeout"] = ready_timeout_s
    if "start_command" in normalized and "ready_url" not in normalized:
        first_url = next((t["url"] for t in targets if "url" in t), None)
        if first_url is None:
            raise ValueError("ui_render.start_command needs a ready_url or at least one url target to wait for")
        normalized["ready_url"] = first_url
    return normalized


def _tail(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-_TAIL_BYTES:].decode("utf-8", errors="replace")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


# Straight to the loopback address: no proxy from the environment (a proxy
# answering 502 would make a dead port look alive) and no redirects.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _url_ready(url: str) -> bool:
    """True once the server answers at all; an HTTP error status still means it is up."""
    try:
        with _OPENER.open(url, timeout=3):  # noqa: S310 - loopback only, validated at plan time
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _stop(proc: subprocess.Popen) -> None:
    """Stop the server and everything it started.

    The whole process group is signalled even when the shell itself has
    already exited: a start_command that backgrounds its server leaves the
    server running in the group after bash returns.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            if proc.poll() is None:
                proc.terminate() if sig == signal.SIGTERM else proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            continue
        # The shell is gone; give the rest of the group a moment to follow.
        for _ in range(20):
            try:
                os.killpg(proc.pid, 0)
            except (ProcessLookupError, PermissionError, OSError):
                return
            time.sleep(0.1)


class _Terminated(Exception):
    """Raised from the SIGTERM handler so `finally` blocks still run."""


def _raise_terminated(signum, frame):  # noqa: ANN001, ARG001
    raise _Terminated()


def summarize(targets: list[dict]) -> dict:
    """Totals across every target and viewport, for the report header."""
    totals = {key: 0 for key in _LIST_KEYS}
    totals.update({"viewports": 0, "viewports_with_overflow": 0, "load_errors": 0, "contrast_unmeasured": 0, "targets_under_44": 0, "blocked_requests": 0})
    for target in targets:
        for vp in target.get("viewports", []) or []:
            totals["viewports"] += 1
            if vp.get("load_error"):
                totals["load_errors"] += 1
            if (vp.get("horizontal_overflow") or {}).get("overflows"):
                totals["viewports_with_overflow"] += 1
            totals["contrast_unmeasured"] += int(vp.get("contrast_unmeasured") or 0)
            totals["targets_under_44"] += int(vp.get("targets_under_44") or 0)
            totals["blocked_requests"] += len(vp.get("blocked_requests") or [])
            for key in _LIST_KEYS:
                # Lists are capped per viewport; `<key>_total` has the real count.
                exact = vp.get(f"{key}_total")
                totals[key] += exact if isinstance(exact, int) and not isinstance(exact, bool) else len(vp.get(key) or [])
    return totals


def capture(root: Path, out_dir: Path, declaration: dict | None, *, probe_timeout_s: int = 600) -> dict:
    """Run the capture and return the report body (without timestamps).

    `status` is `captured`, `skipped` (nothing declared, or no node / browser
    driver here) or `failed` (declared, but the server or a target could not
    be brought up). Screenshots land in `out_dir`; the caller writes the
    report itself through the write boundary.
    """
    base = {"schema": 1, "widths": list(UI_RENDER_WIDTHS), "height": UI_RENDER_HEIGHT, "targets": []}
    if not declaration:
        return {**base, "status": "skipped", "reason": "the execution graph declares no ui_render targets"}

    node = shutil.which("node")
    if node is None:
        return {**base, "status": "skipped", "reason": "node is not installed, so the render probe cannot run"}

    # A target whose file is missing is recorded on that target; the rest
    # of the capture still runs.
    spec_targets: list[dict] = []
    unloadable: dict[str, str] = {}
    for target in declaration.get("targets", []):
        entry = {"id": target["id"]}
        if "url" in target:
            entry["url"] = target["url"]
        else:
            try:
                file_path = (root / target["file"]).resolve()
                file_path.relative_to(root.resolve())
            except (ValueError, OSError):
                unloadable[target["id"]] = "file escapes the workspace"
                continue
            if not file_path.is_file():
                unloadable[target["id"]] = f"file not found: {target['file']}"
                continue
            entry["url"] = file_path.as_uri()
        if target.get("wait_for"):
            entry["wait_for"] = target["wait_for"]
        spec_targets.append(entry)

    def _unloadable_targets() -> list[dict]:
        by_id = {t["id"]: t for t in declaration.get("targets", [])}
        return [
            {"id": tid, "file": by_id[tid].get("file"), "load_error": why, "viewports": []}
            for tid, why in unloadable.items()
        ]

    if not spec_targets:
        return {
            **base, "status": "failed", "targets": _unloadable_targets(),
            "reason": "no target could be loaded: " + "; ".join(f"{tid}: {why}" for tid, why in unloadable.items()),
        }

    out_dir.mkdir(parents=True, exist_ok=True)

    # ctl being terminated must not leave the server (its own session) or
    # the temp dir behind.
    previous_handler = None
    try:
        previous_handler = signal.signal(signal.SIGTERM, _raise_terminated)
    except ValueError:  # not the main thread
        previous_handler = None
    server: subprocess.Popen | None = None
    server_log: Path | None = None
    server_reused = False
    start_command = declaration.get("start_command")
    with tempfile.TemporaryDirectory(prefix="dynos-ui-render-") as tmp:
        tmp_dir = Path(tmp)
        try:
            if start_command:
                ready_url = declaration["ready_url"]
                # A server the developer already has running serves the same
                # working tree; reuse it rather than fight over the port.
                server_reused = _url_ready(ready_url)
            if start_command and not server_reused:
                server_log = tmp_dir / "server.log"
                with server_log.open("wb") as log:
                    server = subprocess.Popen(  # noqa: S603 - command comes from the human-approved plan
                        ["bash", "-c", start_command], cwd=str(root), stdout=log, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL, start_new_session=True,
                    )
                deadline = time.monotonic() + int(declaration.get("ready_timeout") or DEFAULT_READY_TIMEOUT_S)
                while not _url_ready(ready_url):
                    # Exit code 0 can be a command that put its server in the
                    # background; keep waiting for the URL in that case.
                    if server.poll() not in (None, 0):
                        return {
                            **base, "status": "failed",
                            "reason": f"start_command exited with code {server.returncode} before {ready_url} answered",
                            "server_output_tail": _tail(server_log),
                        }
                    if time.monotonic() > deadline:
                        return {
                            **base, "status": "failed",
                            "reason": f"{ready_url} did not answer within the ready timeout",
                            "server_output_tail": _tail(server_log),
                        }
                    time.sleep(0.5)

            spec_path = tmp_dir / "spec.json"
            spec_path.write_text(json.dumps({
                "targets": spec_targets, "widths": list(UI_RENDER_WIDTHS), "height": UI_RENDER_HEIGHT,
                "root": str(root.resolve()),
                "nav_timeout_ms": DEFAULT_NAV_TIMEOUT_MS,
            }))
            probe = Path(__file__).resolve().parent / "ui_render_probe.mjs"
            try:
                proc = subprocess.run(  # noqa: S603
                    [node, str(probe), "--spec", str(spec_path), "--out", str(out_dir)],
                    cwd=str(root), capture_output=True, text=True, timeout=probe_timeout_s, check=False,
                )
            except subprocess.TimeoutExpired:
                return {**base, "status": "failed", "reason": f"render probe exceeded {probe_timeout_s}s"}
            try:
                result = json.loads(proc.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                return {
                    **base, "status": "failed",
                    "reason": f"render probe produced no result (exit {proc.returncode})",
                    "probe_output_tail": (proc.stderr or proc.stdout)[-_TAIL_BYTES:],
                }
        except _Terminated:
            return {**base, "status": "failed", "reason": "the capture was terminated before it finished"}
        finally:
            if server is not None:
                _stop(server)
            if previous_handler is not None:
                signal.signal(signal.SIGTERM, previous_handler)

    status = result.get("status")
    if status != "captured":
        return {**base, "status": status if status in {"skipped", "failed"} else "failed", "reason": str(result.get("reason", "render probe failed"))}

    targets = result.get("targets", [])
    declared_files = {t["id"]: t["file"] for t in declaration.get("targets", []) if "file" in t}
    for target in targets:
        if target.get("id") in declared_files:
            target["file"] = declared_files[target["id"]]
            target.pop("url", None)
        for vp in target.get("viewports", []) or []:
            if vp.get("screenshot"):
                vp["screenshot"] = f"{UI_RENDER_REL_DIR}/{vp['screenshot']}"
    targets.extend(_unloadable_targets())
    totals = summarize(targets)
    totals["load_errors"] += len(unloadable)
    report = {**base, "status": "captured", "driver": result.get("driver"), "totals": totals, "targets": targets}
    if start_command:
        report["server"] = "reused a server already answering at ready_url" if server_reused else "started by start_command"
    if totals["viewports"] and totals["load_errors"] == totals["viewports"]:
        report["status"] = "failed"
        report["reason"] = "no target could be loaded; see each viewport's load_error"
    return report
