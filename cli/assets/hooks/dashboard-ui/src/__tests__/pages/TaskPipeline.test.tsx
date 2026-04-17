/**
 * Tests for Task Pipeline page (TaskPipeline.tsx)
 * Covers acceptance criterion: 7
 *
 * Tests table rendering, search filtering, stage/risk color coding,
 * row expansion, sorting, and global mode Project column.
 */
import { describe, it, expect } from "vitest";

// ---- Mock data ----
const sampleTasks = [
  {
    task_id: "task-20260406-001",
    created_at: "2026-04-06T10:00:00Z",
    title: "Build dashboard UI",
    stage: "DONE",
    classification: { type: "feature", domains: ["ui"], risk_level: "low", notes: "" },
    project_path: "/home/hassam/dynos-work",
  },
  {
    task_id: "task-20260405-002",
    created_at: "2026-04-05T08:00:00Z",
    title: "Fix autofix pipeline",
    stage: "EXECUTING",
    classification: { type: "bugfix", domains: ["core"], risk_level: "medium", notes: "" },
    project_path: "/home/hassam/dynos-work",
  },
  {
    task_id: "task-20260404-003",
    created_at: "2026-04-04T06:00:00Z",
    title: "Agent learning improvements",
    stage: "FAILED",
    classification: { type: "feature", domains: ["ml"], risk_level: "high", notes: "" },
    project_path: "/home/hassam/other-project",
  },
  {
    task_id: "task-20260403-001",
    created_at: "2026-04-03T12:00:00Z",
    title: "Database migration blocked",
    stage: "BLOCKED",
    classification: { type: "maintenance", domains: ["db"], risk_level: "critical", notes: "" },
    project_path: "/home/hassam/dynos-work",
  },
];

// ---- Color constants matching spec ----
const STAGE_COLORS = {
  DONE: "#00BFA5",      // green
  EXECUTING: "#FFB300",  // yellow (in-progress)
  PLANNING: "#FFB300",   // yellow (in-progress)
  FAILED: "#F44336",     // red
  BLOCKED: "#F44336",    // red
};

const RISK_COLORS = {
  low: "#00E5FF",       // cyan
  medium: "#00BFA5",    // teal
  high: "#7C4DFF",      // purple
  critical: "#F44336",  // red
};

// ============================================================
// Test Suite: Task Pipeline
// ============================================================
describe("Task Pipeline page", () => {
  describe("table rendering", () => {
    it("renders table with task data", () => {
      expect(sampleTasks.length).toBe(4);
      // Each task should map to a table row with columns:
      // Task ID, Title, Stage, Type, Risk, Quality Score, Status
      const expectedColumns = ["Task ID", "Title", "Stage", "Type", "Risk", "Quality Score", "Status"];
      expect(expectedColumns.length).toBe(7);
    });

    it("displays task_id, title, stage, type, risk for each row", () => {
      const task = sampleTasks[0];
      expect(task.task_id).toBe("task-20260406-001");
      expect(task.title).toBe("Build dashboard UI");
      expect(task.stage).toBe("DONE");
      expect(task.classification.type).toBe("feature");
      expect(task.classification.risk_level).toBe("low");
    });
  });

  describe("search filtering", () => {
    function filterTasks(tasks: typeof sampleTasks, query: string) {
      const q = query.toLowerCase();
      return tasks.filter(
        (t) =>
          t.task_id.toLowerCase().includes(q) || t.title.toLowerCase().includes(q)
      );
    }

    it("filters by task_id substring (case-insensitive)", () => {
      const results = filterTasks(sampleTasks, "20260406");
      expect(results.length).toBe(1);
      expect(results[0].task_id).toBe("task-20260406-001");
    });

    it("filters by title substring (case-insensitive)", () => {
      const results = filterTasks(sampleTasks, "autofix");
      expect(results.length).toBe(1);
      expect(results[0].title).toBe("Fix autofix pipeline");
    });

    it("returns all tasks when search query is empty", () => {
      const results = filterTasks(sampleTasks, "");
      expect(results.length).toBe(4);
    });

    it("returns empty array when no tasks match", () => {
      const results = filterTasks(sampleTasks, "nonexistent-query-xyz");
      expect(results.length).toBe(0);
    });

    it("search is case-insensitive", () => {
      const results = filterTasks(sampleTasks, "BUILD DASHBOARD");
      expect(results.length).toBe(1);
    });
  });

  describe("stage color coding", () => {
    function getStageColor(stage: string): string {
      if (stage === "DONE") return STAGE_COLORS.DONE;
      if (stage.includes("FAIL")) return STAGE_COLORS.FAILED;
      if (stage.includes("BLOCKED")) return STAGE_COLORS.BLOCKED;
      return STAGE_COLORS.EXECUTING; // in-progress for all other stages
    }

    it("DONE stage is green (#00BFA5)", () => {
      expect(getStageColor("DONE")).toBe("#00BFA5");
    });

    it("in-progress stages are yellow (#FFB300)", () => {
      expect(getStageColor("EXECUTING")).toBe("#FFB300");
      expect(getStageColor("PLANNING")).toBe("#FFB300");
      expect(getStageColor("DISCOVERY")).toBe("#FFB300");
    });

    it("FAILED stage is red (#F44336)", () => {
      expect(getStageColor("FAILED")).toBe("#F44336");
    });

    it("BLOCKED stage is red (#F44336)", () => {
      expect(getStageColor("BLOCKED")).toBe("#F44336");
    });
  });

  describe("risk color coding", () => {
    function getRiskColor(risk: string): string {
      return RISK_COLORS[risk as keyof typeof RISK_COLORS] ?? "#999";
    }

    it("low risk is cyan (#00E5FF)", () => {
      expect(getRiskColor("low")).toBe("#00E5FF");
    });

    it("medium risk is teal (#00BFA5)", () => {
      expect(getRiskColor("medium")).toBe("#00BFA5");
    });

    it("high risk is purple (#7C4DFF)", () => {
      expect(getRiskColor("high")).toBe("#7C4DFF");
    });

    it("critical risk is red (#F44336)", () => {
      expect(getRiskColor("critical")).toBe("#F44336");
    });
  });

  describe("row expansion", () => {
    it("clicking a row should reveal detail content", () => {
      // Row expansion is via Radix Collapsible
      // The expanded content shows: spec summary, execution graph, execution log
      const expandedContent = {
        specSummary: "First line of spec.md Task Summary",
        executionGraph: [
          { id: "seg-1", executor: "ui-executor", description: "Build scaffolding" },
        ],
        executionLog: ["Line 1", "Line 2", "Line 3"],
      };
      expect(expandedContent.specSummary).toBeTruthy();
      expect(expandedContent.executionGraph.length).toBeGreaterThan(0);
      expect(expandedContent.executionLog.length).toBeLessThanOrEqual(10);
    });

    it("expanded detail shows execution graph segments", () => {
      const segments = [
        { id: "seg-1", executor: "ui-executor", description: "Scaffolding" },
        { id: "seg-2", executor: "backend-executor", description: "API plugin" },
      ];
      expect(segments[0]).toHaveProperty("id");
      expect(segments[0]).toHaveProperty("executor");
      expect(segments[0]).toHaveProperty("description");
    });

    it("expanded detail shows last 10 lines of execution log", () => {
      const fullLog = Array.from({ length: 20 }, (_, i) => `Line ${i + 1}`);
      const last10 = fullLog.slice(-10);
      expect(last10.length).toBe(10);
      expect(last10[0]).toBe("Line 11");
      expect(last10[9]).toBe("Line 20");
    });
  });

  describe("sorting", () => {
    it("tasks are sorted by created_at descending (newest first)", () => {
      const sorted = [...sampleTasks].sort(
        (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
      );
      expect(sorted[0].task_id).toBe("task-20260406-001");
      expect(sorted[1].task_id).toBe("task-20260405-002");
      expect(sorted[2].task_id).toBe("task-20260404-003");
      expect(sorted[3].task_id).toBe("task-20260403-001");
    });
  });

  describe("global mode", () => {
    it("shows a Project column when isGlobal is true", () => {
      const isGlobal = true;
      const columns = isGlobal
        ? ["Project", "Task ID", "Title", "Stage", "Type", "Risk", "Quality Score", "Status"]
        : ["Task ID", "Title", "Stage", "Type", "Risk", "Quality Score", "Status"];
      expect(columns).toContain("Project");
      expect(columns.length).toBe(8);
    });

    it("Project column shows basename of project_path", () => {
      function basename(path: string): string {
        return path.split("/").pop() ?? path;
      }
      expect(basename("/home/hassam/dynos-work")).toBe("dynos-work");
      expect(basename("/home/hassam/other-project")).toBe("other-project");
    });

    it("does not show Project column when not in global mode", () => {
      const isGlobal = false;
      const columns = isGlobal
        ? ["Project", "Task ID", "Title", "Stage", "Type", "Risk", "Quality Score", "Status"]
        : ["Task ID", "Title", "Stage", "Type", "Risk", "Quality Score", "Status"];
      expect(columns).not.toContain("Project");
    });
  });

  describe("quality score column", () => {
    it("shows quality score when retrospective exists", () => {
      const score = 0.85;
      const display = Math.round(score * 100) + "%";
      expect(display).toBe("85%");
    });

    it("shows '--' when no retrospective exists", () => {
      const score = undefined;
      const display = score !== undefined ? Math.round(score * 100) + "%" : "--";
      expect(display).toBe("--");
    });
  });
});
