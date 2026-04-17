/**
 * Autofix page — /autofix
 *
 * Displays autofix metrics (8 cards in 2 rows), filter bar, route posture,
 * recent PRs, category bar chart, resolution rate trend, and enriched findings
 * table with client-side pagination.
 */
import { useState, useMemo, useCallback } from "react";
import { motion } from "motion/react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  Cell,
  AreaChart,
  Area,
} from "recharts";
import {
  Bug,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  ExternalLink,
  GitPullRequest,
  ShieldOff,
  CircleSlash,
  FileWarning,
  BarChart3,
  Filter,
  X,
} from "lucide-react";
import { usePollingData } from "@/data/hooks";
import type { ProactiveFinding, AutofixMetrics, RecentPR, AutofixCategoryStats } from "@/data/types";
import {
  Table,
  TableHeader,
  TableBody,
  TableHead,
  TableRow,
  TableCell,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { TimeRangeFilter, filterByTimeRange } from "@/components/TimeRangeFilter";
import type { TimeRange } from "@/components/TimeRangeFilter";
import { ChartCard } from "@/components/ChartCard";
import {
  computeResolutionRate,
  deriveResolutionRateTrend,
  applyFindingsFilter,
} from "./autofix-utils";
import type { FindingsFilter } from "./autofix-utils";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PAGE_SIZE = 25;

const CARD_BASE =
  "border border-white/5 bg-[#0F1114]/60 backdrop-blur-md p-6 rounded-xl";

const GRADIENT_PALETTE = [
  "#BDF000",
  "#2DD4A8",
  "#B47AFF",
  "#FF6D00",
  "#F50057",
  "#64FFDA",
  "#EEFF41",
  "#448AFF",
];

/** Maps a status string to a Tailwind color class for badges. */
const STATUS_COLOR_MAP: Record<string, string> = {
  fixed: "bg-green-500/20 text-green-400 border-green-500/30",
  merged: "bg-green-500/20 text-green-400 border-green-500/30",
  failed: "bg-red-500/20 text-red-400 border-red-500/30",
  "issue-opened": "bg-cyan-500/20 text-cyan-400 border-cyan-500/30",
  pending: "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
  new: "bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
  "already-exists": "bg-gray-500/20 text-gray-400 border-gray-500/30",
  suppressed: "bg-gray-500/20 text-gray-400 border-gray-500/30",
  open: "bg-blue-500/20 text-blue-400 border-blue-500/30",
  closed: "bg-red-500/20 text-red-400 border-red-500/30",
  clean: "bg-green-500/20 text-green-400 border-green-500/30",
  reverted: "bg-orange-500/20 text-orange-400 border-orange-500/30",
};

const DEFAULT_STATUS_COLOR = "bg-gray-500/20 text-gray-400 border-gray-500/30";

/** Maps severity to a small colored dot indicator. */
const SEVERITY_DOT_COLOR: Record<string, string> = {
  critical: "bg-red-500",
  high: "bg-orange-500",
  medium: "bg-yellow-500",
  low: "bg-green-500",
  info: "bg-blue-400",
};

/** Validates that a URL uses http: or https: protocol. */
function isSafeUrl(url: string): boolean {
  try {
    return ["https:", "http:"].includes(new URL(url).protocol);
  } catch {
    return false;
  }
}

/** PR timeline stage definitions with colors and labels. */
const PR_TIMELINE_STAGES = [
  { key: "created", color: "#EAB308", label: "Created" },
  { key: "reviewed", color: "#22D3EE", label: "Reviewed" },
  { key: "merged", color: "#22C55E", label: "Merged" },
  { key: "closed", color: "#EF4444", label: "Closed" },
] as const;

const DEFAULT_FILTERS: FindingsFilter = {
  status: [],
  category: [],
  severity: [],
  suppression: "show",
  prState: [],
};

const SUPPRESSION_OPTIONS = ["show", "hide", "only"] as const;

// ---------------------------------------------------------------------------
// Metric card definitions (AC 4, 5, 6, 7)
// ---------------------------------------------------------------------------

interface MetricCardDef {
  label: string;
  getValue: (metrics: AutofixMetrics) => string;
  icon: React.ElementType;
  accent: string;
}

const METRIC_CARDS_ROW1: MetricCardDef[] = [
  {
    label: "Findings",
    getValue: (m) => String(m.totals.findings),
    icon: Bug,
    accent: "text-[#BDF000]",
  },
  {
    label: "Resolution Rate",
    getValue: (m) => {
      const rate = computeResolutionRate(m.totals);
      return `${Math.round(rate * 10) / 10}%`;
    },
    icon: CheckCircle2,
    accent: "text-[#2DD4A8]",
  },
  {
    label: "Open PRs",
    getValue: (m) => `${m.rate_limits.open_prs}/${m.rate_limits.max_open_prs}`,
    icon: GitPullRequest,
    accent: "text-yellow-400",
  },
  {
    label: "PRs Today",
    getValue: (m) => `${m.rate_limits.prs_today}/${m.rate_limits.max_prs_per_day}`,
    icon: BarChart3,
    accent: "text-blue-400",
  },
];

const METRIC_CARDS_ROW2: MetricCardDef[] = [
  {
    label: "Issues Opened",
    getValue: (m) => String(m.totals.issues_opened),
    icon: AlertTriangle,
    accent: "text-cyan-400",
  },
  {
    label: "Suppressed",
    getValue: (m) => String(m.totals.suppression_count),
    icon: ShieldOff,
    accent: "text-gray-400",
  },
  {
    label: "Verification Failed",
    getValue: (m) => {
      const total = Object.values(m.categories).reduce(
        (sum, cat) => sum + (cat.verification_failed ?? 0),
        0,
      );
      return String(total);
    },
    icon: FileWarning,
    accent: "text-red-400",
  },
  {
    label: "Closed/Reverted",
    getValue: (m) => String(m.totals.closed_unmerged + m.totals.reverted),
    icon: CircleSlash,
    accent: "text-orange-400",
  },
];

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function MetricCardSkeleton() {
  return (
    <div className={CARD_BASE} aria-hidden="true">
      <Skeleton className="h-4 w-24 mb-3" />
      <Skeleton className="h-8 w-16" />
    </div>
  );
}

function MetricCard({ def, metrics }: { def: MetricCardDef; metrics: AutofixMetrics }) {
  const Icon = def.icon;
  return (
    <motion.div
      className={`${CARD_BASE} card-hover-glow`}
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
    >
      <div className="flex items-center gap-2 mb-2">
        <Icon className={`w-4 h-4 ${def.accent}`} aria-hidden="true" />
        <span className="text-xs font-mono text-slate-400 uppercase tracking-wider">
          {def.label}
        </span>
      </div>
      <p className={`text-2xl font-mono font-bold ${def.accent}`}>
        {def.getValue(metrics)}
      </p>
    </motion.div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colorClass = STATUS_COLOR_MAP[status] ?? DEFAULT_STATUS_COLOR;
  return (
    <Badge variant="outline" className={`${colorClass} rounded-full px-2.5 py-0.5 text-[10px] font-medium font-mono uppercase`}>
      {status}
    </Badge>
  );
}

/** Small colored dot indicating severity level. */
function SeverityDot({ severity }: { severity: string }) {
  const dotColor = SEVERITY_DOT_COLOR[severity.toLowerCase()] ?? "bg-gray-500";
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className={`inline-block w-2 h-2 rounded-full flex-shrink-0 ${dotColor}`}
        aria-hidden="true"
      />
      <span>{severity}</span>
    </span>
  );
}

function ChartSkeleton() {
  return (
    <div className={`${CARD_BASE} h-72`} aria-hidden="true">
      <Skeleton className="h-5 w-48 mb-4" />
      <Skeleton className="h-52 w-full" />
    </div>
  );
}

/** Mini PR timeline showing colored dots connected by a thin line. */
function PrTimeline({ finding }: { finding: ProactiveFinding }) {
  if (!finding.pr_url) return null;

  const stages: { active: boolean; color: string; label: string; timestamp: string | null }[] = [
    {
      active: Boolean(finding.found_at),
      color: PR_TIMELINE_STAGES[0].color,
      label: PR_TIMELINE_STAGES[0].label,
      timestamp: finding.found_at ?? null,
    },
    {
      active: Boolean(finding.processed_at),
      color: PR_TIMELINE_STAGES[1].color,
      label: PR_TIMELINE_STAGES[1].label,
      timestamp: finding.processed_at ?? null,
    },
    {
      active: finding.pr_state === "merged" || Boolean(finding.merged_at),
      color: PR_TIMELINE_STAGES[2].color,
      label: PR_TIMELINE_STAGES[2].label,
      timestamp: finding.merged_at ?? null,
    },
    {
      active: finding.pr_state === "closed" && !finding.merged_at,
      color: PR_TIMELINE_STAGES[3].color,
      label: PR_TIMELINE_STAGES[3].label,
      timestamp: finding.processed_at ?? null,
    },
  ];

  return (
    <div className="flex items-center gap-0" role="img" aria-label={`PR timeline: ${stages.filter((s) => s.active).map((s) => s.label).join(", ")}`}>
      {stages.map((stage, idx) => (
        <div key={stage.label} className="flex items-center">
          {idx > 0 && (
            <div
              className="w-3 h-[2px]"
              style={{ backgroundColor: stage.active ? stage.color : "#334155" }}
            />
          )}
          <div
            className="w-2 h-2 rounded-full flex-shrink-0"
            style={{ backgroundColor: stage.active ? stage.color : "#334155" }}
            title={
              stage.active && stage.timestamp
                ? `${stage.label}: ${new Date(stage.timestamp).toLocaleString()}`
                : stage.label
            }
            aria-hidden="true"
          />
        </div>
      ))}
    </div>
  );
}

function TableSkeleton() {
  return (
    <div className={CARD_BASE} aria-hidden="true">
      <Skeleton className="h-5 w-40 mb-4" />
      {Array.from({ length: 5 }).map((_, i) => (
        <Skeleton key={i} className="h-8 w-full mb-2" />
      ))}
    </div>
  );
}

function RoutePostureSkeleton() {
  return (
    <div className={CARD_BASE} aria-hidden="true">
      <Skeleton className="h-5 w-36 mb-4" />
      {Array.from({ length: 3 }).map((_, i) => (
        <Skeleton key={i} className="h-8 w-full mb-2" />
      ))}
    </div>
  );
}

function RecentPRsSkeleton() {
  return (
    <div className={CARD_BASE} aria-hidden="true">
      <Skeleton className="h-5 w-32 mb-4" />
      {Array.from({ length: 3 }).map((_, i) => (
        <Skeleton key={i} className="h-8 w-full mb-2" />
      ))}
    </div>
  );
}

/** Multi-select toggle button for filter bar. */
function FilterToggle({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`px-2.5 py-1 text-[10px] font-medium tracking-wider uppercase rounded-full transition-all duration-150 font-mono ${
        active
          ? "bg-[#BDF000]/20 text-[#BDF000] border border-[#BDF000]/30"
          : "bg-[#2A2A2A] text-[#7A776E] hover:text-[#C8C4B8] hover:bg-[#333] border border-transparent"
      }`}
      aria-pressed={active}
      aria-label={`Filter by ${label}`}
    >
      {label}
    </button>
  );
}

/** Truncated text with tooltip for long content. */
function TruncatedCell({ text, maxLen = 60 }: { text: string; maxLen?: number }) {
  if (text.length <= maxLen) {
    return <span>{text}</span>;
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="cursor-help">{text.slice(0, maxLen)}...</span>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-sm break-words">
        {text}
      </TooltipContent>
    </Tooltip>
  );
}

// ---------------------------------------------------------------------------
// Filter Bar Component (AC 15, 16, 17)
// ---------------------------------------------------------------------------

function FilterBar({
  filters,
  setFilters,
  timeRange,
  setTimeRange,
  availableStatuses,
  availableCategories,
  availableSeverities,
  availablePrStates,
}: {
  filters: FindingsFilter;
  setFilters: (fn: (prev: FindingsFilter) => FindingsFilter) => void;
  timeRange: TimeRange;
  setTimeRange: (range: TimeRange) => void;
  availableStatuses: string[];
  availableCategories: string[];
  availableSeverities: string[];
  availablePrStates: string[];
}) {
  const hasActiveFilters =
    filters.status.length > 0 ||
    filters.category.length > 0 ||
    filters.severity.length > 0 ||
    filters.suppression !== "show" ||
    filters.prState.length > 0;

  const toggleFilter = useCallback(
    (dimension: "status" | "category" | "severity" | "prState", value: string) => {
      setFilters((prev) => {
        const current = prev[dimension];
        const next = current.includes(value)
          ? current.filter((v) => v !== value)
          : [...current, value];
        return { ...prev, [dimension]: next };
      });
    },
    [setFilters],
  );

  const clearFilters = useCallback(() => {
    setFilters(() => DEFAULT_FILTERS);
  }, [setFilters]);

  return (
    <motion.div
      className={`${CARD_BASE} space-y-3`}
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Filter className="w-4 h-4 text-slate-400" aria-hidden="true" />
          <span className="text-xs font-mono text-slate-400 uppercase tracking-wider">
            Filters
          </span>
        </div>
        <div className="flex items-center gap-3">
          <TimeRangeFilter value={timeRange} onChange={setTimeRange} />
          {hasActiveFilters && (
            <button
              onClick={clearFilters}
              className="flex items-center gap-1 px-2.5 py-1 text-[10px] font-mono text-red-400 hover:text-red-300 bg-red-500/10 hover:bg-red-500/20 rounded-full transition-colors"
              aria-label="Clear all filters"
            >
              <X className="w-3 h-3" aria-hidden="true" />
              Clear
            </button>
          )}
        </div>
      </div>

      {/* Status */}
      {availableStatuses.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-mono text-slate-500 uppercase w-16 flex-shrink-0">Status</span>
          {availableStatuses.map((s) => (
            <FilterToggle
              key={s}
              label={s}
              active={filters.status.includes(s)}
              onClick={() => toggleFilter("status", s)}
            />
          ))}
        </div>
      )}

      {/* Category */}
      {availableCategories.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-mono text-slate-500 uppercase w-16 flex-shrink-0">Category</span>
          {availableCategories.map((c) => (
            <FilterToggle
              key={c}
              label={c}
              active={filters.category.includes(c)}
              onClick={() => toggleFilter("category", c)}
            />
          ))}
        </div>
      )}

      {/* Severity */}
      {availableSeverities.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-mono text-slate-500 uppercase w-16 flex-shrink-0">Severity</span>
          {availableSeverities.map((s) => (
            <FilterToggle
              key={s}
              label={s}
              active={filters.severity.includes(s)}
              onClick={() => toggleFilter("severity", s)}
            />
          ))}
        </div>
      )}

      {/* Suppression */}
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-[10px] font-mono text-slate-500 uppercase w-16 flex-shrink-0">Suppressed</span>
        {SUPPRESSION_OPTIONS.map((opt) => (
          <FilterToggle
            key={opt}
            label={opt}
            active={filters.suppression === opt}
            onClick={() =>
              setFilters((prev) => ({ ...prev, suppression: opt }))
            }
          />
        ))}
      </div>

      {/* PR State */}
      {availablePrStates.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-mono text-slate-500 uppercase w-16 flex-shrink-0">PR State</span>
          {availablePrStates.map((s) => (
            <FilterToggle
              key={s}
              label={s}
              active={filters.prState.includes(s)}
              onClick={() => toggleFilter("prState", s)}
            />
          ))}
        </div>
      )}
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Route Posture Table (AC 9, 10)
// ---------------------------------------------------------------------------

function RoutePostureTable({
  categories,
  categoryFilter,
}: {
  categories: Record<string, AutofixCategoryStats>;
  categoryFilter: string[];
}) {
  const entries = useMemo(() => {
    return Object.entries(categories).filter(
      ([name]) => categoryFilter.length === 0 || categoryFilter.includes(name),
    );
  }, [categories, categoryFilter]);

  if (entries.length === 0) {
    return (
      <motion.div
        className={CARD_BASE}
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3 }}
      >
        <h2 className="text-sm font-mono font-semibold text-slate-300 uppercase tracking-wider mb-4">
          Route Posture
        </h2>
        <div className="flex flex-col items-center justify-center py-10 gap-2" role="status">
          <BarChart3 className="w-8 h-8 text-slate-600" aria-hidden="true" />
          <p className="text-sm font-mono text-slate-500 text-center">
            No route posture data available
          </p>
        </div>
      </motion.div>
    );
  }

  return (
    <motion.div
      className={CARD_BASE}
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
    >
      <h2 className="text-sm font-mono font-semibold text-slate-300 uppercase tracking-wider mb-4">
        Route Posture
      </h2>
      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow className="border-white/5">
              <TableHead className="text-slate-400 font-mono text-xs">Category</TableHead>
              <TableHead className="text-slate-400 font-mono text-xs">Mode</TableHead>
              <TableHead className="text-slate-400 font-mono text-xs">Enabled</TableHead>
              <TableHead className="text-slate-400 font-mono text-xs">Confidence</TableHead>
              <TableHead className="text-slate-400 font-mono text-xs">Merged</TableHead>
              <TableHead className="text-slate-400 font-mono text-xs">Closed Unmerged</TableHead>
              <TableHead className="text-slate-400 font-mono text-xs">Reverted</TableHead>
              <TableHead className="text-slate-400 font-mono text-xs">Issues Opened</TableHead>
              <TableHead className="text-slate-400 font-mono text-xs">Verification Failed</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {entries.map(([name, cat], idx) => (
              <TableRow
                key={name}
                className={`border-white/5 transition-colors hover:bg-white/[0.04] ${idx % 2 === 0 ? "bg-white/[0.02]" : ""}`}
              >
                <TableCell className="font-mono text-xs text-slate-300 max-w-[150px] truncate">
                  {name}
                </TableCell>
                <TableCell className="font-mono text-xs text-slate-400">
                  <StatusBadge status={cat.mode} />
                </TableCell>
                <TableCell>
                  <Badge
                    variant="outline"
                    className={`rounded-full px-2.5 py-0.5 text-[10px] font-medium font-mono uppercase ${
                      cat.enabled
                        ? "bg-green-500/20 text-green-400 border-green-500/30"
                        : "bg-red-500/20 text-red-400 border-red-500/30"
                    }`}
                  >
                    {cat.enabled ? "yes" : "no"}
                  </Badge>
                </TableCell>
                <TableCell className="font-mono text-xs text-slate-400">
                  {cat.confidence.toFixed(2)}
                </TableCell>
                <TableCell className="font-mono text-xs text-slate-400 text-center">
                  {cat.merged}
                </TableCell>
                <TableCell className="font-mono text-xs text-slate-400 text-center">
                  {cat.closed_unmerged}
                </TableCell>
                <TableCell className="font-mono text-xs text-slate-400 text-center">
                  {cat.reverted}
                </TableCell>
                <TableCell className="font-mono text-xs text-slate-400 text-center">
                  {cat.issues_opened}
                </TableCell>
                <TableCell className="font-mono text-xs text-slate-400 text-center">
                  {cat.verification_failed}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Recent PRs Section (AC 22, 23, 24)
// ---------------------------------------------------------------------------

function RecentPRsSection({
  recentPrs,
  categoryFilter,
}: {
  recentPrs: RecentPR[];
  categoryFilter: string[];
}) {
  const filtered = useMemo(() => {
    if (categoryFilter.length === 0) return recentPrs;
    return recentPrs.filter((pr) => categoryFilter.includes(pr.category));
  }, [recentPrs, categoryFilter]);

  return (
    <motion.div
      className={CARD_BASE}
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
    >
      <h2 className="text-sm font-mono font-semibold text-slate-300 uppercase tracking-wider mb-4">
        Recent PRs
      </h2>

      {filtered.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-10 gap-2" role="status">
          <GitPullRequest className="w-8 h-8 text-slate-600" aria-hidden="true" />
          <p className="text-sm font-mono text-slate-500 text-center">
            No recent PR activity
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow className="border-white/5">
                <TableHead className="text-slate-400 font-mono text-xs">PR</TableHead>
                <TableHead className="text-slate-400 font-mono text-xs">Category</TableHead>
                <TableHead className="text-slate-400 font-mono text-xs">State</TableHead>
                <TableHead className="text-slate-400 font-mono text-xs">Merge Outcome</TableHead>
                <TableHead className="text-slate-400 font-mono text-xs">Title</TableHead>
                <TableHead className="text-slate-400 font-mono text-xs">Branch</TableHead>
                <TableHead className="text-slate-400 font-mono text-xs">Created</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((pr, idx) => (
                <TableRow
                  key={`${pr.finding_id}-${pr.number ?? idx}`}
                  className={`border-white/5 transition-colors hover:bg-white/[0.04] ${idx % 2 === 0 ? "bg-white/[0.02]" : ""}`}
                >
                  <TableCell>
                    {pr.url && pr.number != null && isSafeUrl(pr.url) ? (
                      <a
                        href={pr.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-[#BDF000] hover:text-[#BDF000]/80 transition-colors font-mono text-xs"
                        aria-label={`Open pull request #${pr.number}`}
                      >
                        #{pr.number}
                        <ExternalLink className="w-3 h-3" aria-hidden="true" />
                      </a>
                    ) : (
                      <span className="text-slate-600 font-mono text-xs" aria-label="No PR number">
                        --
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-slate-400 max-w-[120px] truncate">
                    {pr.category}
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={pr.state} />
                  </TableCell>
                  <TableCell>
                    {pr.merge_outcome ? (
                      <StatusBadge status={pr.merge_outcome} />
                    ) : (
                      <span className="text-slate-600 font-mono text-xs">--</span>
                    )}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-slate-300 max-w-[200px]">
                    <TruncatedCell text={pr.title} maxLen={50} />
                  </TableCell>
                  <TableCell className="font-mono text-xs text-slate-400 max-w-[150px] truncate">
                    {pr.branch ?? "--"}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-slate-400 whitespace-nowrap">
                    {pr.created_at
                      ? new Date(pr.created_at).toLocaleDateString()
                      : "--"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Main page component
// ---------------------------------------------------------------------------

export default function Autofix() {
  const {
    data: metrics,
    loading: metricsLoading,
    error: metricsError,
    refetch: refetchMetrics,
  } = usePollingData<AutofixMetrics>("/api/autofix-metrics");

  const {
    data: findings,
    loading: findingsLoading,
    error: findingsError,
    refetch: refetchFindings,
  } = usePollingData<ProactiveFinding[]>("/api/findings");

  const [page, setPage] = useState(1);
  const [timeRange, setTimeRange] = useState<TimeRange>("All");
  const [filters, setFilters] = useState<FindingsFilter>(DEFAULT_FILTERS);

  // Reset page when filters change
  const updateFilters = useCallback(
    (fn: (prev: FindingsFilter) => FindingsFilter) => {
      setFilters((prev) => {
        const next = fn(prev);
        setPage(1);
        return next;
      });
    },
    [],
  );

  // Derive available filter options from data + currently selected (AC: survive polling refreshes)
  const availableStatuses = useMemo(() => {
    const fromData = new Set(findings?.map((f) => f.status) ?? []);
    for (const s of filters.status) fromData.add(s);
    return Array.from(fromData).sort();
  }, [findings, filters.status]);

  const availableCategories = useMemo(() => {
    const fromData = new Set(findings?.map((f) => f.category) ?? []);
    if (metrics?.categories) {
      for (const k of Object.keys(metrics.categories)) fromData.add(k);
    }
    for (const c of filters.category) fromData.add(c);
    return Array.from(fromData).sort();
  }, [findings, metrics, filters.category]);

  const availableSeverities = useMemo(() => {
    const fromData = new Set(findings?.map((f) => f.severity) ?? []);
    for (const s of filters.severity) fromData.add(s);
    return Array.from(fromData).sort();
  }, [findings, filters.severity]);

  const availablePrStates = useMemo(() => {
    const fromData = new Set(
      findings?.map((f) => f.pr_state).filter((s): s is string => Boolean(s)) ?? [],
    );
    for (const s of filters.prState) fromData.add(s);
    return Array.from(fromData).sort();
  }, [findings, filters.prState]);

  // Apply multi-dimension filter to findings
  const filteredByDimension = useMemo(() => {
    if (!findings) return [];
    return applyFindingsFilter(findings, filters);
  }, [findings, filters]);

  // Time-range filtered findings for charts (applied on top of dimension filter)
  const filteredFindings = useMemo(() => {
    return filterByTimeRange(filteredByDimension, (f) => f.found_at, timeRange);
  }, [filteredByDimension, timeRange]);

  // Derive category chart data from filtered findings (AC 11: no fallback path)
  const categoryData = useMemo(() => {
    const counts = new Map<string, number>();
    for (const f of filteredFindings) {
      counts.set(f.category, (counts.get(f.category) ?? 0) + 1);
    }
    return Array.from(counts.entries()).map(([name, count]) => ({ name, count }));
  }, [filteredFindings]);

  // Derive resolution rate trend from filtered findings (AC 13, 14)
  const resolutionRateTrend = useMemo(() => {
    if (filteredFindings.length === 0) return [];
    return deriveResolutionRateTrend(filteredFindings);
  }, [filteredFindings]);

  // Pagination on filtered findings (AC 18)
  const totalFilteredFindings = filteredByDimension.length;
  const totalPages = Math.max(1, Math.ceil(totalFilteredFindings / PAGE_SIZE));
  const clampedPage = Math.min(page, totalPages);
  const paginatedFindings = useMemo(() => {
    const start = (clampedPage - 1) * PAGE_SIZE;
    return filteredByDimension.slice(start, start + PAGE_SIZE);
  }, [filteredByDimension, clampedPage]);

  // Check if any dimension filters are active (for showing filter-specific empty state)
  const hasActiveFilters =
    filters.status.length > 0 ||
    filters.category.length > 0 ||
    filters.severity.length > 0 ||
    filters.suppression !== "show" ||
    filters.prState.length > 0;

  const isLoading = metricsLoading || findingsLoading;
  const hasError = metricsError || findingsError;

  // ---------------------------------------------------------------------------
  // Loading state (AC 25)
  // ---------------------------------------------------------------------------
  if (isLoading) {
    return (
      <div className="p-4 sm:p-6 space-y-6" aria-busy="true" aria-label="Loading autofix data">
        <h1 className="text-lg font-mono font-semibold text-[#BDF000] tracking-wider uppercase">
          Autofix
        </h1>
        {/* 8 card skeletons: 2 rows of 4 */}
        <div className="grid grid-cols-2 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <MetricCardSkeleton key={`r1-${i}`} />
          ))}
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <MetricCardSkeleton key={`r2-${i}`} />
          ))}
        </div>
        <RoutePostureSkeleton />
        <RecentPRsSkeleton />
        <ChartSkeleton />
        <ChartSkeleton />
        <TableSkeleton />
      </div>
    );
  }

  // ---------------------------------------------------------------------------
  // Error state
  // ---------------------------------------------------------------------------
  if (hasError && !metrics && !findings) {
    return (
      <div className="p-4 sm:p-6 space-y-6">
        <h1 className="text-lg font-mono font-semibold text-[#BDF000] tracking-wider uppercase">
          Autofix
        </h1>
        <div
          className={`${CARD_BASE} flex flex-col items-center justify-center py-16 gap-4`}
          role="alert"
        >
          <XCircle className="w-10 h-10 text-red-400" aria-hidden="true" />
          <p className="text-sm font-mono text-slate-400 text-center max-w-md">
            Unable to load autofix data. Please check that the daemon is running and try again.
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              refetchMetrics();
              refetchFindings();
            }}
            aria-label="Retry loading autofix data"
          >
            Retry
          </Button>
        </div>
      </div>
    );
  }

  // ---------------------------------------------------------------------------
  // Success / Empty states
  // ---------------------------------------------------------------------------
  return (
    <div className="p-4 sm:p-6 space-y-6">
      <h1 className="text-lg font-mono font-semibold text-[#BDF000] tracking-wider uppercase">
        Autofix
      </h1>

      {/* ---- Metric Cards Row 1 (AC 4, 6, 7: unfiltered, system-wide) ---- */}
      {metrics && (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {METRIC_CARDS_ROW1.map((def) => (
              <MetricCard key={def.label} def={def} metrics={metrics} />
            ))}
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {METRIC_CARDS_ROW2.map((def) => (
              <MetricCard key={def.label} def={def} metrics={metrics} />
            ))}
          </div>
        </>
      )}

      {/* ---- Filter Bar (AC 15, 16, 17) ---- */}
      <FilterBar
        filters={filters}
        setFilters={updateFilters}
        timeRange={timeRange}
        setTimeRange={setTimeRange}
        availableStatuses={availableStatuses}
        availableCategories={availableCategories}
        availableSeverities={availableSeverities}
        availablePrStates={availablePrStates}
      />

      {/* ---- Route Posture (AC 9, 10) ---- */}
      {metrics && (
        <RoutePostureTable
          categories={metrics.categories}
          categoryFilter={filters.category}
        />
      )}

      {/* ---- Recent PRs (AC 22, 23, 24) ---- */}
      {metrics && (
        <RecentPRsSection
          recentPrs={metrics.recent_prs ?? []}
          categoryFilter={filters.category}
        />
      )}

      {/* ---- Category Bar Chart (AC 11) ---- */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3, delay: 0.1 }}
      >
        <ChartCard title="Category Breakdown">
          {categoryData.length > 0 ? (
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={categoryData} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#333" vertical={false} />
                  <XAxis
                    dataKey="name"
                    tick={{ fill: "#999", fontFamily: "'JetBrains Mono', monospace", fontSize: 11 }}
                    axisLine={{ stroke: "#333" }}
                    tickLine={false}
                  />
                  <YAxis
                    tick={{ fill: "#999", fontFamily: "'JetBrains Mono', monospace", fontSize: 11 }}
                    axisLine={{ stroke: "#333" }}
                    tickLine={false}
                    allowDecimals={false}
                  />
                  <RechartsTooltip
                    contentStyle={{
                      background: "#0D1321",
                      border: "1px solid rgba(189, 240, 0, 0.15)",
                      borderRadius: "8px",
                      fontFamily: "'JetBrains Mono', monospace",
                      fontSize: "12px",
                      color: "#E2E8F0",
                    }}
                    cursor={{ fill: "rgba(189, 240, 0, 0.05)" }}
                  />
                  <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                    {categoryData.map((_, idx) => (
                      <Cell
                        key={idx}
                        fill={GRADIENT_PALETTE[idx % GRADIENT_PALETTE.length]}
                      />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center py-10 gap-2" role="status">
              <BarChart3 className="w-8 h-8 text-slate-600" aria-hidden="true" />
              <p className="text-sm font-mono text-slate-500 text-center">
                No category data for the current filters
              </p>
            </div>
          )}
        </ChartCard>
      </motion.div>

      {/* ---- Resolution Rate Trend (AC 12, 13, 14) ---- */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3, delay: 0.2 }}
      >
        <ChartCard title="Resolution Rate Trend">
          {resolutionRateTrend.length > 0 ? (
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={resolutionRateTrend} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
                  <defs>
                    <linearGradient id="resolutionRateGradient" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#BDF000" stopOpacity={0.3} />
                      <stop offset="95%" stopColor="#BDF000" stopOpacity={0.02} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="#333" vertical={false} />
                  <XAxis
                    dataKey="week"
                    tick={{ fill: "#999", fontFamily: "'JetBrains Mono', monospace", fontSize: 11 }}
                    axisLine={{ stroke: "#333" }}
                    tickLine={false}
                  />
                  <YAxis
                    tick={{ fill: "#999", fontFamily: "'JetBrains Mono', monospace", fontSize: 11 }}
                    axisLine={{ stroke: "#333" }}
                    tickLine={false}
                    domain={[0, 100]}
                    unit="%"
                  />
                  <RechartsTooltip
                    contentStyle={{
                      background: "#0D1321",
                      border: "1px solid rgba(189, 240, 0, 0.15)",
                      borderRadius: "8px",
                      fontFamily: "'JetBrains Mono', monospace",
                      fontSize: "12px",
                      color: "#E2E8F0",
                    }}
                    formatter={(value: number) => [`${value}%`, "Resolution Rate"]}
                    cursor={{ stroke: "rgba(189, 240, 0, 0.3)" }}
                  />
                  <Area
                    type="monotone"
                    dataKey="rate"
                    stroke="#BDF000"
                    strokeWidth={2}
                    fill="url(#resolutionRateGradient)"
                    dot={{ fill: "#BDF000", r: 3, strokeWidth: 0 }}
                    activeDot={{ fill: "#BDF000", r: 5, strokeWidth: 2, stroke: "#0F1114" }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center py-10 gap-2" role="status">
              <BarChart3 className="w-8 h-8 text-slate-600" aria-hidden="true" />
              <p className="text-sm font-mono text-slate-500 text-center">
                No trend data for the current filters
              </p>
            </div>
          )}
        </ChartCard>
      </motion.div>

      {/* ---- Findings Table (AC 19, 20, 21) ---- */}
      <motion.div
        className={CARD_BASE}
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3, delay: 0.3 }}
      >
        <h2 className="text-sm font-mono font-semibold text-slate-300 uppercase tracking-wider mb-4">
          Findings
        </h2>

        {totalFilteredFindings === 0 ? (
          /* Empty state: distinguish filter-empty from data-empty */
          hasActiveFilters ? (
            <div className="flex flex-col items-center justify-center py-16 gap-3" role="status">
              <Filter className="w-10 h-10 text-slate-600" aria-hidden="true" />
              <p className="text-sm font-mono text-slate-500 text-center">
                No findings match your filters
              </p>
              <Button
                variant="outline"
                size="sm"
                onClick={() => updateFilters(() => DEFAULT_FILTERS)}
                aria-label="Clear all filters"
              >
                Clear Filters
              </Button>
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center py-16 gap-3" role="status">
              <Bug className="w-10 h-10 text-slate-600" aria-hidden="true" />
              <p className="text-sm font-mono text-slate-500 text-center">
                No findings recorded
              </p>
              <p className="text-xs font-mono text-slate-600 text-center max-w-sm">
                When the autofix scanner detects issues in your codebase, they will appear here.
              </p>
            </div>
          )
        ) : (
          <>
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow className="border-white/5">
                    <TableHead className="text-slate-400 font-mono text-xs">Finding ID</TableHead>
                    <TableHead className="text-slate-400 font-mono text-xs">Category</TableHead>
                    <TableHead className="text-slate-400 font-mono text-xs">Severity</TableHead>
                    <TableHead className="text-slate-400 font-mono text-xs">Status</TableHead>
                    <TableHead className="text-slate-400 font-mono text-xs">Description</TableHead>
                    <TableHead className="text-slate-400 font-mono text-xs">PR</TableHead>
                    <TableHead className="text-slate-400 font-mono text-xs">Issue</TableHead>
                    <TableHead className="text-slate-400 font-mono text-xs">Timeline</TableHead>
                    <TableHead className="text-slate-400 font-mono text-xs">Attempts</TableHead>
                    <TableHead className="text-slate-400 font-mono text-xs">Fail Reason</TableHead>
                    <TableHead className="text-slate-400 font-mono text-xs">Suppression</TableHead>
                    <TableHead className="text-slate-400 font-mono text-xs">Confidence</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {paginatedFindings.map((finding, idx) => {
                    const isSuppressed =
                      Boolean(finding.suppressed_until) || finding.status === "suppressed";
                    const isIssueOnly =
                      finding.status === "issue-opened" || Boolean(finding.issue_url);
                    return (
                      <TableRow
                        key={finding.finding_id}
                        className={`border-white/5 transition-colors hover:bg-white/[0.04] ${
                          isSuppressed
                            ? "opacity-50"
                            : isIssueOnly
                              ? "bg-cyan-500/[0.03]"
                              : idx % 2 === 0
                                ? "bg-white/[0.02]"
                                : ""
                        }`}
                      >
                        <TableCell className="font-mono text-xs text-slate-300 max-w-[180px] truncate">
                          {finding.finding_id}
                        </TableCell>
                        <TableCell className="font-mono text-xs text-slate-400 max-w-[120px] truncate">
                          {finding.category}
                        </TableCell>
                        <TableCell className="font-mono text-xs text-slate-400">
                          <SeverityDot severity={finding.severity} />
                        </TableCell>
                        <TableCell>
                          <div className="flex items-center gap-1.5">
                            <StatusBadge status={finding.status} />
                            {isSuppressed && (
                              <Badge
                                variant="outline"
                                className="bg-gray-500/10 text-gray-500 border-gray-500/20 rounded-full px-1.5 py-0 text-[9px] font-mono uppercase"
                              >
                                suppressed
                              </Badge>
                            )}
                          </div>
                        </TableCell>
                        {/* AC 19: Description column */}
                        <TableCell className="font-mono text-xs text-slate-400 max-w-[200px]">
                          <TruncatedCell text={finding.description} maxLen={60} />
                        </TableCell>
                        {/* PR column */}
                        <TableCell>
                          {finding.pr_url && isSafeUrl(finding.pr_url) ? (
                            <a
                              href={finding.pr_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="inline-flex items-center gap-1 text-[#BDF000] hover:text-[#BDF000]/80 transition-colors font-mono text-xs"
                              aria-label={`Open pull request ${finding.pr_number ?? ""}`}
                            >
                              #{finding.pr_number ?? ""}
                              <ExternalLink className="w-3 h-3" aria-hidden="true" />
                            </a>
                          ) : (
                            <span className="text-slate-600 font-mono text-xs" aria-label="No pull request">
                              --
                            </span>
                          )}
                        </TableCell>
                        {/* AC 19, 20: Issue column */}
                        <TableCell>
                          {finding.issue_url && isSafeUrl(finding.issue_url) ? (
                            <a
                              href={finding.issue_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="inline-flex items-center gap-1 text-[#BDF000] hover:text-[#BDF000]/80 transition-colors font-mono text-xs"
                              aria-label={`Open issue ${finding.issue_number ?? ""}`}
                            >
                              #{finding.issue_number}
                              <ExternalLink className="w-3 h-3" aria-hidden="true" />
                            </a>
                          ) : (
                            <span className="text-slate-600 font-mono text-xs" aria-label="No issue">
                              --
                            </span>
                          )}
                        </TableCell>
                        <TableCell>
                          {finding.pr_url ? (
                            <PrTimeline finding={finding} />
                          ) : (
                            <span className="text-slate-600 font-mono text-xs" aria-label="No timeline">
                              --
                            </span>
                          )}
                        </TableCell>
                        <TableCell className="font-mono text-xs text-slate-400 text-center">
                          {finding.attempt_count}
                        </TableCell>
                        {/* AC 19: Fail Reason column */}
                        <TableCell className="font-mono text-xs text-slate-400 max-w-[150px]">
                          {finding.fail_reason ? (
                            <TruncatedCell text={finding.fail_reason} maxLen={40} />
                          ) : (
                            <span className="text-slate-600">--</span>
                          )}
                        </TableCell>
                        {/* AC 19, 21: Suppression column */}
                        <TableCell className="font-mono text-xs text-slate-400 max-w-[150px]">
                          {finding.suppression_reason || finding.suppressed_until ? (
                            <div className="space-y-0.5">
                              {finding.suppression_reason && (
                                <TruncatedCell text={finding.suppression_reason} maxLen={30} />
                              )}
                              {finding.suppressed_until && (
                                <div className="text-[10px] text-slate-500">
                                  until {new Date(finding.suppressed_until).toLocaleDateString()}
                                </div>
                              )}
                            </div>
                          ) : (
                            <span className="text-slate-600">--</span>
                          )}
                        </TableCell>
                        {/* AC 19: Confidence column */}
                        <TableCell className="font-mono text-xs text-slate-400 text-center">
                          {finding.confidence_score != null
                            ? finding.confidence_score.toFixed(2)
                            : "--"}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>

            {/* Pagination (AC 18) */}
            <div className="flex items-center justify-between mt-4 pt-4 border-t border-white/5">
              <span className="text-xs font-mono text-slate-500">
                Page {clampedPage} of {totalPages} ({totalFilteredFindings} findings)
              </span>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={clampedPage <= 1}
                  aria-label="Previous page"
                >
                  Prev
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={clampedPage >= totalPages}
                  aria-label="Next page"
                >
                  Next
                </Button>
              </div>
            </div>
          </>
        )}
      </motion.div>
    </div>
  );
}
