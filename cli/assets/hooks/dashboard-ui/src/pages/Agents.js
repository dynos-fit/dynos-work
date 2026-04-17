import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useMemo } from "react";
import { motion } from "motion/react";
import { AlertTriangle, Bot, Compass, GitBranch, TestTube2, TrendingDown, TrendingUp, } from "lucide-react";
import { Bar, BarChart, CartesianGrid, Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis, } from "recharts";
import { usePollingData } from "@/data/hooks";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { MetricCard } from "@/components/MetricCard";
import { ChartCard } from "@/components/ChartCard";
const MODE_COLORS = {
    replace: "#BDF000",
    alongside: "#2DD4A8",
    shadow: "#B47AFF",
};
const RECOMMENDATION_COLORS = {
    promote: "#BDF000",
    keep: "#2DD4A8",
    replace: "#BDF000",
    alongside: "#2DD4A8",
    shadow: "#B47AFF",
    demote: "#FF3B3B",
    reject: "#FF6D00",
};
const DEFAULT_MODE_COLOR = "#7A776E";
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
function getModeColor(mode) {
    return MODE_COLORS[mode] ?? DEFAULT_MODE_COLOR;
}
function formatDelta(delta) {
    return delta > 0 ? `+${delta.toFixed(2)}` : delta.toFixed(2);
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
function formatCount(value) {
    if (value >= 1000)
        return `${(value / 1000).toFixed(1)}K`;
    return `${value}`;
}
function shortenLabel(label, max = 18) {
    return label.length > max ? `${label.slice(0, max - 3)}...` : label;
}
function freshnessBucket(offset) {
    if (offset === undefined || offset === null || offset < 0)
        return "unknown";
    if (offset === 0)
        return "current";
    if (offset <= 5)
        return "1-5";
    if (offset <= 10)
        return "6-10";
    return "10+";
}
function statusNeedsAttention(agent) {
    return !agent.route_allowed
        || agent.status.includes("demoted")
        || !agent.benchmark_summary
        || (agent.last_benchmarked_task_offset ?? 0) > 10
        || agent.last_evaluation?.blocked_by_category != null;
}
function compareByCount(a, b) {
    return b[1] - a[1] || a[0].localeCompare(b[0]);
}
function DataTable({ headers, rows, empty, }) {
    if (rows.length === 0) {
        return _jsx("p", { className: "text-slate-600 font-mono text-xs py-8 text-center", children: empty });
    }
    return (_jsx("div", { className: "overflow-x-auto", children: _jsxs("table", { className: "w-full font-mono text-xs", children: [_jsx("thead", { children: _jsx("tr", { className: "border-b border-white/10", children: headers.map((header) => (_jsx("th", { className: "text-left text-slate-500 py-2 pr-4 uppercase tracking-wider", children: header }, header))) }) }), _jsx("tbody", { children: rows.map((row, rowIndex) => (_jsx("tr", { className: "border-b border-white/5", children: row.map((cell, cellIndex) => (_jsx("td", { className: `py-2 pr-4 align-top ${cellIndex === row.length - 1 ? "text-slate-300" : "text-slate-400"}`, children: cell }, `${rowIndex}-${cellIndex}`))) }, `${row.join("-")}-${rowIndex}`))) })] }) }));
}
function SkeletonCards() {
    return (_jsxs("div", { className: "space-y-6", role: "status", "aria-label": "Loading agents", children: [_jsx("div", { className: "grid grid-cols-2 lg:grid-cols-5 gap-3", children: Array.from({ length: 5 }).map((_, i) => (_jsxs("div", { className: "border border-white/5 bg-[#0F1114]/60 backdrop-blur-md p-5 rounded-2xl", children: [_jsx(Skeleton, { className: "h-3 w-20 mb-3" }), _jsx(Skeleton, { className: "h-8 w-16" })] }, i))) }), _jsx("div", { className: "grid grid-cols-1 xl:grid-cols-2 gap-6", children: Array.from({ length: 4 }).map((_, i) => (_jsxs("div", { className: "border border-white/5 bg-[#0F1114]/60 backdrop-blur-md p-6 rounded-2xl", children: [_jsx(Skeleton, { className: "h-4 w-40 mb-4" }), _jsx(Skeleton, { className: "h-64 w-full" })] }, i))) }), _jsx("div", { className: "grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-6", children: Array.from({ length: 6 }).map((_, i) => (_jsxs("div", { className: "border border-white/5 bg-[#0F1114]/60 backdrop-blur-md p-6 rounded-2xl", children: [_jsx(Skeleton, { className: "h-4 w-32 mb-3" }), _jsx(Skeleton, { className: "h-7 w-48 mb-6" }), _jsx(Skeleton, { className: "h-28 w-full mb-4" }), _jsx(Skeleton, { className: "h-20 w-full" })] }, i))) })] }));
}
function ErrorCard({ message, onRetry }) {
    return (_jsxs("div", { className: "border border-red-500/30 bg-red-500/10 backdrop-blur-md p-8 text-center max-w-md mx-auto rounded-2xl", role: "alert", children: [_jsx("div", { className: "text-red-400 font-mono text-sm mb-2", children: "SYSTEM ERROR" }), _jsx("p", { className: "text-slate-400 text-sm mb-6", children: message }), _jsx("button", { onClick: onRetry, className: "px-4 py-2 bg-[#BDF000]/5 hover:bg-[#BDF000]/20 text-[#BDF000] border border-[#BDF000]/20 font-mono text-xs transition-colors rounded-xl", "aria-label": "Retry loading agents", children: "RETRY" })] }));
}
function EmptyState() {
    return (_jsxs("div", { className: "text-center py-16 max-w-md mx-auto", role: "status", children: [_jsx(Bot, { className: "w-12 h-12 text-slate-600 mx-auto mb-4", "aria-hidden": "true" }), _jsx("p", { className: "text-slate-400 font-mono text-sm", children: "No learned agents registered" }), _jsx("p", { className: "text-slate-600 font-mono text-xs mt-2", children: "Agents are created automatically when tasks complete and patterns are learned." })] }));
}
function MetricBar({ label, value, color, }) {
    return (_jsxs("div", { children: [_jsxs("div", { className: "flex items-center justify-between text-[10px] font-mono text-slate-500 uppercase tracking-wider mb-1", children: [_jsx("span", { children: label }), _jsx("span", { className: "text-slate-300", children: value.toFixed(2) })] }), _jsx("div", { className: "h-2 rounded-full bg-white/5 overflow-hidden", children: _jsx("div", { className: "h-full rounded-full", style: { width: `${Math.max(5, Math.min(100, value * 100))}%`, backgroundColor: color } }) })] }));
}
function BaselineCandidateComparison({ agent, }) {
    if (!agent.benchmark_summary || !agent.baseline_summary)
        return null;
    const rows = [
        {
            label: "quality",
            baseline: agent.baseline_summary.mean_quality,
            candidate: agent.benchmark_summary.mean_quality,
        },
        {
            label: "cost",
            baseline: agent.baseline_summary.mean_cost,
            candidate: agent.benchmark_summary.mean_cost,
        },
        {
            label: "efficiency",
            baseline: agent.baseline_summary.mean_efficiency,
            candidate: agent.benchmark_summary.mean_efficiency,
        },
        {
            label: "composite",
            baseline: agent.baseline_summary.mean_composite,
            candidate: agent.benchmark_summary.mean_composite,
        },
    ];
    return (_jsxs("div", { className: "pt-4 border-t border-white/5 space-y-2", children: [_jsx("div", { className: "text-[10px] font-mono text-slate-500 uppercase tracking-wider", children: "Baseline vs Candidate" }), rows.map((row) => {
                const maxValue = Math.max(row.baseline, row.candidate, 0.01);
                return (_jsxs("div", { className: "grid grid-cols-[56px_1fr_40px_1fr_40px] gap-2 items-center", children: [_jsx("span", { className: "text-[10px] font-mono text-slate-500 uppercase", children: row.label }), _jsx("div", { className: "h-2 rounded-full bg-white/5 overflow-hidden", children: _jsx("div", { className: "h-full rounded-full bg-[#5A5A5A]", style: { width: `${Math.max(5, (row.baseline / maxValue) * 100)}%` } }) }), _jsx("span", { className: "text-[10px] font-mono text-slate-500 text-right", children: row.baseline.toFixed(2) }), _jsx("div", { className: "h-2 rounded-full bg-white/5 overflow-hidden", children: _jsx("div", { className: "h-full rounded-full", style: { width: `${Math.max(5, (row.candidate / maxValue) * 100)}%`, backgroundColor: getModeColor(agent.mode) } }) }), _jsx("span", { className: "text-[10px] font-mono text-slate-300 text-right", children: row.candidate.toFixed(2) })] }, row.label));
            })] }));
}
function AgentCard({ agent, index }) {
    const modeColor = getModeColor(agent.mode);
    const hasBenchmark = agent.benchmark_summary != null;
    const evaluation = agent.last_evaluation;
    const blockedBy = evaluation?.blocked_by_category;
    const routeLabel = agent.route_allowed ? "routeable" : "blocked";
    const freshness = freshnessBucket(agent.last_benchmarked_task_offset);
    return (_jsxs(motion.div, { initial: { opacity: 0, y: 20 }, animate: { opacity: 1, y: 0 }, transition: { delay: Math.min(index * 0.04, 0.24), duration: 0.35 }, className: "border border-white/5 bg-[#0F1114]/60 backdrop-blur-md p-6 rounded-2xl relative card-hover-glow", role: "article", "aria-label": `Agent: ${agent.agent_name}`, children: [_jsx("div", { className: "absolute top-0 right-0 w-8 h-8 border-t border-r rounded-tr-2xl", style: { borderColor: `${modeColor}4D` }, "aria-hidden": "true" }), _jsxs("div", { className: "flex items-start justify-between gap-3 mb-5", children: [_jsxs("div", { className: "min-w-0", children: [_jsxs("div", { className: "text-[10px] text-slate-500 font-mono tracking-widest mb-1 flex items-center gap-2 uppercase", children: [_jsx(Bot, { className: "w-3 h-3 shrink-0", "aria-hidden": "true" }), _jsxs("span", { className: "truncate", children: [agent.item_kind, " / ", agent.task_type] })] }), _jsx("h2", { className: "text-xl font-medium tracking-wide text-slate-200 truncate", title: agent.agent_name, children: agent.agent_name }), _jsxs("div", { className: "text-xs text-slate-400 font-mono mt-1 truncate", children: ["role: ", agent.role] })] }), _jsxs("div", { className: "flex flex-wrap gap-2 justify-end", children: [_jsx(Badge, { variant: "outline", className: "font-mono text-[10px] uppercase", style: { borderColor: `${modeColor}55`, color: modeColor }, children: agent.mode }), _jsx(Badge, { variant: "outline", className: `font-mono text-[10px] uppercase ${agent.route_allowed ? "text-[#2DD4A8]" : "text-[#FF9F43]"}`, children: routeLabel })] })] }), _jsxs("div", { className: "space-y-4", children: [_jsxs("div", { className: "grid grid-cols-2 gap-3", children: [_jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-4", children: [_jsx("div", { className: "text-[10px] text-slate-500 font-mono uppercase tracking-wider mb-2", children: "Composite" }), _jsx("div", { className: "text-2xl font-mono", style: { color: modeColor }, children: hasBenchmark ? agent.benchmark_summary.mean_composite.toFixed(2) : "--" }), _jsx("div", { className: "text-[10px] text-slate-600 font-mono mt-2", children: hasBenchmark ? `${agent.benchmark_summary.sample_count} benchmark samples` : "No benchmark data yet" })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-4", children: [_jsx("div", { className: "text-[10px] text-slate-500 font-mono uppercase tracking-wider mb-2", children: "Freshness" }), _jsx("div", { className: "text-2xl font-mono text-slate-200", children: freshness }), _jsxs("div", { className: "text-[10px] text-slate-600 font-mono mt-2", children: ["task offset: ", agent.last_benchmarked_task_offset ?? "n/a"] })] })] }), hasBenchmark && (_jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-4 space-y-3", children: [_jsx("div", { className: "text-[10px] text-slate-500 font-mono uppercase tracking-wider", children: "Benchmark Profile" }), _jsx(MetricBar, { label: "quality", value: agent.benchmark_summary.mean_quality, color: "#2DD4A8" }), _jsx(MetricBar, { label: "cost", value: agent.benchmark_summary.mean_cost, color: "#FF9F43" }), _jsx(MetricBar, { label: "efficiency", value: agent.benchmark_summary.mean_efficiency, color: "#B47AFF" })] })), _jsx(BaselineCandidateComparison, { agent: agent }), _jsxs("div", { className: "grid grid-cols-2 gap-3 text-xs font-mono", children: [_jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-3", children: [_jsx("div", { className: "text-slate-500 uppercase tracking-wider mb-1", children: "Source" }), _jsx("div", { className: "text-slate-300 break-all", children: agent.source || "n/a" })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-3", children: [_jsx("div", { className: "text-slate-500 uppercase tracking-wider mb-1", children: "Generated From" }), _jsx("div", { className: "text-slate-300 break-all", children: agent.generated_from || "n/a" })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-3", children: [_jsx("div", { className: "text-slate-500 uppercase tracking-wider mb-1", children: "Generated At" }), _jsx("div", { className: "text-slate-300", children: formatDate(agent.generated_at) })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-3", children: [_jsx("div", { className: "text-slate-500 uppercase tracking-wider mb-1", children: "Status" }), _jsx("div", { className: agent.status.includes("demoted") ? "text-[#FF3B3B]" : "text-slate-300", children: agent.status })] })] }), evaluation && (_jsxs("div", { className: "pt-4 border-t border-white/5 space-y-3", children: [_jsxs("div", { className: "flex items-center justify-between gap-3", children: [_jsx(Badge, { variant: "outline", className: "font-mono text-[10px] uppercase", style: { color: RECOMMENDATION_COLORS[evaluation.recommendation] ?? "#C8C4B8" }, children: evaluation.recommendation }), _jsxs("div", { className: "text-sm font-mono flex items-center gap-1", style: { color: evaluation.delta_composite >= 0 ? "#2DD4A8" : "#FF3B3B" }, children: [evaluation.delta_composite >= 0 ? (_jsx(TrendingUp, { className: "w-3 h-3", "aria-hidden": "true" })) : (_jsx(TrendingDown, { className: "w-3 h-3", "aria-hidden": "true" })), formatDelta(evaluation.delta_composite)] })] }), _jsxs("div", { className: "grid grid-cols-2 gap-3 text-xs font-mono", children: [_jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-3", children: [_jsx("div", { className: "text-slate-500 uppercase tracking-wider mb-1", children: "Delta Quality" }), _jsx("div", { className: evaluation.delta_quality >= 0 ? "text-[#2DD4A8]" : "text-[#FF3B3B]", children: formatDelta(evaluation.delta_quality) })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-3", children: [_jsx("div", { className: "text-slate-500 uppercase tracking-wider mb-1", children: "Blocked By" }), _jsx("div", { className: blockedBy ? "text-[#FF9F43]" : "text-slate-300", children: blockedBy ?? "none" })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-3", children: [_jsx("div", { className: "text-slate-500 uppercase tracking-wider mb-1", children: "Fixture" }), _jsx("div", { className: "text-slate-300 break-all", children: evaluation.fixture_id || "n/a" })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-3", children: [_jsx("div", { className: "text-slate-500 uppercase tracking-wider mb-1", children: "Source Tasks" }), _jsx("div", { className: "text-slate-300", children: evaluation.source_tasks.length })] })] })] }))] })] }));
}
export default function Agents() {
    const { data, loading, error, refetch } = usePollingData("/api/agents");
    const isInitialLoad = loading && data === null;
    const isError = error !== null && data === null;
    const isEmpty = !loading && !error && data !== null && data.length === 0;
    const hasData = data !== null && data.length > 0;
    const isStaleError = error !== null && data !== null;
    const summaryCards = useMemo(() => {
        if (!data)
            return [];
        const benchmarked = data.filter((agent) => agent.benchmark_summary != null);
        return [
            {
                label: "Total Agents",
                value: data.length,
                icon: Bot,
                color: "#BDF000",
            },
            {
                label: "Routeable",
                value: data.filter((agent) => agent.route_allowed).length,
                icon: GitBranch,
                color: "#2DD4A8",
            },
            {
                label: "Benchmarked",
                value: benchmarked.length,
                icon: TestTube2,
                color: "#B47AFF",
            },
            {
                label: "Stale Benchmarks",
                value: data.filter((agent) => (agent.last_benchmarked_task_offset ?? 0) > 10).length,
                icon: Compass,
                color: "#FF9F43",
            },
            {
                label: "Demoted",
                value: data.filter((agent) => agent.status.includes("demoted")).length,
                icon: AlertTriangle,
                color: "#FF3B3B",
            },
        ];
    }, [data]);
    const modeData = useMemo(() => {
        if (!data)
            return [];
        const counts = new Map();
        for (const agent of data) {
            counts.set(agent.mode, (counts.get(agent.mode) ?? 0) + 1);
        }
        return Array.from(counts.entries())
            .sort(compareByCount)
            .map(([name, value]) => ({ name, value, color: getModeColor(name) }));
    }, [data]);
    const roleData = useMemo(() => {
        if (!data)
            return [];
        const counts = new Map();
        for (const agent of data) {
            counts.set(agent.role, (counts.get(agent.role) ?? 0) + 1);
        }
        return Array.from(counts.entries())
            .sort(compareByCount)
            .map(([role, count]) => ({ role, count }));
    }, [data]);
    const taskTypeData = useMemo(() => {
        if (!data)
            return [];
        const counts = new Map();
        for (const agent of data) {
            counts.set(agent.task_type, (counts.get(agent.task_type) ?? 0) + 1);
        }
        return Array.from(counts.entries())
            .sort(compareByCount)
            .slice(0, 8)
            .map(([taskType, count]) => ({ taskType: shortenLabel(taskType, 20), count }));
    }, [data]);
    const benchmarkProfileData = useMemo(() => {
        if (!data)
            return [];
        const benchmarked = data.filter((agent) => agent.benchmark_summary != null);
        if (benchmarked.length === 0)
            return [];
        const totals = benchmarked.reduce((acc, agent) => {
            acc.quality += agent.benchmark_summary.mean_quality;
            acc.cost += agent.benchmark_summary.mean_cost;
            acc.efficiency += agent.benchmark_summary.mean_efficiency;
            acc.composite += agent.benchmark_summary.mean_composite;
            return acc;
        }, { quality: 0, cost: 0, efficiency: 0, composite: 0 });
        return [
            { metric: "quality", value: totals.quality / benchmarked.length, color: "#2DD4A8" },
            { metric: "cost", value: totals.cost / benchmarked.length, color: "#FF9F43" },
            { metric: "efficiency", value: totals.efficiency / benchmarked.length, color: "#B47AFF" },
            { metric: "composite", value: totals.composite / benchmarked.length, color: "#BDF000" },
        ];
    }, [data]);
    const freshnessData = useMemo(() => {
        if (!data)
            return [];
        const counts = new Map();
        for (const agent of data) {
            const bucket = agent.benchmark_summary ? freshnessBucket(agent.last_benchmarked_task_offset) : "unbenchmarked";
            counts.set(bucket, (counts.get(bucket) ?? 0) + 1);
        }
        const order = ["current", "1-5", "6-10", "10+", "unbenchmarked", "unknown"];
        return order
            .map((bucket) => ({ bucket, count: counts.get(bucket) ?? 0 }))
            .filter((entry) => entry.count > 0);
    }, [data]);
    const recommendationData = useMemo(() => {
        if (!data)
            return [];
        const counts = new Map();
        for (const agent of data) {
            const recommendation = agent.last_evaluation?.recommendation;
            if (!recommendation)
                continue;
            counts.set(recommendation, (counts.get(recommendation) ?? 0) + 1);
        }
        return Array.from(counts.entries())
            .sort(compareByCount)
            .map(([name, value]) => ({
            name,
            value,
            color: RECOMMENDATION_COLORS[name] ?? "#7A776E",
        }));
    }, [data]);
    const blockedCategoryData = useMemo(() => {
        if (!data)
            return [];
        const counts = new Map();
        for (const agent of data) {
            const blocked = agent.last_evaluation?.blocked_by_category;
            if (!blocked)
                continue;
            counts.set(blocked, (counts.get(blocked) ?? 0) + 1);
        }
        return Array.from(counts.entries())
            .sort(compareByCount)
            .map(([category, count]) => ({ category: shortenLabel(category, 22), count }));
    }, [data]);
    const topMoversRows = useMemo(() => {
        if (!data)
            return [];
        return [...data]
            .filter((agent) => agent.last_evaluation != null)
            .sort((a, b) => Math.abs(b.last_evaluation.delta_composite) - Math.abs(a.last_evaluation.delta_composite))
            .slice(0, 8)
            .map((agent) => [
            agent.agent_name,
            agent.role,
            agent.last_evaluation.recommendation,
            formatDelta(agent.last_evaluation.delta_composite),
        ]);
    }, [data]);
    const attentionRows = useMemo(() => {
        if (!data)
            return [];
        return data
            .filter(statusNeedsAttention)
            .sort((a, b) => {
            const aWeight = Number(a.status.includes("demoted")) * 10 + Number(!a.route_allowed) * 5 + (a.last_benchmarked_task_offset ?? 0);
            const bWeight = Number(b.status.includes("demoted")) * 10 + Number(!b.route_allowed) * 5 + (b.last_benchmarked_task_offset ?? 0);
            return bWeight - aWeight;
        })
            .slice(0, 8)
            .map((agent) => [
            agent.agent_name,
            agent.status,
            agent.route_allowed ? "routeable" : "blocked",
            agent.benchmark_summary ? `offset ${agent.last_benchmarked_task_offset}` : "no benchmark",
        ]);
    }, [data]);
    return (_jsxs("div", { className: "p-8 h-full flex flex-col", children: [_jsxs("header", { className: "mb-8", children: [_jsx("h1", { className: "text-3xl font-mono font-light tracking-[0.2em] text-[#BDF000]", children: "AGENTS" }), _jsx("p", { className: "text-slate-500 font-mono text-xs mt-2", children: "// LEARNED AGENT PORTFOLIO, BENCHMARK HEALTH, AND EVALUATION POSTURE" })] }), isStaleError && hasData && (_jsxs("div", { className: "mb-4 px-4 py-2 border border-red-500/30 bg-red-500/10 text-red-400 text-xs font-mono flex items-center justify-between rounded-xl", role: "alert", children: [_jsx("span", { children: "Connection issue: displaying cached data" }), _jsx("button", { onClick: refetch, className: "text-[#BDF000] hover:underline ml-4", "aria-label": "Retry connection", children: "RETRY" })] })), isInitialLoad && _jsx(SkeletonCards, {}), isError && !hasData && (_jsx(ErrorCard, { message: "Unable to load agent data. Check that the daemon is running.", onRetry: refetch })), isEmpty && _jsx(EmptyState, {}), hasData && (_jsxs("div", { className: "space-y-6 flex-1 overflow-auto", children: [_jsx("div", { className: "grid grid-cols-2 lg:grid-cols-5 gap-3", children: summaryCards.map((card, index) => {
                            const Icon = card.icon;
                            return (_jsx(MetricCard, { label: card.label, value: card.value, trend: null, icon: _jsx(Icon, { className: "w-3.5 h-3.5", style: { color: card.color }, "aria-hidden": "true" }), delay: index * 0.05 }, card.label));
                        }) }), _jsxs("div", { className: "grid grid-cols-1 xl:grid-cols-2 gap-6", children: [_jsx(ChartCard, { title: "Mode Distribution", subtitle: "Portfolio split by current routing mode.", children: _jsx(ResponsiveContainer, { width: "100%", height: PIE_HEIGHT, children: _jsxs(PieChart, { children: [_jsx(Pie, { data: modeData, dataKey: "value", nameKey: "name", cx: "50%", cy: "44%", innerRadius: "45%", outerRadius: "70%", paddingAngle: 2, label: false, children: modeData.map((entry) => (_jsx(Cell, { fill: entry.color }, entry.name))) }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE, formatter: (value) => [formatCount(value), "Agents"] }), _jsx(Legend, { wrapperStyle: { fontFamily: "JetBrains Mono", fontSize: 11, color: "#999" } })] }) }) }), _jsx(ChartCard, { title: "Role Distribution", subtitle: "How learned agents are distributed across executor and auditor roles.", children: _jsx(ResponsiveContainer, { width: "100%", height: CHART_HEIGHT, children: _jsxs(BarChart, { data: roleData, margin: CHART_MARGIN, children: [_jsx(CartesianGrid, { stroke: "#333", strokeDasharray: "3 3", vertical: false }), _jsx(XAxis, { dataKey: "role", tick: AXIS_TICK_STYLE, axisLine: { stroke: "#333" }, tickLine: false, angle: -30, textAnchor: "end", height: 68, tickFormatter: (value) => shortenLabel(value) }), _jsx(YAxis, { tick: AXIS_TICK_STYLE, axisLine: { stroke: "#333" }, tickLine: false, allowDecimals: false }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE, formatter: (value) => [formatCount(value), "Agents"] }), _jsx(Bar, { dataKey: "count", radius: [4, 4, 0, 0], fill: "#2DD4A8" })] }) }) })] }), _jsxs("div", { className: "grid grid-cols-1 xl:grid-cols-2 gap-6", children: [_jsx(ChartCard, { title: "Benchmark Profile", subtitle: "Average benchmark dimensions across benchmarked learned agents.", children: _jsx(ResponsiveContainer, { width: "100%", height: CHART_HEIGHT, children: _jsxs(BarChart, { data: benchmarkProfileData, margin: CHART_MARGIN, children: [_jsx(CartesianGrid, { stroke: "#333", strokeDasharray: "3 3", vertical: false }), _jsx(XAxis, { dataKey: "metric", tick: AXIS_TICK_STYLE, axisLine: { stroke: "#333" }, tickLine: false }), _jsx(YAxis, { domain: [0, 1], tick: AXIS_TICK_STYLE, axisLine: { stroke: "#333" }, tickLine: false }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE, formatter: (value) => [value.toFixed(3), "Average"] }), _jsx(Bar, { dataKey: "value", radius: [4, 4, 0, 0], children: benchmarkProfileData.map((entry) => (_jsx(Cell, { fill: entry.color }, entry.metric))) })] }) }) }), _jsx(ChartCard, { title: "Benchmark Freshness", subtitle: "How stale the current benchmark snapshot is by task offset.", children: _jsx(ResponsiveContainer, { width: "100%", height: CHART_HEIGHT, children: _jsxs(BarChart, { data: freshnessData, margin: CHART_MARGIN, children: [_jsx(CartesianGrid, { stroke: "#333", strokeDasharray: "3 3", vertical: false }), _jsx(XAxis, { dataKey: "bucket", tick: AXIS_TICK_STYLE, axisLine: { stroke: "#333" }, tickLine: false }), _jsx(YAxis, { tick: AXIS_TICK_STYLE, axisLine: { stroke: "#333" }, tickLine: false, allowDecimals: false }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE, formatter: (value) => [formatCount(value), "Agents"] }), _jsx(Bar, { dataKey: "count", radius: [4, 4, 0, 0], fill: "#FF9F43" })] }) }) })] }), _jsxs("div", { className: "grid grid-cols-1 xl:grid-cols-2 gap-6", children: [_jsx(ChartCard, { title: "Recommendation Mix", subtitle: "Most recent evaluation recommendations across learned agents.", children: _jsx(ResponsiveContainer, { width: "100%", height: PIE_HEIGHT, children: _jsxs(PieChart, { children: [_jsx(Pie, { data: recommendationData, dataKey: "value", nameKey: "name", cx: "50%", cy: "44%", innerRadius: "45%", outerRadius: "70%", paddingAngle: 2, label: false, children: recommendationData.map((entry) => (_jsx(Cell, { fill: entry.color }, entry.name))) }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE, formatter: (value) => [formatCount(value), "Evaluations"] }), _jsx(Legend, { wrapperStyle: { fontFamily: "JetBrains Mono", fontSize: 11, color: "#999" } })] }) }) }), _jsx(ChartCard, { title: "Blocked Categories", subtitle: "Reasons learned agents were blocked during their latest evaluation.", children: blockedCategoryData.length === 0 ? (_jsx("p", { className: "text-slate-600 font-mono text-xs py-8 text-center", children: "No blocked evaluation categories recorded." })) : (_jsx(ResponsiveContainer, { width: "100%", height: CHART_HEIGHT, children: _jsxs(BarChart, { data: blockedCategoryData, margin: CHART_MARGIN, children: [_jsx(CartesianGrid, { stroke: "#333", strokeDasharray: "3 3", vertical: false }), _jsx(XAxis, { dataKey: "category", tick: AXIS_TICK_STYLE, axisLine: { stroke: "#333" }, tickLine: false, angle: -30, textAnchor: "end", height: 68 }), _jsx(YAxis, { tick: AXIS_TICK_STYLE, axisLine: { stroke: "#333" }, tickLine: false, allowDecimals: false }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE, formatter: (value) => [formatCount(value), "Agents"] }), _jsx(Bar, { dataKey: "count", radius: [4, 4, 0, 0], fill: "#FF3B3B" })] }) })) })] }), _jsxs("div", { className: "grid grid-cols-1 xl:grid-cols-2 gap-6", children: [_jsx(ChartCard, { title: "Top Movers", subtitle: "Largest absolute evaluation deltas across learned agents.", children: _jsx(DataTable, { headers: ["Agent", "Role", "Recommendation", "Delta"], rows: topMoversRows, empty: "No evaluated agents yet." }) }), _jsx(ChartCard, { title: "Needs Attention", subtitle: "Agents that are demoted, blocked, stale, or still missing benchmark data.", children: _jsx(DataTable, { headers: ["Agent", "Status", "Route", "Freshness"], rows: attentionRows, empty: "No agents need attention right now." }) })] }), _jsx(ChartCard, { title: "Task Type Coverage", subtitle: "Top learned-agent task types represented in the current portfolio.", children: _jsx(ResponsiveContainer, { width: "100%", height: CHART_HEIGHT, children: _jsxs(BarChart, { data: taskTypeData, margin: CHART_MARGIN, children: [_jsx(CartesianGrid, { stroke: "#333", strokeDasharray: "3 3", vertical: false }), _jsx(XAxis, { dataKey: "taskType", tick: AXIS_TICK_STYLE, axisLine: { stroke: "#333" }, tickLine: false, angle: -25, textAnchor: "end", height: 62 }), _jsx(YAxis, { tick: AXIS_TICK_STYLE, axisLine: { stroke: "#333" }, tickLine: false, allowDecimals: false }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE, formatter: (value) => [formatCount(value), "Agents"] }), _jsx(Bar, { dataKey: "count", radius: [4, 4, 0, 0], fill: "#BDF000" })] }) }) }), _jsx("div", { className: "grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-6", children: data.map((agent, idx) => (_jsx(AgentCard, { agent: agent, index: idx }, `${agent.agent_name}-${agent.role}-${agent.task_type}`))) })] }))] }));
}
