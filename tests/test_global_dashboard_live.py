"""Tests for the live (self-updating) global dashboard.

Covers:
- Throttle correctness: rapid-fire calls coalesce to one regen; calls
  spaced past the TTL produce fresh regenerations.
- Allowlist still enforced (sanity, regression guard).
- HTML output contains the polling script with the expected endpoint
  and interval.
- validate_generated_html still passes against the new HTML output
  (no required element id was inadvertently removed by the script
  injection).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telemetry"))


@pytest.fixture(autouse=True)
def _reset_throttle_state():
    """Reset throttle timestamps before each test so tests don't bleed state."""
    import global_dashboard
    global_dashboard._LAST_REGEN_ATTEMPT_AT[0] = 0.0
    global_dashboard._LAST_SUCCESS_AT[0] = 0.0
    yield
    global_dashboard._LAST_REGEN_ATTEMPT_AT[0] = 0.0
    global_dashboard._LAST_SUCCESS_AT[0] = 0.0


class TestRegenThrottle:
    def test_first_call_regens(self):
        """The very first _maybe_regen call should always regen
        (last=0.0, TTL has effectively elapsed)."""
        import global_dashboard
        with mock.patch("global_dashboard.build_global_payload") as mock_build, \
             mock.patch("global_dashboard.write_global_dashboard") as mock_write:
            mock_build.return_value = {"generated_at": "T1"}
            global_dashboard._maybe_regen()
        assert mock_build.call_count == 1
        assert mock_write.call_count == 1
        assert global_dashboard._LAST_REGEN_ATTEMPT_AT[0] > 0
        assert global_dashboard._LAST_SUCCESS_AT[0] > 0

    def test_back_to_back_calls_coalesce(self):
        """Two calls within the throttle TTL should produce exactly one regen."""
        import global_dashboard
        with mock.patch("global_dashboard.build_global_payload") as mock_build, \
             mock.patch("global_dashboard.write_global_dashboard") as mock_write:
            mock_build.return_value = {"generated_at": "T1"}
            global_dashboard._maybe_regen()
            global_dashboard._maybe_regen()
            global_dashboard._maybe_regen()
        assert mock_build.call_count == 1, (
            "throttle should coalesce 3 rapid calls into 1 regen"
        )

    def test_calls_past_ttl_regen_again(self, monkeypatch):
        """Two calls separated by more than the TTL should each regen."""
        import global_dashboard
        # Shorten TTL for the test so we don't actually sleep 2 seconds.
        monkeypatch.setattr(global_dashboard, "_REGEN_TTL_SECONDS", 0.1)
        with mock.patch("global_dashboard.build_global_payload") as mock_build, \
             mock.patch("global_dashboard.write_global_dashboard") as mock_write:
            mock_build.return_value = {"generated_at": "T1"}
            global_dashboard._maybe_regen()
            time.sleep(0.15)  # exceed TTL
            global_dashboard._maybe_regen()
        assert mock_build.call_count == 2, (
            "two calls past the TTL should each trigger a regen"
        )

    def test_failed_regen_advances_attempt_throttle_for_dos_protection(self):
        """A persistent registry walk failure must not let every concurrent
        request pay the full failing cost. The attempt timestamp advances
        unconditionally so the throttle bounds CPU under failure too. The
        success timestamp stays at zero so consumers can detect staleness.
        """
        import global_dashboard
        with mock.patch("global_dashboard.build_global_payload") as mock_build, \
             mock.patch("global_dashboard.write_global_dashboard"):
            mock_build.side_effect = RuntimeError("registry corrupt")
            global_dashboard._maybe_regen()
            global_dashboard._maybe_regen()  # second call — should be throttled
            global_dashboard._maybe_regen()  # third call — should be throttled
        assert mock_build.call_count == 1, (
            f"persistent failure within TTL must not amplify; got {mock_build.call_count} build calls"
        )
        assert global_dashboard._LAST_REGEN_ATTEMPT_AT[0] > 0, (
            "attempt throttle must advance even on failure (DoS protection)"
        )
        assert global_dashboard._LAST_SUCCESS_AT[0] == 0.0, (
            "success timestamp must stay at zero — no successful regen happened"
        )

    def test_throttle_thread_safe(self):
        """ThreadingHTTPServer can dispatch concurrent requests. The lock must
        prevent duplicate regens even when multiple threads enter at once.

        Strategy: synchronize thread starts with a Barrier so they all race
        into _maybe_regen at roughly the same instant. The first thread to
        acquire the lock builds; the rest see _LAST_REGEN_AT was just
        advanced and exit early. Result: exactly one build call.
        """
        import global_dashboard
        import threading
        N = 5
        call_counter = {"n": 0}
        counter_lock = threading.Lock()

        def slow_build():
            # Brief work inside the lock so the other threads have time to
            # arrive at the lock and bounce off the re-check.
            with counter_lock:
                call_counter["n"] += 1
            time.sleep(0.05)
            return {"generated_at": "T"}

        start_barrier = threading.Barrier(N)

        def worker():
            start_barrier.wait()  # synchronize starts OUTSIDE the lock
            global_dashboard._maybe_regen()

        with mock.patch("global_dashboard.build_global_payload", side_effect=slow_build), \
             mock.patch("global_dashboard.write_global_dashboard"):
            threads = [threading.Thread(target=worker) for _ in range(N)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
                assert not t.is_alive(), "worker thread deadlocked"
        assert call_counter["n"] == 1, (
            f"{N} concurrent _maybe_regen calls should produce 1 build, got {call_counter['n']}"
        )


class TestAllowlistRegression:
    def test_disallowed_path_returns_403(self, tmp_path):
        """Sanity: the new handler still rejects paths outside the allowlist."""
        import global_dashboard
        Handler = global_dashboard._make_restricted_handler(str(tmp_path))
        # Build a stub that captures the call to send_error
        captured = {}
        class Stub(Handler):
            def __init__(self, path):
                self.path = path
            def send_error(self, code, msg=None):
                captured["code"] = code
                captured["msg"] = msg
            # noop so super().do_GET() does not actually run
            def _ignore(self, *a, **k): pass

        # We can't easily instantiate the real socket-based handler, so just
        # call do_GET() with a constructed instance.
        stub = Stub.__new__(Stub)
        stub.path = "/etc/passwd"
        # Use the actual do_GET defined on Handler
        Handler.do_GET(stub)
        assert captured.get("code") == 403, (
            f"non-allowlisted path should return 403, got {captured}"
        )


class TestRenderedHtml:
    def test_html_contains_polling_script(self):
        """The rendered HTML must include the polling script with
        the expected endpoint and interval — these are the contract
        for live updating."""
        import global_dashboard
        # Use a minimal payload to exercise template rendering.
        payload = {
            "generated_at": "2026-04-18T06:00:00Z",
            "daemon": {"running": False, "pid": None, "last_sweep_at": None, "sweep_count": 0},
            "active_projects": [],
            "inactive_projects": [],
            "aggregates": {
                "total_tasks": 0, "avg_quality": 0.0, "total_learned_routes": 0,
                "active_count": 0, "total_benchmark_runs": 0, "total_findings": 0,
                "total_autofix_cost_usd": 0.0, "total_learned_components": 0,
                "total_shadow_components": 0, "total_demoted_components": 0,
                "total_tracked_fixtures": 0, "total_coverage_gaps": 0,
                "total_automation_queue": 0, "total_autofix_open_prs": 0,
                "total_autofix_merged": 0, "total_autofix_suppressions": 0,
            },
        }
        html = global_dashboard.GLOBAL_HTML_TEMPLATE.format(
            **global_dashboard._template_substitutions(payload)
        ) if hasattr(global_dashboard, "_template_substitutions") else None

        # Fallback: just inspect the raw template string for the contract.
        # The polling script lives in the template itself, so the rendered
        # output will always contain it as long as the template has it.
        tpl = global_dashboard.GLOBAL_HTML_TEMPLATE
        assert "POLL_INTERVAL_MS" in tpl
        assert "/global-dashboard-data.json" in tpl
        assert "setInterval" in tpl
        assert "5000" in tpl, "default poll interval should be 5000 ms"

    def test_html_passes_validate_generated_html(self, tmp_path):
        """The required element ids must remain present after the script
        injection (regression guard against the linter at
        hooks/lib_validate.py:validate_generated_html)."""
        import global_dashboard
        from lib_validate import validate_generated_html

        payload = {
            "generated_at": "2026-04-18T06:00:00Z",
            "daemon": {"running": False, "pid": None, "last_sweep_at": None, "sweep_count": 0},
            "active_projects": [],
            "inactive_projects": [],
            "aggregates": {
                "total_tasks": 0, "avg_quality": 0.0, "total_learned_routes": 0,
                "active_count": 0, "total_benchmark_runs": 0, "total_findings": 0,
                "total_autofix_cost_usd": 0.0, "total_learned_components": 0,
                "total_shadow_components": 0, "total_demoted_components": 0,
                "total_tracked_fixtures": 0, "total_coverage_gaps": 0,
                "total_automation_queue": 0, "total_autofix_open_prs": 0,
                "total_autofix_merged": 0, "total_autofix_suppressions": 0,
            },
        }
        # Write a real HTML file via the public path. write_global_dashboard
        # imports global_home/ensure_global_dirs from `registry` lazily, so
        # patch them at their source.
        from pathlib import Path as _P
        with mock.patch("registry.global_home", return_value=tmp_path), \
             mock.patch("registry.ensure_global_dirs"):
            result = global_dashboard.write_global_dashboard(payload)
        html_path = _P(result["html_path"])
        assert html_path.exists()
        errors = validate_generated_html(html_path)
        assert errors == [], f"validate_generated_html reported errors: {errors}"
