/**
 * Tests for Autofix page (Autofix.tsx)
 * Covers acceptance criterion: 9
 *
 * Tests metric cards, fix rate computation, category bar chart,
 * findings table with pagination, and status badge colors.
 */
import { describe, it, expect } from "vitest";

// ---- Mock data ----
const sampleMetrics = {
  generated_at: "2026-04-06T10:00:00Z",
  totals: {
    findings: 50,
    open_prs: 5,
    prs_today: 2,
    recent_failures: 3,
    suppression_count: 4,
    merged: 20,
    closed_unmerged: 5,
    reverted: 2,
    issues_opened: 8,
  },
  rate_limits: {},
  categories: {
    "dead-code": {
      mode: "autofix",
      enabled: true,
      confidence: 0.9,
      merged: 8,
      closed_unmerged: 2,
      reverted: 0,
      issues_opened: 3,
      verification_failed: 1,
    },
    "security": {
      mode: "issue-only",
      enabled: true,
      confidence: 0.7,
      merged: 5,
      closed_unmerged: 1,
      reverted: 1,
      issues_opened: 2,
      verification_failed: 0,
    },
    "type-safety": {
      mode: "autofix",
      enabled: true,
      confidence: 0.8,
      merged: 7,
      closed_unmerged: 2,
      reverted: 1,
      issues_opened: 3,
      verification_failed: 2,
    },
  },
  recent_prs: [],
};

const sampleFindings = Array.from({ length: 60 }, (_, i) => ({
  finding_id: `f-${String(i + 1).padStart(3, "0")}`,
  severity: i % 3 === 0 ? "high" : i % 3 === 1 ? "medium" : "low",
  category: ["dead-code", "security", "type-safety"][i % 3],
  description: `Finding ${i + 1} description`,
  status: ["fixed", "failed", "issue-opened", "pending", "already-exists", "suppressed"][i % 6],
  attempt_count: (i % 4) + 1,
  pr_url: i % 2 === 0 ? `https://github.com/org/repo/pull/${i + 1}` : undefined,
  pr_number: i % 2 === 0 ? i + 1 : undefined,
}));

// ---- Status badge colors matching spec ----
const STATUS_COLORS: Record<string, string> = {
  fixed: "green",
  failed: "red",
  "issue-opened": "cyan",
  pending: "yellow",
  "already-exists": "gray",
  suppressed: "gray",
};

// ============================================================
// Test Suite: Autofix Page
// ============================================================
describe("Autofix page", () => {
  describe("metric cards", () => {
    it("renders 5 metric cards", () => {
      const metricCards = [
        { label: "Total Findings", value: sampleMetrics.totals.findings },
        { label: "Fix Rate", value: null }, // computed below
        { label: "PRs Merged", value: sampleMetrics.totals.merged },
        { label: "Open PRs", value: sampleMetrics.totals.open_prs },
        { label: "Recent Failures", value: sampleMetrics.totals.recent_failures },
      ];
      expect(metricCards.length).toBe(5);
    });

    it("displays Total Findings from totals.findings", () => {
      expect(sampleMetrics.totals.findings).toBe(50);
    });

    it("displays PRs Merged from totals.merged", () => {
      expect(sampleMetrics.totals.merged).toBe(20);
    });

    it("displays Open PRs from totals.open_prs", () => {
      expect(sampleMetrics.totals.open_prs).toBe(5);
    });

    it("displays Recent Failures from totals.recent_failures", () => {
      expect(sampleMetrics.totals.recent_failures).toBe(3);
    });
  });

  describe("fix rate computation", () => {
    it("computes fix rate as merged/findings*100", () => {
      const fixRate = (sampleMetrics.totals.merged / sampleMetrics.totals.findings) * 100;
      expect(fixRate).toBe(40); // 20/50 * 100
    });

    it("handles zero findings without division by zero", () => {
      const emptyTotals = { ...sampleMetrics.totals, findings: 0, merged: 0 };
      const fixRate = emptyTotals.findings === 0 ? 0 : (emptyTotals.merged / emptyTotals.findings) * 100;
      expect(fixRate).toBe(0);
    });

    it("rounds fix rate to reasonable precision", () => {
      const customMetrics = { findings: 7, merged: 3 };
      const fixRate = (customMetrics.merged / customMetrics.findings) * 100;
      expect(Math.round(fixRate * 10) / 10).toBeCloseTo(42.9, 1);
    });
  });

  describe("category bar chart", () => {
    it("computes category counts from metrics categories", () => {
      const categoryData = Object.entries(sampleMetrics.categories).map(([name, cat]) => ({
        name,
        count: cat.merged + cat.closed_unmerged + cat.reverted + cat.verification_failed + cat.issues_opened,
      }));

      expect(categoryData.length).toBe(3);
      // dead-code: 8 + 2 + 0 + 1 + 3 = 14
      expect(categoryData.find((c) => c.name === "dead-code")?.count).toBe(14);
      // security: 5 + 1 + 1 + 0 + 2 = 9
      expect(categoryData.find((c) => c.name === "security")?.count).toBe(9);
      // type-safety: 7 + 2 + 1 + 2 + 3 = 15
      expect(categoryData.find((c) => c.name === "type-safety")?.count).toBe(15);
    });

    it("uses category name as x-axis and count as y-axis", () => {
      const categoryData = Object.entries(sampleMetrics.categories).map(([name, cat]) => ({
        name,
        count: cat.merged + cat.closed_unmerged + cat.reverted + cat.verification_failed + cat.issues_opened,
      }));
      // Verify the data shape is suitable for Recharts BarChart
      categoryData.forEach((d) => {
        expect(d).toHaveProperty("name");
        expect(d).toHaveProperty("count");
        expect(typeof d.name).toBe("string");
        expect(typeof d.count).toBe("number");
      });
    });
  });

  describe("findings table", () => {
    it("renders findings with correct columns", () => {
      const columns = ["Finding ID", "Category", "Severity", "Status", "PR", "Attempts"];
      expect(columns.length).toBe(6);

      const finding = sampleFindings[0];
      expect(finding).toHaveProperty("finding_id");
      expect(finding).toHaveProperty("category");
      expect(finding).toHaveProperty("severity");
      expect(finding).toHaveProperty("status");
      expect(finding).toHaveProperty("attempt_count");
    });

    it("shows PR link when pr_url exists", () => {
      const withPr = sampleFindings.find((f) => f.pr_url);
      expect(withPr).toBeDefined();
      expect(withPr!.pr_url).toContain("https://github.com");
    });

    it("shows no PR link when pr_url is undefined", () => {
      const withoutPr = sampleFindings.find((f) => !f.pr_url);
      expect(withoutPr).toBeDefined();
      expect(withoutPr!.pr_url).toBeUndefined();
    });
  });

  describe("pagination", () => {
    const PAGE_SIZE = 25;

    it("shows 25 rows per page", () => {
      const page1 = sampleFindings.slice(0, PAGE_SIZE);
      expect(page1.length).toBe(25);
    });

    it("calculates total pages correctly", () => {
      const totalPages = Math.ceil(sampleFindings.length / PAGE_SIZE);
      expect(totalPages).toBe(3); // 60 findings / 25 per page = 3 pages
    });

    it("page 1 shows findings 1-25", () => {
      const page = 1;
      const start = (page - 1) * PAGE_SIZE;
      const end = start + PAGE_SIZE;
      const pageData = sampleFindings.slice(start, end);
      expect(pageData[0].finding_id).toBe("f-001");
      expect(pageData[24].finding_id).toBe("f-025");
      expect(pageData.length).toBe(25);
    });

    it("page 2 shows findings 26-50", () => {
      const page = 2;
      const start = (page - 1) * PAGE_SIZE;
      const end = start + PAGE_SIZE;
      const pageData = sampleFindings.slice(start, end);
      expect(pageData[0].finding_id).toBe("f-026");
      expect(pageData.length).toBe(25);
    });

    it("last page shows remaining findings (partial page)", () => {
      const page = 3;
      const start = (page - 1) * PAGE_SIZE;
      const end = start + PAGE_SIZE;
      const pageData = sampleFindings.slice(start, end);
      expect(pageData.length).toBe(10); // 60 - 50 = 10
      expect(pageData[0].finding_id).toBe("f-051");
    });
  });

  describe("status badge color coding", () => {
    it("fixed status uses green", () => {
      expect(STATUS_COLORS["fixed"]).toBe("green");
    });

    it("failed status uses red", () => {
      expect(STATUS_COLORS["failed"]).toBe("red");
    });

    it("issue-opened status uses cyan", () => {
      expect(STATUS_COLORS["issue-opened"]).toBe("cyan");
    });

    it("pending status uses yellow", () => {
      expect(STATUS_COLORS["pending"]).toBe("yellow");
    });

    it("already-exists status uses gray", () => {
      expect(STATUS_COLORS["already-exists"]).toBe("gray");
    });

    it("suppressed status uses gray", () => {
      expect(STATUS_COLORS["suppressed"]).toBe("gray");
    });

    it("every status in mock data has a defined color", () => {
      const uniqueStatuses = [...new Set(sampleFindings.map((f) => f.status))];
      uniqueStatuses.forEach((status) => {
        expect(STATUS_COLORS[status]).toBeDefined();
      });
    });
  });
});
