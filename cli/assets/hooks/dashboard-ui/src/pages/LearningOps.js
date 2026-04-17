import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
/**
 * Learning Ops page — /learning-ops
 *
 * Unified view of the learning system health: daemon status, maintenance
 * cycles, promotion funnel, benchmark freshness, coverage gaps, and
 * attention queue. Fetches from three endpoints on mount.
 */
import { useMemo } from "react";
import { motion } from "motion/react";
import { BarChart, Bar, XAxis, YAxis, ResponsiveContainer, Tooltip as RechartsTooltip, Cell, Pie, PieChart, Legend, } from "recharts";
import { AlertTriangle, CheckCircle2, XCircle, } from "lucide-react";
import { usePollingData } from "@/data/hooks";
import { Table, TableHeader, TableBody, TableHead, TableRow, TableCell, } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ChartCard } from "@/components/ChartCard";
// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const CARD_BASE = "border border-white/5 bg-[#0F1114]/60 backdrop-blur-md p-6 rounded-xl";
const COLORS = {
    primary: "#BDF000",
    secondary: "#2DD4A8",
    warning: "#FF6D00",
    danger: "#FF3B3B",
    purple: "#B47AFF",
};
const MODE_COLORS = {
    replace: COLORS.primary,
    alongside: COLORS.secondary,
    shadow: COLORS.purple,
};
const FRESHNESS_COLORS = {
    Fresh: COLORS.primary,
    Recent: COLORS.secondary,
    Aging: COLORS.warning,
    Stale: COLORS.danger,
    Unbenchmarked: "#7A776E",
};
const CHART_HEIGHT = 280;
const PIE_HEIGHT = 320;
const CHART_MARGIN = { top: 8, right: 16, left: 0, bottom: 8 };
const AXIS_TICK_STYLE = {
    fill: "#999",
    fontFamily: "JetBrains Mono",
    fontSize: 11,
};
const TOOLTIP_STYLE = {
    backgroundColor: "#1A1F2E",
    border: "1px solid #333",
    borderRadius: 8,
    fontFamily: "JetBrains Mono",
    fontSize: 11,
    color: "#ccc",
};
const URGENCY_ORDER = {
    "demoted on regression": 0,
    unbenchmarked: 1,
    "stale benchmark": 2,
    "coverage gap": 3,
};
const POLL_INTERVAL = 30_000;
// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function getBannerColor(opts) {
    if (!opts.running)
        return "red";
    if (!opts.lastCycleOk || opts.queueBacklog > 0)
        return "amber";
    return "green";
}
const BANNER_COLOR_MAP = {
    green: { border: "border-green-500/30", bg: "bg-green-500/10", text: "text-green-400" },
    amber: { border: "border-yellow-500/30", bg: "bg-yellow-500/10", text: "text-yellow-400" },
    red: { border: "border-red-500/30", bg: "bg-red-500/10", text: "text-red-400" },
};
function formatRelativeTime(iso) {
    if (!iso)
        return "n/a";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime()))
        return iso;
    const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
    if (seconds < 60)
        return `${seconds}s ago`;
    if (seconds < 3600)
        return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400)
        return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
}
function formatTimestamp(iso) {
    if (!iso)
        return "n/a";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime()))
        return iso;
    return date.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
    });
}
function formatDate(iso) {
    if (!iso)
        return "n/a";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime()))
        return iso;
    return date.toLocaleString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
    });
}
function classifyFreshness(offset, hasBenchmark) {
    if (!hasBenchmark)
        return "Unbenchmarked";
    if (offset === 0)
        return "Fresh";
    if (offset <= 2)
        return "Recent";
    if (offset <= 5)
        return "Aging";
    return "Stale";
}
// ---------------------------------------------------------------------------
// Sub-components: Skeletons
// ---------------------------------------------------------------------------
function BannerSkeleton() {
    return (_jsx("div", { className: CARD_BASE, role: "status", "aria-label": "Loading health banner", children: _jsx("div", { className: "grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-3", children: Array.from({ length: 7 }).map((_, i) => (_jsxs("div", { children: [_jsx(Skeleton, { className: "h-3 w-16 mb-2" }), _jsx(Skeleton, { className: "h-6 w-12" })] }, i))) }) }));
}
function SectionSkeleton({ label }) {
    return (_jsxs("div", { className: CARD_BASE, role: "status", "aria-label": `Loading ${label}`, children: [_jsx(Skeleton, { className: "h-5 w-48 mb-4" }), _jsx(Skeleton, { className: "h-52 w-full" })] }));
}
// ---------------------------------------------------------------------------
// Sub-components: Error
// ---------------------------------------------------------------------------
function SectionError({ message, onRetry }) {
    return (_jsxs("div", { className: "border border-red-500/30 bg-red-500/10 backdrop-blur-md p-6 text-center rounded-xl", role: "alert", children: [_jsx(XCircle, { className: "w-6 h-6 text-red-400 mx-auto mb-2", "aria-hidden": "true" }), _jsx("p", { className: "text-slate-400 text-sm font-mono mb-4", children: message }), _jsx("button", { onClick: onRetry, className: "px-4 py-2 bg-[#BDF000]/5 hover:bg-[#BDF000]/20 text-[#BDF000] border border-[#BDF000]/20 font-mono text-xs transition-colors rounded-xl", "aria-label": "Retry loading", children: "RETRY" })] }));
}
function HealthBanner({ maintainer, controlPlane }) {
    const lastCycleOk = maintainer.last_cycle?.ok ?? true;
    const queueBacklog = controlPlane.queue?.items?.length ?? 0;
    const bannerColor = getBannerColor({
        running: maintainer.running,
        lastCycleOk,
        queueBacklog,
    });
    const colorClasses = BANNER_COLOR_MAP[bannerColor];
    const summary = controlPlane.agent_summary ?? { total: 0, routeable: 0, shadow: 0, demoted: 0 };
    const fields = [
        { label: "Daemon", value: maintainer.running ? "Running" : "Stopped" },
        { label: "PID", value: maintainer.pid ?? "n/a" },
        { label: "Last Cycle", value: maintainer.last_cycle?.executed_at ? formatRelativeTime(maintainer.last_cycle.executed_at) : "n/a" },
        { label: "Cycle Status", value: lastCycleOk ? "OK" : "Failed" },
        { label: "Total Cycles", value: maintainer.cycle_count ?? 0 },
        { label: "Poll Interval", value: `${maintainer.poll_seconds ?? 0}s` },
        { label: "Autofix", value: controlPlane.autofix_enabled ? "Enabled" : "Disabled" },
        { label: "Total Agents", value: summary.total },
        { label: "Routeable", value: summary.routeable },
        { label: "Shadow", value: summary.shadow },
        { label: "Demoted", value: summary.demoted },
        { label: "Queue", value: queueBacklog },
        { label: "Coverage Gaps", value: controlPlane.coverage_gaps?.length ?? 0 },
    ];
    return (_jsxs(motion.div, { className: `${CARD_BASE} ${colorClasses.border} ${colorClasses.bg}`, initial: { opacity: 0, y: -8 }, animate: { opacity: 1, y: 0 }, transition: { duration: 0.3 }, role: "banner", "aria-label": "Learning system health", children: [_jsxs("div", { className: "flex items-center gap-2 mb-4", children: [bannerColor === "green" && _jsx(CheckCircle2, { className: "w-4 h-4 text-green-400", "aria-hidden": "true" }), bannerColor === "amber" && _jsx(AlertTriangle, { className: "w-4 h-4 text-yellow-400", "aria-hidden": "true" }), bannerColor === "red" && _jsx(XCircle, { className: "w-4 h-4 text-red-400", "aria-hidden": "true" }), _jsx("span", { className: `text-xs font-mono uppercase tracking-wider ${colorClasses.text}`, children: "System Health" })] }), _jsx("div", { className: "grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-x-6 gap-y-3", children: fields.map((field) => (_jsxs("div", { children: [_jsx("div", { className: "text-[10px] text-slate-500 font-mono uppercase tracking-wider", children: field.label }), _jsx("div", { className: "text-sm font-mono text-slate-200 mt-0.5 truncate", title: String(field.value), children: field.value })] }, field.label))) })] }));
}
function MaintenanceCyclesSection({ cycles, totalCycles }) {
    const last20 = cycles.slice(-20);
    const totalFailures = cycles.filter((c) => !(c.ok ?? c.failed_steps.length === 0)).length;
    const failureRate = totalCycles === 0 ? 0 : (totalFailures / totalCycles) * 100;
    return (_jsx(motion.div, { initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: { delay: 0.1, duration: 0.3 }, children: _jsxs(ChartCard, { title: "Maintenance Cycles", subtitle: "Daemon cycle history and failure tracking.", children: [_jsxs("div", { className: "grid grid-cols-3 gap-4 mb-6", children: [_jsxs("div", { children: [_jsx("div", { className: "text-[10px] text-slate-500 font-mono uppercase tracking-wider", children: "Total Cycles" }), _jsx("div", { className: "text-xl font-mono text-slate-200", children: totalCycles })] }), _jsxs("div", { children: [_jsx("div", { className: "text-[10px] text-slate-500 font-mono uppercase tracking-wider", children: "Total Failures" }), _jsx("div", { className: "text-xl font-mono text-red-400", children: totalFailures })] }), _jsxs("div", { children: [_jsx("div", { className: "text-[10px] text-slate-500 font-mono uppercase tracking-wider", children: "Failure Rate" }), _jsxs("div", { className: "text-xl font-mono text-slate-200", children: [failureRate.toFixed(1), "%"] })] })] }), _jsxs("div", { className: "mb-4", children: [_jsx("div", { className: "flex items-center gap-1.5 flex-wrap", role: "img", "aria-label": "Cycle outcomes timeline", children: last20.map((cycle, idx) => {
                                const ok = cycle.ok ?? cycle.failed_steps.length === 0;
                                return (_jsx("div", { className: "w-3 h-3 rounded-full flex-shrink-0", style: { backgroundColor: ok ? COLORS.secondary : COLORS.danger }, title: `${formatTimestamp(cycle.executed_at)}: ${ok ? "OK" : "Failed"}`, "aria-hidden": "true" }, `${cycle.executed_at}-${idx}`));
                            }) }), _jsx("p", { className: "text-[10px] text-slate-500 font-mono mt-2", children: "Cycle outcomes (last 20)." })] }), cycles.length === 0 ? (_jsx("p", { className: "text-slate-600 font-mono text-xs py-8 text-center", children: "No maintenance cycles recorded yet." })) : (_jsx("div", { className: "overflow-x-auto max-h-[400px] overflow-y-auto", children: _jsxs(Table, { children: [_jsx(TableHeader, { children: _jsxs(TableRow, { className: "border-b border-white/10", children: [_jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Timestamp" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Status" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Failed Steps" })] }) }), _jsx(TableBody, { children: last20.map((cycle, idx) => {
                                    const ok = cycle.ok ?? cycle.failed_steps.length === 0;
                                    return (_jsxs(TableRow, { className: "border-b border-white/5", children: [_jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: formatTimestamp(cycle.executed_at) }), _jsx(TableCell, { children: _jsx(Badge, { variant: "outline", className: `font-mono text-[10px] uppercase ${ok ? "text-green-400 border-green-500/30" : "text-red-400 border-red-500/30"}`, children: ok ? "OK" : "Failed" }) }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: cycle.failed_steps.length === 0 ? "none" : cycle.failed_steps.join(", ") })] }, `${cycle.executed_at}-${idx}`));
                                }) })] }) }))] }) }));
}
function PromotionFunnelSection({ agents }) {
    const funnelData = useMemo(() => {
        const shadow = agents.filter((a) => a.mode === "shadow").length;
        const alongside = agents.filter((a) => a.mode === "alongside").length;
        const replace = agents.filter((a) => a.mode === "replace").length;
        return [{ name: "Distribution", shadow, alongside, replace }];
    }, [agents]);
    const demotedCount = useMemo(() => agents.filter((a) => a.mode === "demoted" || a.status.includes("demoted")).length, [agents]);
    if (agents.length === 0) {
        return (_jsx(motion.div, { initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: { delay: 0.15, duration: 0.3 }, children: _jsx(ChartCard, { title: "Promotion Funnel", subtitle: "Agent mode distribution and roster.", children: _jsx("p", { className: "text-slate-600 font-mono text-xs py-8 text-center", children: "No learned agents registered." }) }) }));
    }
    return (_jsx(motion.div, { initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: { delay: 0.15, duration: 0.3 }, children: _jsxs(ChartCard, { title: "Promotion Funnel", subtitle: "Agent mode distribution and roster.", children: [_jsxs("div", { className: "mb-4", children: [_jsx(ResponsiveContainer, { width: "100%", height: 80, children: _jsxs(BarChart, { data: funnelData, layout: "vertical", margin: { top: 0, right: 16, left: 0, bottom: 0 }, children: [_jsx(XAxis, { type: "number", hide: true }), _jsx(YAxis, { type: "category", dataKey: "name", hide: true }), _jsx(RechartsTooltip, { contentStyle: TOOLTIP_STYLE }), _jsx(Bar, { dataKey: "shadow", stackId: "a", fill: COLORS.purple, name: "Shadow", radius: [4, 0, 0, 4] }), _jsx(Bar, { dataKey: "alongside", stackId: "a", fill: COLORS.secondary, name: "Alongside" }), _jsx(Bar, { dataKey: "replace", stackId: "a", fill: COLORS.primary, name: "Replace", radius: [0, 4, 4, 0] })] }) }), _jsxs("div", { className: "flex items-center gap-4 mt-2", children: [_jsxs("span", { className: "text-[10px] font-mono text-slate-500 flex items-center gap-1", children: [_jsx("span", { className: "w-2 h-2 rounded-full inline-block", style: { backgroundColor: COLORS.purple }, "aria-hidden": "true" }), " Shadow"] }), _jsxs("span", { className: "text-[10px] font-mono text-slate-500 flex items-center gap-1", children: [_jsx("span", { className: "w-2 h-2 rounded-full inline-block", style: { backgroundColor: COLORS.secondary }, "aria-hidden": "true" }), " Alongside"] }), _jsxs("span", { className: "text-[10px] font-mono text-slate-500 flex items-center gap-1", children: [_jsx("span", { className: "w-2 h-2 rounded-full inline-block", style: { backgroundColor: COLORS.primary }, "aria-hidden": "true" }), " Replace"] }), _jsxs("span", { className: "text-[10px] font-mono text-slate-500 ml-auto", children: ["Demoted: ", _jsx("span", { className: "text-red-400", children: demotedCount })] })] }), _jsx("p", { className: "text-[10px] text-slate-500 font-mono mt-2", children: "Current-state distribution (not historical transitions)." })] }), _jsx("div", { className: "overflow-x-auto max-h-[400px] overflow-y-auto", children: _jsxs(Table, { children: [_jsx(TableHeader, { children: _jsxs(TableRow, { className: "border-b border-white/10", children: [_jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Name" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Kind" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Role" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Task Type" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Mode" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Status" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Route" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Composite" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Evaluated" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Generated From" })] }) }), _jsx(TableBody, { children: agents.map((agent) => (_jsxs(TableRow, { className: "border-b border-white/5", children: [_jsx(TableCell, { className: "text-xs font-mono text-slate-300 max-w-[160px] truncate", title: agent.agent_name, children: agent.agent_name }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: agent.item_kind }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: agent.role }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: agent.task_type }), _jsx(TableCell, { children: _jsx(Badge, { variant: "outline", className: "font-mono text-[10px] uppercase", style: { borderColor: `${MODE_COLORS[agent.mode] ?? "#7A776E"}55`, color: MODE_COLORS[agent.mode] ?? "#7A776E" }, children: agent.mode }) }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: agent.status }), _jsx(TableCell, { children: _jsx(Badge, { variant: "outline", className: `font-mono text-[10px] uppercase ${agent.route_allowed ? "text-green-400 border-green-500/30" : "text-red-400 border-red-500/30"}`, children: agent.route_allowed ? "Yes" : "No" }) }), _jsx(TableCell, { className: "text-xs font-mono text-slate-300", children: agent.benchmark_summary?.mean_composite?.toFixed(2) ?? "n/a" }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: formatDate(agent.last_evaluation?.evaluated_at) }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400 max-w-[120px] truncate", title: agent.generated_from, children: agent.generated_from || "n/a" })] }, `${agent.agent_name}-${agent.role}-${agent.task_type}`))) })] }) })] }) }));
}
function BenchmarkFreshnessSection({ agents, freshnessBuckets, coverageGaps, recentRuns }) {
    const pieData = useMemo(() => {
        // Prefer server-computed buckets, fall back to client-computed
        if (freshnessBuckets && freshnessBuckets.length > 0) {
            return freshnessBuckets.map((b) => ({
                name: b.label,
                value: b.count,
                color: FRESHNESS_COLORS[b.label] ?? "#7A776E",
            }));
        }
        const counts = { Fresh: 0, Recent: 0, Aging: 0, Stale: 0, Unbenchmarked: 0 };
        for (const agent of agents) {
            const hasBenchmark = agent.benchmark_summary != null && (agent.benchmark_summary.sample_count ?? 0) > 0;
            const bucket = classifyFreshness(agent.last_benchmarked_task_offset, hasBenchmark);
            counts[bucket]++;
        }
        return Object.entries(counts)
            .filter(([, count]) => count > 0)
            .map(([name, value]) => ({ name, value, color: FRESHNESS_COLORS[name] ?? "#7A776E" }));
    }, [agents, freshnessBuckets]);
    const last10Runs = recentRuns.slice(-10);
    return (_jsx(motion.div, { initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: { delay: 0.2, duration: 0.3 }, children: _jsxs("div", { className: "space-y-6", children: [_jsxs("div", { className: "grid grid-cols-1 xl:grid-cols-2 gap-6", children: [_jsxs(ChartCard, { title: "Benchmark Freshness", subtitle: "Distribution of benchmark staleness across agents.", children: [pieData.length === 0 ? (_jsx("p", { className: "text-slate-600 font-mono text-xs py-8 text-center", children: "No agent data available." })) : (_jsx(ResponsiveContainer, { width: "100%", height: PIE_HEIGHT, children: _jsxs(PieChart, { children: [_jsx(Pie, { data: pieData, dataKey: "value", nameKey: "name", cx: "50%", cy: "44%", innerRadius: "45%", outerRadius: "70%", paddingAngle: 2, label: false, children: pieData.map((entry) => (_jsx(Cell, { fill: entry.color }, entry.name))) }), _jsx(RechartsTooltip, { contentStyle: TOOLTIP_STYLE, formatter: (value) => [value, "Agents"] }), _jsx(Legend, { wrapperStyle: { fontFamily: "JetBrains Mono", fontSize: 11, color: "#999" } })] }) })), _jsx("p", { className: "text-[10px] text-slate-500 font-mono mt-2", children: "Based on last_benchmarked_task_offset at time of page load." })] }), _jsx(ChartCard, { title: "Coverage Gaps", subtitle: "Roles and task types with no learned agent.", children: coverageGaps.length === 0 ? (_jsx("p", { className: "text-slate-600 font-mono text-xs py-8 text-center", children: "No coverage gaps detected." })) : (_jsx("div", { className: "overflow-x-auto max-h-[320px] overflow-y-auto", children: _jsxs(Table, { children: [_jsx(TableHeader, { children: _jsxs(TableRow, { className: "border-b border-white/10", children: [_jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Target" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Role" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Task Type" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Kind" })] }) }), _jsx(TableBody, { children: coverageGaps.map((gap, idx) => (_jsxs(TableRow, { className: "border-b border-white/5", children: [_jsx(TableCell, { className: "text-xs font-mono text-slate-300 max-w-[160px] truncate", title: gap.target_name, children: gap.target_name }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: gap.role }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: gap.task_type }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: gap.item_kind })] }, `${gap.target_name}-${idx}`))) })] }) })) })] }), _jsx(ChartCard, { title: "Recent Benchmark Runs", subtitle: "Last 10 benchmark runs across all agents.", children: last10Runs.length === 0 ? (_jsx("p", { className: "text-slate-600 font-mono text-xs py-8 text-center", children: "No benchmark runs recorded yet." })) : (_jsx("div", { className: "overflow-x-auto", children: _jsxs(Table, { children: [_jsx(TableHeader, { children: _jsxs(TableRow, { className: "border-b border-white/10", children: [_jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Run ID" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Target" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Role" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Task Type" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Executed" })] }) }), _jsx(TableBody, { children: last10Runs.map((run, idx) => (_jsxs(TableRow, { className: "border-b border-white/5", children: [_jsx(TableCell, { className: "text-xs font-mono text-slate-300 max-w-[120px] truncate", title: String(run.run_id ?? ""), children: String(run.run_id ?? "n/a") }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: String(run.target_name ?? "n/a") }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: String(run.role ?? "n/a") }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: String(run.task_type ?? "n/a") }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: formatTimestamp(String(run.executed_at ?? "")) })] }, `${String(run.run_id ?? idx)}-${idx}`))) })] }) })) })] }) }));
}
function AttentionQueueSection({ items }) {
    const sorted = useMemo(() => [...items].sort((a, b) => (URGENCY_ORDER[a.reason] ?? 99) - (URGENCY_ORDER[b.reason] ?? 99)), [items]);
    if (sorted.length === 0) {
        return (_jsx(motion.div, { initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: { delay: 0.25, duration: 0.3 }, children: _jsx(ChartCard, { title: "Attention Queue", subtitle: "Items requiring manual review or intervention.", children: _jsxs("div", { className: "py-8 text-center", children: [_jsx(CheckCircle2, { className: "w-8 h-8 text-green-400 mx-auto mb-2", "aria-hidden": "true" }), _jsx("p", { className: "text-slate-400 font-mono text-sm", children: "Nothing needs attention" })] }) }) }));
    }
    const reasonBadgeColor = {
        "demoted on regression": "text-red-400 border-red-500/30",
        unbenchmarked: "text-yellow-400 border-yellow-500/30",
        "stale benchmark": "text-orange-400 border-orange-500/30",
        "coverage gap": "text-slate-400 border-slate-500/30",
    };
    return (_jsx(motion.div, { initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, transition: { delay: 0.25, duration: 0.3 }, children: _jsx(ChartCard, { title: "Attention Queue", subtitle: "Items requiring manual review or intervention.", children: _jsx("div", { className: "overflow-x-auto max-h-[400px] overflow-y-auto", children: _jsxs(Table, { children: [_jsx(TableHeader, { children: _jsxs(TableRow, { className: "border-b border-white/10", children: [_jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Agent" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Reason" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Mode" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Status" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Recommendation" }), _jsx(TableHead, { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Delta" })] }) }), _jsx(TableBody, { children: sorted.map((item, idx) => (_jsxs(TableRow, { className: "border-b border-white/5", children: [_jsx(TableCell, { className: "text-xs font-mono text-slate-300 max-w-[160px] truncate", title: item.agent_name, children: item.agent_name }), _jsx(TableCell, { children: _jsx(Badge, { variant: "outline", className: `font-mono text-[10px] ${reasonBadgeColor[item.reason] ?? "text-slate-400 border-slate-500/30"}`, children: item.reason }) }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: item.mode }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: item.status }), _jsx(TableCell, { className: "text-xs font-mono text-slate-400", children: item.recommendation ?? "n/a" }), _jsx(TableCell, { className: "text-xs font-mono text-slate-300", children: item.delta_composite != null ? item.delta_composite.toFixed(2) : "n/a" })] }, `${item.agent_name}-${item.reason}-${idx}`))) })] }) }) }) }));
}
export default function LearningOps() {
    const status = usePollingData("/api/maintainer-status", POLL_INTERVAL);
    const cyclesData = usePollingData("/api/maintenance-cycles", POLL_INTERVAL);
    const controlPlane = usePollingData("/api/control-plane", POLL_INTERVAL);
    const statusLoading = status.loading && status.data === null;
    const cyclesLoading = cyclesData.loading && cyclesData.data === null;
    const cpLoading = controlPlane.loading && controlPlane.data === null;
    const statusError = status.error !== null && status.data === null;
    const cyclesError = cyclesData.error !== null && cyclesData.data === null;
    const cpError = controlPlane.error !== null && controlPlane.data === null;
    return (_jsxs("div", { className: "p-8 h-full flex flex-col", children: [_jsxs("header", { className: "mb-8", children: [_jsx("h1", { className: "text-3xl font-mono font-light tracking-[0.2em] text-[#BDF000]", children: "LEARNING OPS" }), _jsx("p", { className: "text-slate-500 font-mono text-xs mt-2", children: "// DAEMON HEALTH, MAINTENANCE CYCLES, PROMOTION FUNNEL, BENCHMARK COVERAGE" })] }), _jsxs("div", { className: "space-y-6 flex-1 overflow-auto", children: [(statusLoading || cpLoading) && !status.data && !controlPlane.data && _jsx(BannerSkeleton, {}), statusError && !cpError && (_jsx(SectionError, { message: `Failed to load maintainer status. ${status.error ?? ""}`, onRetry: status.refetch })), cpError && !statusError && (_jsx(SectionError, { message: `Failed to load control plane. ${controlPlane.error ?? ""}`, onRetry: controlPlane.refetch })), statusError && cpError && (_jsx(SectionError, { message: `Failed to load system data. ${status.error ?? ""}`, onRetry: () => { status.refetch(); controlPlane.refetch(); } })), status.data && controlPlane.data && (_jsx(HealthBanner, { maintainer: status.data, controlPlane: controlPlane.data })), cyclesLoading && _jsx(SectionSkeleton, { label: "maintenance cycles" }), cyclesError && (_jsx(SectionError, { message: `Failed to load maintenance cycles. ${cyclesData.error ?? ""}`, onRetry: cyclesData.refetch })), cyclesData.data && (_jsx(MaintenanceCyclesSection, { cycles: cyclesData.data.cycles, totalCycles: cyclesData.data.total_cycles })), cpLoading && _jsx(SectionSkeleton, { label: "promotion funnel" }), cpError && !controlPlane.data && (_jsx(SectionError, { message: `Failed to load agent data. ${controlPlane.error ?? ""}`, onRetry: controlPlane.refetch })), controlPlane.data && (_jsx(PromotionFunnelSection, { agents: controlPlane.data.agents ?? [] })), cpLoading && _jsx(SectionSkeleton, { label: "benchmark freshness" }), controlPlane.data && (_jsx(BenchmarkFreshnessSection, { agents: controlPlane.data.agents ?? [], freshnessBuckets: controlPlane.data.freshness_buckets ?? [], coverageGaps: controlPlane.data.coverage_gaps ?? [], recentRuns: controlPlane.data.recent_runs ?? [] })), cpLoading && _jsx(SectionSkeleton, { label: "attention queue" }), controlPlane.data && (_jsx(AttentionQueueSection, { items: controlPlane.data.attention_items ?? [] }))] })] }));
}
