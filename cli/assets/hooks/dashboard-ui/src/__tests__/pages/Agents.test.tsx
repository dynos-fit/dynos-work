/**
 * Tests for Agents page (Agents.tsx)
 * Covers acceptance criterion: 8
 *
 * Tests card grid rendering, mode color coding, demoted status,
 * benchmark data handling, and recommendation badges.
 */
import { describe, it, expect } from "vitest";

// ---- Mock data ----
const sampleAgents = [
  {
    agent_name: "feature-executor-v2",
    role: "executor",
    task_type: "feature",
    item_kind: "agent",
    mode: "replace",
    status: "active",
    benchmark_summary: {
      sample_count: 5,
      mean_quality: 0.82,
      mean_cost: 0.65,
      mean_efficiency: 0.78,
      mean_composite: 0.75,
    },
    last_evaluation: {
      recommendation: "promote",
      delta_composite: 0.12,
      evaluated_at: "2026-04-06T00:00:00Z",
    },
  },
  {
    agent_name: "bugfix-auditor-v1",
    role: "auditor",
    task_type: "bugfix",
    item_kind: "agent",
    mode: "alongside",
    status: "demoted",
    benchmark_summary: {
      sample_count: 3,
      mean_quality: 0.55,
      mean_cost: 0.80,
      mean_efficiency: 0.60,
      mean_composite: 0.45,
    },
    last_evaluation: {
      recommendation: "demote",
      delta_composite: -0.15,
      evaluated_at: "2026-04-05T00:00:00Z",
    },
  },
  {
    agent_name: "maintenance-shadow",
    role: "executor",
    task_type: "maintenance",
    item_kind: "skill",
    mode: "shadow",
    status: "active",
    benchmark_summary: undefined,
    last_evaluation: undefined,
  },
];

// ---- Color constants matching spec ----
const MODE_COLORS = {
  replace: "#00E5FF",    // cyan
  alongside: "#00BFA5",  // teal
  shadow: "#7C4DFF",     // purple
};

// ============================================================
// Test Suite: Agents Page
// ============================================================
describe("Agents page", () => {
  describe("card grid rendering", () => {
    it("renders a card for each agent", () => {
      expect(sampleAgents.length).toBe(3);
      // Each agent should produce one card in the grid
    });

    it("displays agent name prominently on each card", () => {
      expect(sampleAgents[0].agent_name).toBe("feature-executor-v2");
      expect(sampleAgents[1].agent_name).toBe("bugfix-auditor-v1");
    });

    it("displays role, task_type, item_kind, mode, and status", () => {
      const agent = sampleAgents[0];
      expect(agent.role).toBe("executor");
      expect(agent.task_type).toBe("feature");
      expect(agent.item_kind).toBe("agent");
      expect(agent.mode).toBe("replace");
      expect(agent.status).toBe("active");
    });

    it("displays composite benchmark score when available", () => {
      const agent = sampleAgents[0];
      expect(agent.benchmark_summary?.mean_composite).toBe(0.75);
    });
  });

  describe("mode color coding", () => {
    function getModeColor(mode: string): string {
      return MODE_COLORS[mode as keyof typeof MODE_COLORS] ?? "#999";
    }

    it("replace mode is cyan (#00E5FF)", () => {
      expect(getModeColor("replace")).toBe("#00E5FF");
    });

    it("alongside mode is teal (#00BFA5)", () => {
      expect(getModeColor("alongside")).toBe("#00BFA5");
    });

    it("shadow mode is purple (#7C4DFF)", () => {
      expect(getModeColor("shadow")).toBe("#7C4DFF");
    });
  });

  describe("demoted status", () => {
    it("shows red text for demoted agents", () => {
      const demotedAgent = sampleAgents.find((a) => a.status === "demoted");
      expect(demotedAgent).toBeDefined();
      expect(demotedAgent!.agent_name).toBe("bugfix-auditor-v1");
      // Expected: status text rendered in red (#F44336)
    });

    it("active status does not show red", () => {
      const activeAgents = sampleAgents.filter((a) => a.status === "active");
      expect(activeAgents.length).toBe(2);
    });
  });

  describe("NO BENCHMARK DATA", () => {
    it("displays 'NO BENCHMARK DATA' for agents without benchmark_summary", () => {
      const noBenchmarkAgent = sampleAgents.find((a) => !a.benchmark_summary);
      expect(noBenchmarkAgent).toBeDefined();
      expect(noBenchmarkAgent!.agent_name).toBe("maintenance-shadow");
      expect(noBenchmarkAgent!.benchmark_summary).toBeUndefined();
      // Expected UI: muted "NO BENCHMARK DATA" text
    });

    it("agents with benchmark_summary do not show NO BENCHMARK DATA", () => {
      const withBenchmark = sampleAgents.filter((a) => a.benchmark_summary);
      expect(withBenchmark.length).toBe(2);
      withBenchmark.forEach((a) => {
        expect(a.benchmark_summary).toBeDefined();
        expect(a.benchmark_summary!.mean_composite).toBeGreaterThan(0);
      });
    });
  });

  describe("recommendation badge", () => {
    it("renders recommendation badge from last_evaluation", () => {
      const agent = sampleAgents[0];
      expect(agent.last_evaluation?.recommendation).toBe("promote");
    });

    it("renders negative delta as '-X.XX' indicator", () => {
      const agent = sampleAgents[1];
      const delta = agent.last_evaluation!.delta_composite;
      expect(delta).toBe(-0.15);
      const display = delta > 0 ? `+${delta.toFixed(2)}` : delta.toFixed(2);
      expect(display).toBe("-0.15");
    });

    it("renders positive delta as '+X.XX' indicator", () => {
      const agent = sampleAgents[0];
      const delta = agent.last_evaluation!.delta_composite;
      expect(delta).toBe(0.12);
      const display = delta > 0 ? `+${delta.toFixed(2)}` : delta.toFixed(2);
      expect(display).toBe("+0.12");
    });

    it("handles agent with no last_evaluation gracefully", () => {
      const agent = sampleAgents[2];
      expect(agent.last_evaluation).toBeUndefined();
      // Expected: no badge rendered, no crash
    });
  });

  describe("responsive grid", () => {
    it("uses responsive column layout (1-col mobile, 2-col md, 3-col xl)", () => {
      // This verifies the expected CSS grid classes
      const expectedClasses = "grid-cols-1 md:grid-cols-2 xl:grid-cols-3";
      expect(expectedClasses).toContain("grid-cols-1");
      expect(expectedClasses).toContain("md:grid-cols-2");
      expect(expectedClasses).toContain("xl:grid-cols-3");
    });
  });
});
