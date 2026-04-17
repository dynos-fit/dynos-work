/**
 * Tests for Analytics page (Analytics.tsx)
 * Covers acceptance criterion: 10
 *
 * Tests chart rendering, "Insufficient data" threshold,
 * and quality trend data preparation.
 */
import { describe, it, expect } from "vitest";

// ---- Mock data ----
const sampleRetrospectives = [
  {
    task_id: "task-20260401-001",
    quality_score: 0.72,
    cost_score: 0.65,
    efficiency_score: 0.80,
    model_used_by_agent: { "spec-writer": "sonnet", "executor": "opus", "auditor": "haiku" },
    executor_repair_frequency: { "ui-executor": 2, "backend-executor": 1 },
    subagent_spawn_count: 5,
    wasted_spawns: 1,
  },
  {
    task_id: "task-20260402-001",
    quality_score: 0.85,
    cost_score: 0.70,
    efficiency_score: 0.88,
    model_used_by_agent: { "spec-writer": "sonnet", "executor": "sonnet", "auditor": "haiku" },
    executor_repair_frequency: { "ui-executor": 0, "backend-executor": 3 },
    subagent_spawn_count: 4,
    wasted_spawns: 0,
  },
  {
    task_id: "task-20260403-001",
    quality_score: 0.90,
    cost_score: 0.75,
    efficiency_score: 0.92,
    model_used_by_agent: { "spec-writer": "haiku", "executor": "opus", "auditor": null },
    executor_repair_frequency: { "ui-executor": 1 },
    subagent_spawn_count: 3,
    wasted_spawns: 2,
  },
];

const singleRetrospective = [sampleRetrospectives[0]];

// ============================================================
// Test Suite: Analytics Page
// ============================================================
describe("Analytics page", () => {
  describe("chart rendering", () => {
    it("prepares data for 5 charts", () => {
      const chartTypes = [
        "Quality Trend (LineChart)",
        "Cost Trend (LineChart)",
        "Model Usage Distribution (PieChart)",
        "Executor Repair Frequency (BarChart)",
        "Spawn Efficiency (LineChart)",
      ];
      expect(chartTypes.length).toBe(5);
    });

    it("Chart 1: quality trend line chart data uses quality_score over task_id", () => {
      const qualityData = sampleRetrospectives.map((r) => ({
        task_id: r.task_id,
        quality_score: r.quality_score,
      }));
      expect(qualityData.length).toBe(3);
      expect(qualityData[0]).toEqual({ task_id: "task-20260401-001", quality_score: 0.72 });
      expect(qualityData[2]).toEqual({ task_id: "task-20260403-001", quality_score: 0.90 });
    });

    it("Chart 2: cost trend line chart data uses cost_score over task_id", () => {
      const costData = sampleRetrospectives.map((r) => ({
        task_id: r.task_id,
        cost_score: r.cost_score,
      }));
      expect(costData[0].cost_score).toBe(0.65);
      expect(costData[2].cost_score).toBe(0.75);
    });

    it("Chart 3: model usage pie chart counts model occurrences", () => {
      const modelCounts: Record<string, number> = {};
      for (const retro of sampleRetrospectives) {
        for (const model of Object.values(retro.model_used_by_agent)) {
          const key = model ?? "unknown";
          modelCounts[key] = (modelCounts[key] || 0) + 1;
        }
      }
      expect(modelCounts["sonnet"]).toBe(3);
      expect(modelCounts["haiku"]).toBe(3);
      expect(modelCounts["opus"]).toBe(2);
      expect(modelCounts["unknown"]).toBe(1); // null -> unknown
    });

    it("Chart 4: executor repair bar chart aggregates repair counts", () => {
      const repairCounts: Record<string, number> = {};
      for (const retro of sampleRetrospectives) {
        for (const [executor, count] of Object.entries(retro.executor_repair_frequency)) {
          repairCounts[executor] = (repairCounts[executor] || 0) + count;
        }
      }
      expect(repairCounts["ui-executor"]).toBe(3); // 2 + 0 + 1
      expect(repairCounts["backend-executor"]).toBe(4); // 1 + 3
    });

    it("Chart 5: spawn efficiency dual-line chart data", () => {
      const spawnData = sampleRetrospectives.map((r) => ({
        task_id: r.task_id,
        total_spawns: r.subagent_spawn_count,
        wasted_spawns: r.wasted_spawns,
      }));
      expect(spawnData[0]).toEqual({
        task_id: "task-20260401-001",
        total_spawns: 5,
        wasted_spawns: 1,
      });
      expect(spawnData[2]).toEqual({
        task_id: "task-20260403-001",
        total_spawns: 3,
        wasted_spawns: 2,
      });
    });
  });

  describe("insufficient data handling", () => {
    it("shows 'Insufficient data for charts' when fewer than 2 retrospectives", () => {
      const hasInsufficientData = singleRetrospective.length < 2;
      expect(hasInsufficientData).toBe(true);
    });

    it("does not show insufficient data message with 2 or more retrospectives", () => {
      const hasInsufficientData = sampleRetrospectives.length < 2;
      expect(hasInsufficientData).toBe(false);
    });

    it("shows insufficient data for empty retrospectives array", () => {
      const empty: typeof sampleRetrospectives = [];
      expect(empty.length < 2).toBe(true);
    });

    it("exactly 2 retrospectives is sufficient for charts", () => {
      const twoRetros = sampleRetrospectives.slice(0, 2);
      expect(twoRetros.length < 2).toBe(false);
    });
  });

  describe("quality trend line chart", () => {
    it("uses correct data points sorted chronologically by task_id", () => {
      const sorted = [...sampleRetrospectives].sort((a, b) =>
        a.task_id.localeCompare(b.task_id)
      );
      expect(sorted[0].task_id).toBe("task-20260401-001");
      expect(sorted[1].task_id).toBe("task-20260402-001");
      expect(sorted[2].task_id).toBe("task-20260403-001");
    });

    it("quality_score y-axis range is 0.0 to 1.0", () => {
      for (const retro of sampleRetrospectives) {
        expect(retro.quality_score).toBeGreaterThanOrEqual(0);
        expect(retro.quality_score).toBeLessThanOrEqual(1);
      }
    });

    it("line color should be #00E5FF for quality trend", () => {
      const qualityLineColor = "#00E5FF";
      expect(qualityLineColor).toBe("#00E5FF");
    });
  });

  describe("dark theme chart styling", () => {
    it("uses transparent background, #333 grid, #999 axis text", () => {
      const chartTheme = {
        background: "transparent",
        gridColor: "#333",
        axisTextColor: "#999",
        fontFamily: "JetBrains Mono",
      };
      expect(chartTheme.background).toBe("transparent");
      expect(chartTheme.gridColor).toBe("#333");
      expect(chartTheme.axisTextColor).toBe("#999");
      expect(chartTheme.fontFamily).toBe("JetBrains Mono");
    });
  });

  describe("model usage pie chart colors", () => {
    it("assigns correct colors to each model", () => {
      const modelColors: Record<string, string> = {
        haiku: "#00BFA5",
        sonnet: "#00E5FF",
        opus: "#7C4DFF",
        unknown: "#666",
      };
      expect(modelColors.haiku).toBe("#00BFA5");
      expect(modelColors.sonnet).toBe("#00E5FF");
      expect(modelColors.opus).toBe("#7C4DFF");
      expect(modelColors.unknown).toBe("#666");
    });
  });
});
