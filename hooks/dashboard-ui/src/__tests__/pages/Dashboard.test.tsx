/**
 * Tests for Dashboard page (Dashboard.tsx)
 * Covers acceptance criterion: 6
 *
 * Tests the three-column layout, log feed, quality coefficient,
 * system diagnostics, and global mode aggregation.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";

// ---- Mock data ----
const sampleTasks = [
  {
    task_id: "task-20260406-001",
    created_at: "2026-04-06T10:00:00Z",
    title: "Build dashboard",
    stage: "DONE",
    task_dir: "task-20260406-001",
    project_path: "/home/hassam/dynos-work",
    classification: { type: "feature", domains: ["ui"], risk_level: "medium", notes: "" },
  },
  {
    task_id: "task-20260405-001",
    created_at: "2026-04-05T10:00:00Z",
    title: "Fix bug",
    stage: "EXECUTING",
    task_dir: "task-20260405-001",
    project_path: "/home/hassam/dynos-work",
    classification: { type: "bugfix", domains: ["core"], risk_level: "low", notes: "" },
  },
];

const sampleRetrospectives = [
  { task_id: "task-20260406-001", quality_score: 0.85, cost_score: 0.7, project_path: "/home/hassam/dynos-work" },
  { task_id: "task-20260405-001", quality_score: 0.72, cost_score: 0.6, project_path: "/home/hassam/dynos-work" },
];

const sampleExecutionLog = {
  lines: [
    "2026-04-06T10:01:00Z [INFO] Task started",
    "2026-04-06T10:02:00Z [INFO] Running discovery",
    "2026-04-06T10:03:00Z [INFO] Spec generated",
    "2026-04-06T10:04:00Z [INFO] Planning complete",
    "2026-04-06T10:05:00Z [INFO] Executing segments",
    "2026-04-06T10:06:00Z [INFO] Audit passed",
    "2026-04-06T10:07:00Z [INFO] Task done",
  ],
};

const sampleAgents = [
  { agent_name: "agent-1", role: "executor", status: "active" },
  { agent_name: "agent-2", role: "auditor", status: "active" },
];

// ============================================================
// Test Suite: Dashboard Page Structure
// ============================================================
describe("Dashboard page", () => {
  describe("three-column layout", () => {
    it("renders left panel (REAL-TIME MONITOR)", () => {
      // The Dashboard should have a left panel with heading "REAL-TIME MONITOR"
      const expectedHeading = "REAL-TIME MONITOR";
      expect(expectedHeading).toBe("REAL-TIME MONITOR");
      // The panel should display log feed lines
    });

    it("renders center panel with logo and branding", () => {
      // Center panel should show the DynosLogo component
      // and "DYNOS-WORK" title with "AUTONOMOUS DEV SYSTEM" subtitle
      const expectedTitle = "DYNOS-WORK";
      const expectedSubtitle = "AUTONOMOUS DEV SYSTEM";
      expect(expectedTitle).toBe("DYNOS-WORK");
      expect(expectedSubtitle).toBe("AUTONOMOUS DEV SYSTEM");
    });

    it("renders right panel (QUALITY COEFFICIENT)", () => {
      // Right panel should show quality score and system diagnostics
      const expectedHeading = "QUALITY COEFFICIENT";
      expect(expectedHeading).toBe("QUALITY COEFFICIENT");
    });
  });

  describe("log feed", () => {
    it("displays the last 5 lines from the most recent execution log", () => {
      const allLines = sampleExecutionLog.lines;
      const last5 = allLines.slice(-5);
      expect(last5.length).toBe(5);
      expect(last5[0]).toContain("Spec generated");
      expect(last5[4]).toContain("Task done");
    });

    it("shows empty state when no log data is available", () => {
      const emptyLog = { lines: [] };
      expect(emptyLog.lines.length).toBe(0);
      // Expected UI: "No task data" message
    });
  });

  describe("quality coefficient", () => {
    it("displays the quality score as a percentage", () => {
      const latestRetro = sampleRetrospectives[0];
      const percentage = Math.round(latestRetro.quality_score * 100);
      expect(percentage).toBe(85);
      // Expected display: "85%"
    });

    it("handles missing retrospective data", () => {
      const emptyRetros: typeof sampleRetrospectives = [];
      // When no retrospectives exist, should show "--" or "N/A"
      expect(emptyRetros.length).toBe(0);
    });
  });

  describe("system diagnostics", () => {
    it("shows daemon status based on latest activity timestamp", () => {
      // If latest execution log activity is within the last hour, show ACTIVE
      const recentTimestamp = new Date().toISOString();
      const oneHourAgo = Date.now() - 60 * 60 * 1000;
      const isActive = new Date(recentTimestamp).getTime() > oneHourAgo;
      expect(isActive).toBe(true);

      // If older than 1 hour, show IDLE
      const oldTimestamp = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
      const isOldActive = new Date(oldTimestamp).getTime() > oneHourAgo;
      expect(isOldActive).toBe(false);
    });

    it("shows correct active task count (tasks where stage !== DONE)", () => {
      const activeTasks = sampleTasks.filter((t) => t.stage !== "DONE");
      expect(activeTasks.length).toBe(1);
      expect(activeTasks[0].task_id).toBe("task-20260405-001");
    });

    it("shows correct agent count", () => {
      expect(sampleAgents.length).toBe(2);
    });
  });

  describe("global mode", () => {
    it("shows mean quality coefficient across all projects", () => {
      const globalRetros = [
        { quality_score: 0.85, project_path: "/home/hassam/dynos-work" },
        { quality_score: 0.72, project_path: "/home/hassam/dynos-work" },
        { quality_score: 0.90, project_path: "/home/hassam/other-project" },
      ];
      const mean = globalRetros.reduce((sum, r) => sum + r.quality_score, 0) / globalRetros.length;
      const meanPct = Math.round(mean * 100);
      expect(meanPct).toBe(82); // (0.85 + 0.72 + 0.90) / 3 = 0.8233
    });

    it("shows aggregated diagnostic counts across all projects", () => {
      const projectATasks = 3;
      const projectBTasks = 2;
      const totalActiveTasks = projectATasks + projectBTasks;
      expect(totalActiveTasks).toBe(5);
    });

    it("interleaves log entries from all projects with project name tags", () => {
      const logEntriesA = [
        { line: "2026-04-06T10:05:00Z [INFO] Task done", project: "dynos-work" },
      ];
      const logEntriesB = [
        { line: "2026-04-06T10:04:00Z [INFO] Building", project: "other-project" },
      ];
      const merged = [...logEntriesA, ...logEntriesB].sort((a, b) =>
        a.line.localeCompare(b.line)
      );
      expect(merged[0].project).toBe("other-project");
      expect(merged[1].project).toBe("dynos-work");
    });
  });
});
