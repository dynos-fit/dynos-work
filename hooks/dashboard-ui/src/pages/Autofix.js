import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
/**
 * Autofix page — /autofix
 *
 * Displays autofix metrics (8 cards in 2 rows), filter bar, route posture,
 * recent PRs, category bar chart, resolution rate trend, and enriched findings
 * table with client-side pagination.
 */
import { useState, useMemo, useCallback } from "react";
import { motion } from "motion/react";
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, ResponsiveContainer, Tooltip as RechartsTooltip, Cell, AreaChart, Area, } from "recharts";
import { Bug, CheckCircle2, XCircle, AlertTriangle, ExternalLink, GitPullRequest, ShieldOff, CircleSlash, FileWarning, BarChart3, Filter, X, } from "lucide-react";
import { usePollingData } from "@/data/hooks";
import { Table, TableHeader, TableBody, TableHead, TableRow, TableCell, } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { TimeRangeFilter, filterByTimeRange } from "@/components/TimeRangeFilter";
import { ChartCard } from "@/components/ChartCard";
import { computeResolutionRate, deriveResolutionRateTrend, applyFindingsFilter, } from "./autofix-utils";
// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const PAGE_SIZE = 25;
const CARD_BASE = "border border-white/5 bg-[#0F1114]/60 backdrop-blur-md p-6 rounded-xl";
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
const STATUS_COLOR_MAP = {
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
const SEVERITY_DOT_COLOR = {
    critical: "bg-red-500",
    high: "bg-orange-500",
    medium: "bg-yellow-500",
    low: "bg-green-500",
    info: "bg-blue-400",
};
/** Validates that a URL uses http: or https: protocol. */
function isSafeUrl(url) {
    try {
        return ["https:", "http:"].includes(new URL(url).protocol);
    }
    catch {
        return false;
    }
}
/** PR timeline stage definitions with colors and labels. */
const PR_TIMELINE_STAGES = [
    { key: "created", color: "#EAB308", label: "Created" },
    { key: "reviewed", color: "#22D3EE", label: "Reviewed" },
    { key: "merged", color: "#22C55E", label: "Merged" },
    { key: "closed", color: "#EF4444", label: "Closed" },
];
const DEFAULT_FILTERS = {
    status: [],
    category: [],
    severity: [],
    suppression: "show",
    prState: [],
};
const SUPPRESSION_OPTIONS = ["show", "hide", "only"];
const METRIC_CARDS_ROW1 = [
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
const METRIC_CARDS_ROW2 = [
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
            const total = Object.values(m.categories).reduce((sum, cat) => sum + (cat.verification_failed ?? 0), 0);
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
    return (_jsxs("div", { className: CARD_BASE, "aria-hidden": "true", children: [_jsx(Skeleton, { className: "h-4 w-24 mb-3" }), _jsx(Skeleton, { className: "h-8 w-16" })] }));
}
function MetricCard({ def, metrics }) {
    const Icon = def.icon;
    return (_jsxs(motion.div, { className: `${CARD_BASE} card-hover-glow`, initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: { duration: 0.3 }, children: [_jsxs("div", { className: "flex items-center gap-2 mb-2", children: [_jsx(Icon, { className: `w-4 h-4 ${def.accent}`, "aria-hidden": "true" }), _jsx("span", { className: "text-xs font-mono text-slate-400 uppercase tracking-wider", children: def.label })] }), _jsx("p", { className: `text-2xl font-mono font-bold ${def.accent}`, children: def.getValue(metrics) })] }));
}
function StatusBadge({ status }) {
    const colorClass = STATUS_COLOR_MAP[status] ?? DEFAULT_STATUS_COLOR;
    return (_jsx(Badge, { variant: "outline", className: `${colorClass} rounded-full px-2.5 py-0.5 text-[10px] font-medium font-mono uppercase`, children: status }));
}
/** Small colored dot indicating severity level. */
function SeverityDot({ severity }) {
    const dotColor = SEVERITY_DOT_COLOR[severity.toLowerCase()] ?? "bg-gray-500";
    return (_jsxs("span", { className: "inline-flex items-center gap-1.5", children: [_jsx("span", { className: `inline-block w-2 h-2 rounded-full flex-shrink-0 ${dotColor}`, "aria-hidden": "true" }), _jsx("span", { children: severity })] }));
}
function ChartSkeleton() {
    return (_jsxs("div", { className: `${CARD_BASE} h-72`, "aria-hidden": "true", children: [_jsx(Skeleton, { className: "h-5 w-48 mb-4" }), _jsx(Skeleton, { className: "h-52 w-full" })] }));
}
/** Mini PR timeline showing colored dots connected by a thin line. */
function PrTimeline({ finding }) {
    if (!finding.pr_url)
        return null;
    const stages = [
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
    return (_jsx("div", { className: "flex items-center gap-0", role: "img", "aria-label": `PR timeline: ${stages.filter((s) => s.active).map((s) => s.label).join(", ")}`, children: stages.map((stage, idx) => (_jsxs("div", { className: "flex items-center", children: [idx > 0 && (_jsx("div", { className: "w-3 h-[2px]", style: { backgroundColor: stage.active ? stage.color : "#334155" } })), _jsx("div", { className: "w-2 h-2 rounded-full flex-shrink-0", style: { backgroundColor: stage.active ? stage.color : "#334155" }, title: stage.active && stage.timestamp
                        ? `${stage.label}: ${new Date(stage.timestamp).toLocaleString()}`
                        : stage.label, "aria-hidden": "true" })] }, stage.label))) }));
}
function TableSkeleton() {
    return (_jsxs("div", { className: CARD_BASE, "aria-hidden": "true", children: [_jsx(Skeleton, { className: "h-5 w-40 mb-4" }), Array.from({ length: 5 }).map((_, i) => (_jsx(Skeleton, { className: "h-8 w-full mb-2" }, i)))] }));
}
function RoutePostureSkeleton() {
    return (_jsxs("div", { className: CARD_BASE, "aria-hidden": "true", children: [_jsx(Skeleton, { className: "h-5 w-36 mb-4" }), Array.from({ length: 3 }).map((_, i) => (_jsx(Skeleton, { className: "h-8 w-full mb-2" }, i)))] }));
}
function RecentPRsSkeleton() {
    return (_jsxs("div", { className: CARD_BASE, "aria-hidden": "true", children: [_jsx(Skeleton, { className: "h-5 w-32 mb-4" }), Array.from({ length: 3 }).map((_, i) => (_jsx(Skeleton, { className: "h-8 w-full mb-2" }, i)))] }));
}
/** Multi-select toggle button for filter bar. */
function FilterToggle({ label, active, onClick, }) {
    return (_jsx("button", { onClick: onClick, className: `px-2.5 py-1 text-[10px] font-medium tracking-wider uppercase rounded-full transition-all duration-150 font-mono ${active
            ? "bg-[#BDF000]/20 text-[#BDF000] border border-[#BDF000]/30"
            : "bg-[#2A2A2A] text-[#7A776E] hover:text-[#C8C4B8] hover:bg-[#333] border border-transparent"}`, "aria-pressed": active, "aria-label": `Filter by ${label}`, children: label }));
}
/** Truncated text with tooltip for long content. */
function TruncatedCell({ text, maxLen = 60 }) {
    if (text.length <= maxLen) {
        return _jsx("span", { children: text });
    }
    return (_jsxs(Tooltip, { children: [_jsx(TooltipTrigger, { asChild: true, children: _jsxs("span", { className: "cursor-help", children: [text.slice(0, maxLen), "..."] }) }), _jsx(TooltipContent, { side: "top", className: "max-w-sm break-words", children: text })] }));
}
// ---------------------------------------------------------------------------
// Filter Bar Component (AC 15, 16, 17)
// ---------------------------------------------------------------------------
function FilterBar({ filters, setFilters, timeRange, setTimeRange, availableStatuses, availableCategories, availableSeverities, availablePrStates, }) {
    const hasActiveFilters = filters.status.length > 0 ||
        filters.category.length > 0 ||
        filters.severity.length > 0 ||
        filters.suppression !== "show" ||
        filters.prState.length > 0;
    const toggleFilter = useCallback((dimension, value) => {
        setFilters((prev) => {
            const current = prev[dimension];
            const next = current.includes(value)
                ? current.filter((v) => v !== value)
                : [...current, value];
            return { ...prev, [dimension]: next };
        });
    }, [setFilters]);
    const clearFilters = useCallback(() => {
        setFilters(() => DEFAULT_FILTERS);
    }, [setFilters]);
    return (_jsxs(motion.div, { className: `${CARD_BASE} space-y-3`, initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: { duration: 0.3 }, children: [_jsxs("div", { className: "flex items-center justify-between", children: [_jsxs("div", { className: "flex items-center gap-2", children: [_jsx(Filter, { className: "w-4 h-4 text-slate-400", "aria-hidden": "true" }), _jsx("span", { className: "text-xs font-mono text-slate-400 uppercase tracking-wider", children: "Filters" })] }), _jsxs("div", { className: "flex items-center gap-3", children: [_jsx(TimeRangeFilter, { value: timeRange, onChange: setTimeRange }), hasActiveFilters && (_jsxs("button", { onClick: clearFilters, className: "flex items-center gap-1 px-2.5 py-1 text-[10px] font-mono text-red-400 hover:text-red-300 bg-red-500/10 hover:bg-red-500/20 rounded-full transition-colors", "aria-label": "Clear all filters", children: [_jsx(X, { className: "w-3 h-3", "aria-hidden": "true" }), "Clear"] }))] })] }), availableStatuses.length > 0 && (_jsxs("div", { className: "flex flex-wrap items-center gap-1.5", children: [_jsx("span", { className: "text-[10px] font-mono text-slate-500 uppercase w-16 flex-shrink-0", children: "Status" }), availableStatuses.map((s) => (_jsx(FilterToggle, { label: s, active: filters.status.includes(s), onClick: () => toggleFilter("status", s) }, s)))] })), availableCategories.length > 0 && (_jsxs("div", { className: "flex flex-wrap items-center gap-1.5", children: [_jsx("span", { className: "text-[10px] font-mono text-slate-500 uppercase w-16 flex-shrink-0", children: "Category" }), availableCategories.map((c) => (_jsx(FilterToggle, { label: c, active: filters.category.includes(c), onClick: () => toggleFilter("category", c) }, c)))] })), availableSeverities.length > 0 && (_jsxs("div", { className: "flex flex-wrap items-center gap-1.5", children: [_jsx("span", { className: "text-[10px] font-mono text-slate-500 uppercase w-16 flex-shrink-0", children: "Severity" }), availableSeverities.map((s) => (_jsx(FilterToggle, { label: s, active: filters.severity.includes(s), onClick: () => toggleFilter("severity", s) }, s)))] })), _jsxs("div", { className: "flex flex-wrap items-center gap-1.5", children: [_jsx("span", { className: "text-[10px] font-mono text-slate-500 uppercase w-16 flex-shrink-0", children: "Suppressed" }), SUPPRESSION_OPTIONS.map((opt) => (_jsx(FilterToggle, { label: opt, active: filters.suppression === opt, onClick: () => setFilters((prev) => ({ ...prev, suppression: opt })) }, opt)))] }), availablePrStates.length > 0 && (_jsxs("div", { className: "flex flex-wrap items-center gap-1.5", children: [_jsx("span", { className: "text-[10px] font-mono text-slate-500 uppercase w-16 flex-shrink-0", children: "PR State" }), availablePrStates.map((s) => (_jsx(FilterToggle, { label: s, active: filters.prState.includes(s), onClick: () => toggleFilter("prState", s) }, s)))] }))] }));
}
// ---------------------------------------------------------------------------
// Route Posture Table (AC 9, 10)
// ---------------------------------------------------------------------------
function RoutePostureTable({ categories, categoryFilter, }) {
    const entries = useMemo(() => {
        return Object.entries(categories).filter(([name]) => categoryFilter.length === 0 || categoryFilter.includes(name));
    }, [categories, categoryFilter]);
    if (entries.length === 0) {
        return (_jsxs(motion.div, { className: CARD_BASE, initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: { duration: 0.3 }, children: [_jsx("h2", { className: "text-sm font-mono font-semibold text-slate-300 uppercase tracking-wider mb-4", children: "Route Posture" }), _jsxs("div", { className: "flex flex-col items-center justify-center py-10 gap-2", role: "status", children: [_jsx(BarChart3, { className: "w-8 h-8 text-slate-600", "aria-hidden": "true" }), _jsx("p", { className: "text-sm font-mono text-slate-500 text-center", children: "No route posture data available" })] })] }));
    }
    return (_jsxs(motion.div, { className: CARD_BASE, initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: { duration: 0.3 }, children: [_jsx("h2", { className: "text-sm font-mono font-semibold text-slate-300 uppercase tracking-wider mb-4", children: "Route Posture" }), _jsx("div", { className: "overflow-x-auto", children: _jsxs(Table, { children: [_jsx(TableHeader, { children: _jsxs(TableRow, { className: "border-white/5", children: [_jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Category" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Mode" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Enabled" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Confidence" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Merged" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Closed Unmerged" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Reverted" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Issues Opened" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Verification Failed" })] }) }), _jsx(TableBody, { children: entries.map(([name, cat], idx) => (_jsxs(TableRow, { className: `border-white/5 transition-colors hover:bg-white/[0.04] ${idx % 2 === 0 ? "bg-white/[0.02]" : ""}`, children: [_jsx(TableCell, { className: "font-mono text-xs text-slate-300 max-w-[150px] truncate", children: name }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400", children: _jsx(StatusBadge, { status: cat.mode }) }), _jsx(TableCell, { children: _jsx(Badge, { variant: "outline", className: `rounded-full px-2.5 py-0.5 text-[10px] font-medium font-mono uppercase ${cat.enabled
                                                ? "bg-green-500/20 text-green-400 border-green-500/30"
                                                : "bg-red-500/20 text-red-400 border-red-500/30"}`, children: cat.enabled ? "yes" : "no" }) }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400", children: cat.confidence.toFixed(2) }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400 text-center", children: cat.merged }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400 text-center", children: cat.closed_unmerged }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400 text-center", children: cat.reverted }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400 text-center", children: cat.issues_opened }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400 text-center", children: cat.verification_failed })] }, name))) })] }) })] }));
}
// ---------------------------------------------------------------------------
// Recent PRs Section (AC 22, 23, 24)
// ---------------------------------------------------------------------------
function RecentPRsSection({ recentPrs, categoryFilter, }) {
    const filtered = useMemo(() => {
        if (categoryFilter.length === 0)
            return recentPrs;
        return recentPrs.filter((pr) => categoryFilter.includes(pr.category));
    }, [recentPrs, categoryFilter]);
    return (_jsxs(motion.div, { className: CARD_BASE, initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: { duration: 0.3 }, children: [_jsx("h2", { className: "text-sm font-mono font-semibold text-slate-300 uppercase tracking-wider mb-4", children: "Recent PRs" }), filtered.length === 0 ? (_jsxs("div", { className: "flex flex-col items-center justify-center py-10 gap-2", role: "status", children: [_jsx(GitPullRequest, { className: "w-8 h-8 text-slate-600", "aria-hidden": "true" }), _jsx("p", { className: "text-sm font-mono text-slate-500 text-center", children: "No recent PR activity" })] })) : (_jsx("div", { className: "overflow-x-auto", children: _jsxs(Table, { children: [_jsx(TableHeader, { children: _jsxs(TableRow, { className: "border-white/5", children: [_jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "PR" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Category" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "State" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Merge Outcome" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Title" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Branch" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Created" })] }) }), _jsx(TableBody, { children: filtered.map((pr, idx) => (_jsxs(TableRow, { className: `border-white/5 transition-colors hover:bg-white/[0.04] ${idx % 2 === 0 ? "bg-white/[0.02]" : ""}`, children: [_jsx(TableCell, { children: pr.url && pr.number != null && isSafeUrl(pr.url) ? (_jsxs("a", { href: pr.url, target: "_blank", rel: "noopener noreferrer", className: "inline-flex items-center gap-1 text-[#BDF000] hover:text-[#BDF000]/80 transition-colors font-mono text-xs", "aria-label": `Open pull request #${pr.number}`, children: ["#", pr.number, _jsx(ExternalLink, { className: "w-3 h-3", "aria-hidden": "true" })] })) : (_jsx("span", { className: "text-slate-600 font-mono text-xs", "aria-label": "No PR number", children: "--" })) }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400 max-w-[120px] truncate", children: pr.category }), _jsx(TableCell, { children: _jsx(StatusBadge, { status: pr.state }) }), _jsx(TableCell, { children: pr.merge_outcome ? (_jsx(StatusBadge, { status: pr.merge_outcome })) : (_jsx("span", { className: "text-slate-600 font-mono text-xs", children: "--" })) }), _jsx(TableCell, { className: "font-mono text-xs text-slate-300 max-w-[200px]", children: _jsx(TruncatedCell, { text: pr.title, maxLen: 50 }) }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400 max-w-[150px] truncate", children: pr.branch ?? "--" }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400 whitespace-nowrap", children: pr.created_at
                                            ? new Date(pr.created_at).toLocaleDateString()
                                            : "--" })] }, `${pr.finding_id}-${pr.number ?? idx}`))) })] }) }))] }));
}
// ---------------------------------------------------------------------------
// Main page component
// ---------------------------------------------------------------------------
export default function Autofix() {
    const { data: metrics, loading: metricsLoading, error: metricsError, refetch: refetchMetrics, } = usePollingData("/api/autofix-metrics");
    const { data: findings, loading: findingsLoading, error: findingsError, refetch: refetchFindings, } = usePollingData("/api/findings");
    const [page, setPage] = useState(1);
    const [timeRange, setTimeRange] = useState("All");
    const [filters, setFilters] = useState(DEFAULT_FILTERS);
    // Reset page when filters change
    const updateFilters = useCallback((fn) => {
        setFilters((prev) => {
            const next = fn(prev);
            setPage(1);
            return next;
        });
    }, []);
    // Derive available filter options from data + currently selected (AC: survive polling refreshes)
    const availableStatuses = useMemo(() => {
        const fromData = new Set(findings?.map((f) => f.status) ?? []);
        for (const s of filters.status)
            fromData.add(s);
        return Array.from(fromData).sort();
    }, [findings, filters.status]);
    const availableCategories = useMemo(() => {
        const fromData = new Set(findings?.map((f) => f.category) ?? []);
        if (metrics?.categories) {
            for (const k of Object.keys(metrics.categories))
                fromData.add(k);
        }
        for (const c of filters.category)
            fromData.add(c);
        return Array.from(fromData).sort();
    }, [findings, metrics, filters.category]);
    const availableSeverities = useMemo(() => {
        const fromData = new Set(findings?.map((f) => f.severity) ?? []);
        for (const s of filters.severity)
            fromData.add(s);
        return Array.from(fromData).sort();
    }, [findings, filters.severity]);
    const availablePrStates = useMemo(() => {
        const fromData = new Set(findings?.map((f) => f.pr_state).filter((s) => Boolean(s)) ?? []);
        for (const s of filters.prState)
            fromData.add(s);
        return Array.from(fromData).sort();
    }, [findings, filters.prState]);
    // Apply multi-dimension filter to findings
    const filteredByDimension = useMemo(() => {
        if (!findings)
            return [];
        return applyFindingsFilter(findings, filters);
    }, [findings, filters]);
    // Time-range filtered findings for charts (applied on top of dimension filter)
    const filteredFindings = useMemo(() => {
        return filterByTimeRange(filteredByDimension, (f) => f.found_at, timeRange);
    }, [filteredByDimension, timeRange]);
    // Derive category chart data from filtered findings (AC 11: no fallback path)
    const categoryData = useMemo(() => {
        const counts = new Map();
        for (const f of filteredFindings) {
            counts.set(f.category, (counts.get(f.category) ?? 0) + 1);
        }
        return Array.from(counts.entries()).map(([name, count]) => ({ name, count }));
    }, [filteredFindings]);
    // Derive resolution rate trend from filtered findings (AC 13, 14)
    const resolutionRateTrend = useMemo(() => {
        if (filteredFindings.length === 0)
            return [];
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
    const hasActiveFilters = filters.status.length > 0 ||
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
        return (_jsxs("div", { className: "p-4 sm:p-6 space-y-6", "aria-busy": "true", "aria-label": "Loading autofix data", children: [_jsx("h1", { className: "text-lg font-mono font-semibold text-[#BDF000] tracking-wider uppercase", children: "Autofix" }), _jsx("div", { className: "grid grid-cols-2 sm:grid-cols-2 lg:grid-cols-4 gap-4", children: Array.from({ length: 4 }).map((_, i) => (_jsx(MetricCardSkeleton, {}, `r1-${i}`))) }), _jsx("div", { className: "grid grid-cols-2 sm:grid-cols-2 lg:grid-cols-4 gap-4", children: Array.from({ length: 4 }).map((_, i) => (_jsx(MetricCardSkeleton, {}, `r2-${i}`))) }), _jsx(RoutePostureSkeleton, {}), _jsx(RecentPRsSkeleton, {}), _jsx(ChartSkeleton, {}), _jsx(ChartSkeleton, {}), _jsx(TableSkeleton, {})] }));
    }
    // ---------------------------------------------------------------------------
    // Error state
    // ---------------------------------------------------------------------------
    if (hasError && !metrics && !findings) {
        return (_jsxs("div", { className: "p-4 sm:p-6 space-y-6", children: [_jsx("h1", { className: "text-lg font-mono font-semibold text-[#BDF000] tracking-wider uppercase", children: "Autofix" }), _jsxs("div", { className: `${CARD_BASE} flex flex-col items-center justify-center py-16 gap-4`, role: "alert", children: [_jsx(XCircle, { className: "w-10 h-10 text-red-400", "aria-hidden": "true" }), _jsx("p", { className: "text-sm font-mono text-slate-400 text-center max-w-md", children: "Unable to load autofix data. Please check that the daemon is running and try again." }), _jsx(Button, { variant: "outline", size: "sm", onClick: () => {
                                refetchMetrics();
                                refetchFindings();
                            }, "aria-label": "Retry loading autofix data", children: "Retry" })] })] }));
    }
    // ---------------------------------------------------------------------------
    // Success / Empty states
    // ---------------------------------------------------------------------------
    return (_jsxs("div", { className: "p-4 sm:p-6 space-y-6", children: [_jsx("h1", { className: "text-lg font-mono font-semibold text-[#BDF000] tracking-wider uppercase", children: "Autofix" }), metrics && (_jsxs(_Fragment, { children: [_jsx("div", { className: "grid grid-cols-2 sm:grid-cols-2 lg:grid-cols-4 gap-4", children: METRIC_CARDS_ROW1.map((def) => (_jsx(MetricCard, { def: def, metrics: metrics }, def.label))) }), _jsx("div", { className: "grid grid-cols-2 sm:grid-cols-2 lg:grid-cols-4 gap-4", children: METRIC_CARDS_ROW2.map((def) => (_jsx(MetricCard, { def: def, metrics: metrics }, def.label))) })] })), _jsx(FilterBar, { filters: filters, setFilters: updateFilters, timeRange: timeRange, setTimeRange: setTimeRange, availableStatuses: availableStatuses, availableCategories: availableCategories, availableSeverities: availableSeverities, availablePrStates: availablePrStates }), metrics && (_jsx(RoutePostureTable, { categories: metrics.categories, categoryFilter: filters.category })), metrics && (_jsx(RecentPRsSection, { recentPrs: metrics.recent_prs ?? [], categoryFilter: filters.category })), _jsx(motion.div, { initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: { duration: 0.3, delay: 0.1 }, children: _jsx(ChartCard, { title: "Category Breakdown", children: categoryData.length > 0 ? (_jsx("div", { className: "h-64", children: _jsx(ResponsiveContainer, { width: "100%", height: "100%", children: _jsxs(BarChart, { data: categoryData, margin: { top: 8, right: 16, bottom: 8, left: 0 }, children: [_jsx(CartesianGrid, { strokeDasharray: "3 3", stroke: "#333", vertical: false }), _jsx(XAxis, { dataKey: "name", tick: { fill: "#999", fontFamily: "'JetBrains Mono', monospace", fontSize: 11 }, axisLine: { stroke: "#333" }, tickLine: false }), _jsx(YAxis, { tick: { fill: "#999", fontFamily: "'JetBrains Mono', monospace", fontSize: 11 }, axisLine: { stroke: "#333" }, tickLine: false, allowDecimals: false }), _jsx(RechartsTooltip, { contentStyle: {
                                            background: "#0D1321",
                                            border: "1px solid rgba(189, 240, 0, 0.15)",
                                            borderRadius: "8px",
                                            fontFamily: "'JetBrains Mono', monospace",
                                            fontSize: "12px",
                                            color: "#E2E8F0",
                                        }, cursor: { fill: "rgba(189, 240, 0, 0.05)" } }), _jsx(Bar, { dataKey: "count", radius: [4, 4, 0, 0], children: categoryData.map((_, idx) => (_jsx(Cell, { fill: GRADIENT_PALETTE[idx % GRADIENT_PALETTE.length] }, idx))) })] }) }) })) : (_jsxs("div", { className: "flex flex-col items-center justify-center py-10 gap-2", role: "status", children: [_jsx(BarChart3, { className: "w-8 h-8 text-slate-600", "aria-hidden": "true" }), _jsx("p", { className: "text-sm font-mono text-slate-500 text-center", children: "No category data for the current filters" })] })) }) }), _jsx(motion.div, { initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: { duration: 0.3, delay: 0.2 }, children: _jsx(ChartCard, { title: "Resolution Rate Trend", children: resolutionRateTrend.length > 0 ? (_jsx("div", { className: "h-64", children: _jsx(ResponsiveContainer, { width: "100%", height: "100%", children: _jsxs(AreaChart, { data: resolutionRateTrend, margin: { top: 8, right: 16, bottom: 8, left: 0 }, children: [_jsx("defs", { children: _jsxs("linearGradient", { id: "resolutionRateGradient", x1: "0", y1: "0", x2: "0", y2: "1", children: [_jsx("stop", { offset: "5%", stopColor: "#BDF000", stopOpacity: 0.3 }), _jsx("stop", { offset: "95%", stopColor: "#BDF000", stopOpacity: 0.02 })] }) }), _jsx(CartesianGrid, { strokeDasharray: "3 3", stroke: "#333", vertical: false }), _jsx(XAxis, { dataKey: "week", tick: { fill: "#999", fontFamily: "'JetBrains Mono', monospace", fontSize: 11 }, axisLine: { stroke: "#333" }, tickLine: false }), _jsx(YAxis, { tick: { fill: "#999", fontFamily: "'JetBrains Mono', monospace", fontSize: 11 }, axisLine: { stroke: "#333" }, tickLine: false, domain: [0, 100], unit: "%" }), _jsx(RechartsTooltip, { contentStyle: {
                                            background: "#0D1321",
                                            border: "1px solid rgba(189, 240, 0, 0.15)",
                                            borderRadius: "8px",
                                            fontFamily: "'JetBrains Mono', monospace",
                                            fontSize: "12px",
                                            color: "#E2E8F0",
                                        }, formatter: (value) => [`${value}%`, "Resolution Rate"], cursor: { stroke: "rgba(189, 240, 0, 0.3)" } }), _jsx(Area, { type: "monotone", dataKey: "rate", stroke: "#BDF000", strokeWidth: 2, fill: "url(#resolutionRateGradient)", dot: { fill: "#BDF000", r: 3, strokeWidth: 0 }, activeDot: { fill: "#BDF000", r: 5, strokeWidth: 2, stroke: "#0F1114" } })] }) }) })) : (_jsxs("div", { className: "flex flex-col items-center justify-center py-10 gap-2", role: "status", children: [_jsx(BarChart3, { className: "w-8 h-8 text-slate-600", "aria-hidden": "true" }), _jsx("p", { className: "text-sm font-mono text-slate-500 text-center", children: "No trend data for the current filters" })] })) }) }), _jsxs(motion.div, { className: CARD_BASE, initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: { duration: 0.3, delay: 0.3 }, children: [_jsx("h2", { className: "text-sm font-mono font-semibold text-slate-300 uppercase tracking-wider mb-4", children: "Findings" }), totalFilteredFindings === 0 ? (
                    /* Empty state: distinguish filter-empty from data-empty */
                    hasActiveFilters ? (_jsxs("div", { className: "flex flex-col items-center justify-center py-16 gap-3", role: "status", children: [_jsx(Filter, { className: "w-10 h-10 text-slate-600", "aria-hidden": "true" }), _jsx("p", { className: "text-sm font-mono text-slate-500 text-center", children: "No findings match your filters" }), _jsx(Button, { variant: "outline", size: "sm", onClick: () => updateFilters(() => DEFAULT_FILTERS), "aria-label": "Clear all filters", children: "Clear Filters" })] })) : (_jsxs("div", { className: "flex flex-col items-center justify-center py-16 gap-3", role: "status", children: [_jsx(Bug, { className: "w-10 h-10 text-slate-600", "aria-hidden": "true" }), _jsx("p", { className: "text-sm font-mono text-slate-500 text-center", children: "No findings recorded" }), _jsx("p", { className: "text-xs font-mono text-slate-600 text-center max-w-sm", children: "When the autofix scanner detects issues in your codebase, they will appear here." })] }))) : (_jsxs(_Fragment, { children: [_jsx("div", { className: "overflow-x-auto", children: _jsxs(Table, { children: [_jsx(TableHeader, { children: _jsxs(TableRow, { className: "border-white/5", children: [_jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Finding ID" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Category" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Severity" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Status" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Description" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "PR" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Issue" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Timeline" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Attempts" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Fail Reason" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Suppression" }), _jsx(TableHead, { className: "text-slate-400 font-mono text-xs", children: "Confidence" })] }) }), _jsx(TableBody, { children: paginatedFindings.map((finding, idx) => {
                                                const isSuppressed = Boolean(finding.suppressed_until) || finding.status === "suppressed";
                                                const isIssueOnly = finding.status === "issue-opened" || Boolean(finding.issue_url);
                                                return (_jsxs(TableRow, { className: `border-white/5 transition-colors hover:bg-white/[0.04] ${isSuppressed
                                                        ? "opacity-50"
                                                        : isIssueOnly
                                                            ? "bg-cyan-500/[0.03]"
                                                            : idx % 2 === 0
                                                                ? "bg-white/[0.02]"
                                                                : ""}`, children: [_jsx(TableCell, { className: "font-mono text-xs text-slate-300 max-w-[180px] truncate", children: finding.finding_id }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400 max-w-[120px] truncate", children: finding.category }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400", children: _jsx(SeverityDot, { severity: finding.severity }) }), _jsx(TableCell, { children: _jsxs("div", { className: "flex items-center gap-1.5", children: [_jsx(StatusBadge, { status: finding.status }), isSuppressed && (_jsx(Badge, { variant: "outline", className: "bg-gray-500/10 text-gray-500 border-gray-500/20 rounded-full px-1.5 py-0 text-[9px] font-mono uppercase", children: "suppressed" }))] }) }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400 max-w-[200px]", children: _jsx(TruncatedCell, { text: finding.description, maxLen: 60 }) }), _jsx(TableCell, { children: finding.pr_url && isSafeUrl(finding.pr_url) ? (_jsxs("a", { href: finding.pr_url, target: "_blank", rel: "noopener noreferrer", className: "inline-flex items-center gap-1 text-[#BDF000] hover:text-[#BDF000]/80 transition-colors font-mono text-xs", "aria-label": `Open pull request ${finding.pr_number ?? ""}`, children: ["#", finding.pr_number ?? "", _jsx(ExternalLink, { className: "w-3 h-3", "aria-hidden": "true" })] })) : (_jsx("span", { className: "text-slate-600 font-mono text-xs", "aria-label": "No pull request", children: "--" })) }), _jsx(TableCell, { children: finding.issue_url && isSafeUrl(finding.issue_url) ? (_jsxs("a", { href: finding.issue_url, target: "_blank", rel: "noopener noreferrer", className: "inline-flex items-center gap-1 text-[#BDF000] hover:text-[#BDF000]/80 transition-colors font-mono text-xs", "aria-label": `Open issue ${finding.issue_number ?? ""}`, children: ["#", finding.issue_number, _jsx(ExternalLink, { className: "w-3 h-3", "aria-hidden": "true" })] })) : (_jsx("span", { className: "text-slate-600 font-mono text-xs", "aria-label": "No issue", children: "--" })) }), _jsx(TableCell, { children: finding.pr_url ? (_jsx(PrTimeline, { finding: finding })) : (_jsx("span", { className: "text-slate-600 font-mono text-xs", "aria-label": "No timeline", children: "--" })) }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400 text-center", children: finding.attempt_count }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400 max-w-[150px]", children: finding.fail_reason ? (_jsx(TruncatedCell, { text: finding.fail_reason, maxLen: 40 })) : (_jsx("span", { className: "text-slate-600", children: "--" })) }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400 max-w-[150px]", children: finding.suppression_reason || finding.suppressed_until ? (_jsxs("div", { className: "space-y-0.5", children: [finding.suppression_reason && (_jsx(TruncatedCell, { text: finding.suppression_reason, maxLen: 30 })), finding.suppressed_until && (_jsxs("div", { className: "text-[10px] text-slate-500", children: ["until ", new Date(finding.suppressed_until).toLocaleDateString()] }))] })) : (_jsx("span", { className: "text-slate-600", children: "--" })) }), _jsx(TableCell, { className: "font-mono text-xs text-slate-400 text-center", children: finding.confidence_score != null
                                                                ? finding.confidence_score.toFixed(2)
                                                                : "--" })] }, finding.finding_id));
                                            }) })] }) }), _jsxs("div", { className: "flex items-center justify-between mt-4 pt-4 border-t border-white/5", children: [_jsxs("span", { className: "text-xs font-mono text-slate-500", children: ["Page ", clampedPage, " of ", totalPages, " (", totalFilteredFindings, " findings)"] }), _jsxs("div", { className: "flex items-center gap-2", children: [_jsx(Button, { variant: "outline", size: "sm", onClick: () => setPage((p) => Math.max(1, p - 1)), disabled: clampedPage <= 1, "aria-label": "Previous page", children: "Prev" }), _jsx(Button, { variant: "outline", size: "sm", onClick: () => setPage((p) => Math.min(totalPages, p + 1)), disabled: clampedPage >= totalPages, "aria-label": "Next page", children: "Next" })] })] })] }))] })] }));
}
