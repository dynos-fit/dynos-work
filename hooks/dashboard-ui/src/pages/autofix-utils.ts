/**
 * Pure utility functions for the Autofix page.
 *
 * Extracted to enable unit testing without DOM/React dependencies.
 * Consumed by both Autofix.tsx and autofix-logic.test.ts.
 */
import type { ProactiveFinding } from "@/data/types";

// ---------------------------------------------------------------------------
// AC 5: Resolution Rate computation
// ---------------------------------------------------------------------------

/**
 * Computes the resolution rate as a raw percentage.
 * Formula: (merged + issues_opened) / (findings - suppression_count) * 100
 * Returns 0 when the denominator is zero.
 */
export function computeResolutionRate(totals: {
  findings: number;
  merged: number;
  issues_opened: number;
  suppression_count: number;
}): number {
  const denominator = totals.findings - totals.suppression_count;
  if (denominator <= 0) return 0;
  return ((totals.merged + totals.issues_opened) / denominator) * 100;
}

// ---------------------------------------------------------------------------
// AC 13: Resolution Rate Trend (weekly buckets)
// ---------------------------------------------------------------------------

/** Compute the Monday-based ISO week key for a given date string. */
function weekKey(dateStr: string): string | null {
  if (!dateStr) return null;
  const date = new Date(dateStr);
  if (isNaN(date.getTime())) return null;
  const day = date.getDay();
  const diff = date.getDate() - day + (day === 0 ? -6 : 1);
  const weekStart = new Date(date);
  weekStart.setDate(diff);
  return weekStart.toISOString().slice(0, 10);
}

/**
 * Derives weekly resolution rate trend data from findings.
 *
 * Rate formula per bucket:
 *   (merged_count + issue_opened_count) / (total_in_bucket - suppressed_count) * 100
 *
 * "merged" = pr_state === "merged" || merged_at truthy
 * "issue_opened" = status === "issue-opened" || issue_number truthy
 * "suppressed" = suppressed_until truthy AND not resolved
 * Zero denominator = rate 0.
 */
export function deriveResolutionRateTrend(
  findings: ProactiveFinding[],
): Array<{ week: string; rate: number }> {
  if (findings.length === 0) return [];

  const buckets = new Map<
    string,
    { total: number; merged: number; issueOpened: number; suppressed: number }
  >();

  for (const f of findings) {
    const dateStr = f.processed_at ?? f.found_at;
    const key = weekKey(dateStr);
    if (!key) continue;

    const bucket = buckets.get(key) ?? { total: 0, merged: 0, issueOpened: 0, suppressed: 0 };
    bucket.total++;

    const isMerged = f.pr_state === "merged" || Boolean(f.merged_at);
    const isIssueOpened = f.status === "issue-opened" || Boolean(f.issue_number);
    const resolved = isMerged || isIssueOpened;

    if (isMerged) {
      bucket.merged++;
    } else if (isIssueOpened) {
      bucket.issueOpened++;
    }

    // Suppressed only if suppressed_until is truthy AND not resolved
    if (f.suppressed_until && !resolved) {
      bucket.suppressed++;
    }

    buckets.set(key, bucket);
  }

  return Array.from(buckets.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([week, { total, merged, issueOpened, suppressed }]) => {
      const denominator = total - suppressed;
      const rate = denominator > 0
        ? Math.round(((merged + issueOpened) / denominator) * 1000) / 10
        : 0;
      return { week, rate };
    });
}

// ---------------------------------------------------------------------------
// AC 15-18: Multi-dimension filter
// ---------------------------------------------------------------------------

export interface FindingsFilter {
  status: string[];
  category: string[];
  severity: string[];
  suppression: "show" | "hide" | "only";
  prState: string[];
}

/** Determines whether a finding is considered "suppressed". */
function isSuppressed(f: ProactiveFinding): boolean {
  return Boolean(f.suppressed_until) || f.status === "suppressed";
}

/**
 * Applies multi-dimension filters to a findings array.
 *
 * - status, category, severity, prState: multi-select (OR within, AND across)
 * - suppression: "show" (include all), "hide" (exclude suppressed), "only" (only suppressed)
 * - Empty arrays = no filter on that dimension.
 */
export function applyFindingsFilter(
  findings: ProactiveFinding[],
  filters: FindingsFilter,
): ProactiveFinding[] {
  return findings.filter((f) => {
    // Suppression filter
    const suppressed = isSuppressed(f);
    if (filters.suppression === "hide" && suppressed) return false;
    if (filters.suppression === "only" && !suppressed) return false;

    // Status filter
    if (filters.status.length > 0 && !filters.status.includes(f.status)) return false;

    // Category filter
    if (filters.category.length > 0 && !filters.category.includes(f.category)) return false;

    // Severity filter
    if (filters.severity.length > 0 && !filters.severity.includes(f.severity)) return false;

    // PR state filter
    if (filters.prState.length > 0 && (!f.pr_state || !filters.prState.includes(f.pr_state))) return false;

    return true;
  });
}
