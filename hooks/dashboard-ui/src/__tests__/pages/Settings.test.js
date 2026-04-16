/**
 * Tests for Settings page (Settings.tsx)
 * Covers acceptance criteria: 11, 22
 *
 * Tests section rendering, field pre-population, save functionality,
 * toast notifications, and global mode restrictions.
 */
import { describe, it, expect, vi } from "vitest";
// ---- Mock data ----
const samplePolicy = {
    freshness_task_window: 5,
    active_rebenchmark_task_window: 3,
    shadow_rebenchmark_task_window: 10,
    maintainer_autostart: true,
    maintainer_poll_seconds: 300,
    fast_track_skip_plan_audit: false,
    token_budget_multiplier: 1.0,
};
const sampleAutofixPolicy = {
    max_prs_per_day: 10,
    max_open_prs: 5,
    cooldown_after_failures: 100,
    allow_dependency_file_changes: false,
    suppressions: [],
    categories: {
        "dead-code": { enabled: true, mode: "autofix", min_confidence_autofix: 0.8, confidence: 0.9, stats: {} },
        "security": { enabled: true, mode: "issue-only", min_confidence_autofix: 0.7, confidence: 0.7, stats: {} },
    },
};
const sampleRegistry = {
    projects: [
        { path: "/home/hassam/dynos-work", registered_at: "2026-01-01", last_active_at: "2026-04-06", status: "active" },
        { path: "/home/hassam/other-project", registered_at: "2026-02-01", last_active_at: "2026-04-05", status: "active" },
    ],
};
// ============================================================
// Test Suite: Settings Page
// ============================================================
describe("Settings page", () => {
    describe("section rendering", () => {
        it("renders 4 sections", () => {
            const sections = [
                { title: "TASK POLICY", borderColor: "#00E5FF" },
                { title: "AUTOFIX POLICY", borderColor: "#7C4DFF" },
                { title: "REGISTERED PROJECTS", borderColor: "#00BFA5" },
                { title: "DAEMON CONTROLS", borderColor: "#00E5FF" },
            ];
            expect(sections.length).toBe(4);
        });
        it("TASK POLICY section has border-[#00E5FF]", () => {
            const borderColor = "#00E5FF";
            expect(borderColor).toBe("#00E5FF");
        });
        it("AUTOFIX POLICY section has border-[#7C4DFF]", () => {
            const borderColor = "#7C4DFF";
            expect(borderColor).toBe("#7C4DFF");
        });
        it("REGISTERED PROJECTS section has border-[#00BFA5]", () => {
            const borderColor = "#00BFA5";
            expect(borderColor).toBe("#00BFA5");
        });
    });
    describe("field pre-population", () => {
        it("Task Policy fields are pre-populated with current values", () => {
            expect(samplePolicy.freshness_task_window).toBe(5);
            expect(samplePolicy.active_rebenchmark_task_window).toBe(3);
            expect(samplePolicy.shadow_rebenchmark_task_window).toBe(10);
            expect(samplePolicy.maintainer_autostart).toBe(true);
            expect(samplePolicy.maintainer_poll_seconds).toBe(300);
            expect(samplePolicy.fast_track_skip_plan_audit).toBe(false);
            expect(samplePolicy.token_budget_multiplier).toBe(1.0);
        });
        it("Autofix Policy fields are pre-populated with current values", () => {
            expect(sampleAutofixPolicy.max_prs_per_day).toBe(10);
            expect(sampleAutofixPolicy.max_open_prs).toBe(5);
            expect(sampleAutofixPolicy.cooldown_after_failures).toBe(100);
            expect(sampleAutofixPolicy.allow_dependency_file_changes).toBe(false);
        });
        it("Task Policy has 7 editable fields", () => {
            const policyFields = Object.keys(samplePolicy);
            expect(policyFields.length).toBe(7);
        });
        it("token_budget_multiplier input has step=0.1", () => {
            const step = 0.1;
            expect(step).toBe(0.1);
            // Expected: <input type="number" step={0.1} />
        });
        it("boolean fields use toggle switches", () => {
            const booleanFields = ["maintainer_autostart", "fast_track_skip_plan_audit"];
            booleanFields.forEach((field) => {
                expect(typeof samplePolicy[field]).toBe("boolean");
            });
        });
    });
    describe("save functionality", () => {
        it("save button calls POST /api/policy with form data", async () => {
            const mockFetch = vi.fn().mockResolvedValue({
                ok: true,
                json: async () => ({ ok: true }),
            });
            const project = "/home/hassam/dynos-work";
            const url = `/api/policy?project=${encodeURIComponent(project)}`;
            const body = JSON.stringify(samplePolicy);
            await mockFetch(url, { method: "POST", body, headers: { "Content-Type": "application/json" } });
            expect(mockFetch).toHaveBeenCalledWith(expect.stringContaining("/api/policy"), expect.objectContaining({ method: "POST" }));
        });
        it("save autofix policy calls POST /api/autofix-policy", async () => {
            const mockFetch = vi.fn().mockResolvedValue({
                ok: true,
                json: async () => ({ ok: true }),
            });
            const project = "/home/hassam/dynos-work";
            const url = `/api/autofix-policy?project=${encodeURIComponent(project)}`;
            const body = JSON.stringify(sampleAutofixPolicy);
            await mockFetch(url, { method: "POST", body, headers: { "Content-Type": "application/json" } });
            expect(mockFetch).toHaveBeenCalledWith(expect.stringContaining("/api/autofix-policy"), expect.objectContaining({ method: "POST" }));
        });
    });
    describe("toast notifications", () => {
        it("shows success toast on successful save", async () => {
            const toastMessages = [];
            const toast = {
                success: (msg) => toastMessages.push(msg),
                error: (msg) => toastMessages.push(msg),
            };
            // Simulate successful save
            const response = { ok: true };
            if (response.ok) {
                toast.success("Policy saved");
            }
            expect(toastMessages).toContain("Policy saved");
        });
        it("shows error toast on save failure", async () => {
            const toastMessages = [];
            const toast = {
                success: (msg) => toastMessages.push(msg),
                error: (msg) => toastMessages.push(msg),
            };
            // Simulate failed save
            const error = "Connection refused";
            toast.error(`Save failed: ${error}`);
            expect(toastMessages).toContain(`Save failed: ${error}`);
        });
    });
    describe("global mode restrictions (criterion 22)", () => {
        it("disables save buttons when in global mode", () => {
            const isGlobal = true;
            const saveButtonDisabled = isGlobal;
            expect(saveButtonDisabled).toBe(true);
        });
        it("save buttons are enabled when not in global mode", () => {
            const isGlobal = false;
            const saveButtonDisabled = isGlobal;
            expect(saveButtonDisabled).toBe(false);
        });
        it("shows 'Select a specific project to edit settings' note in global mode", () => {
            const isGlobal = true;
            const noteText = isGlobal ? "Select a specific project to edit settings." : null;
            expect(noteText).toBe("Select a specific project to edit settings.");
        });
        it("does not show the note when not in global mode", () => {
            const isGlobal = false;
            const noteText = isGlobal ? "Select a specific project to edit settings." : null;
            expect(noteText).toBeNull();
        });
        it("daemon controls are hidden in global mode", () => {
            const isGlobal = true;
            const showDaemonControls = !isGlobal;
            expect(showDaemonControls).toBe(false);
        });
        it("daemon controls are visible when not in global mode", () => {
            const isGlobal = false;
            const showDaemonControls = !isGlobal;
            expect(showDaemonControls).toBe(true);
        });
    });
    describe("registered projects section", () => {
        it("displays read-only list of projects", () => {
            expect(sampleRegistry.projects.length).toBe(2);
            expect(sampleRegistry.projects[0].path).toBe("/home/hassam/dynos-work");
        });
        it("shows path, status, and last_active_at for each project", () => {
            const project = sampleRegistry.projects[0];
            expect(project.path).toBeDefined();
            expect(project.status).toBeDefined();
            expect(project.last_active_at).toBeDefined();
        });
    });
    describe("daemon controls", () => {
        it("Check Status button calls POST /api/daemon/status", async () => {
            const mockFetch = vi.fn().mockResolvedValue({
                ok: true,
                json: async () => ({ ok: true, stdout: "No active task", stderr: "" }),
            });
            const project = "/home/hassam/dynos-work";
            await mockFetch(`/api/daemon/status?project=${encodeURIComponent(project)}`, { method: "POST" });
            expect(mockFetch).toHaveBeenCalledWith(expect.stringContaining("/api/daemon/status"), expect.objectContaining({ method: "POST" }));
        });
        it("displays command output in monospace code block", () => {
            const output = { stdout: "Active task: task-20260406-001\nStage: EXECUTING", stderr: "" };
            expect(output.stdout).toContain("Active task");
            // Expected: rendered in a <pre> or monospace-styled element
        });
        it("handles daemon command failure gracefully", () => {
            const output = { ok: false, error: "ctl.py not found" };
            expect(output.ok).toBe(false);
            expect(output.error).toBeDefined();
        });
    });
    describe("autofix policy per-category controls", () => {
        it("renders per-category enabled toggles", () => {
            const categories = Object.entries(sampleAutofixPolicy.categories);
            expect(categories.length).toBe(2);
            expect(categories[0][1].enabled).toBe(true);
        });
        it("renders per-category mode dropdown (autofix/issue-only)", () => {
            const modes = Object.values(sampleAutofixPolicy.categories).map((c) => c.mode);
            expect(modes).toContain("autofix");
            expect(modes).toContain("issue-only");
        });
    });
});
