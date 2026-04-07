import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
/**
 * Analytics page — /analytics
 * Scrollable page with 10 Recharts charts powered by GET /api/retrospectives,
 * plus a Costs tab powered by GET /api/cost-summary.
 *
 * Tabs: CHARTS | COSTS
 *
 * Charts tab (10 charts):
 *   1. Quality Trend (LineChart, full width)
 *   2. Cost Score Trend (LineChart, full width)
 *   3. Agent-Model Assignment Mix (PieChart, half width)
 *   4. Executor Repair Frequency (BarChart, half width)
 *   5. Spawn Efficiency (LineChart, full width, dual lines)
 *   6. Token Cost Breakdown (stacked BarChart, full width)
 *   6b. Token I/O per Task (stacked BarChart, input vs output, full width)
 *   7. Findings Per Task (BarChart, full width)
 *   8. Repair Intensity vs Findings (LineChart, full width)
 *   9. Routing Distribution (PieChart, half width — paired with empty half)
 *
 * Costs tab:
 *   - Cost summary table by model
 *   - By-agent token breakdown
 *   - Editable pricing rates with client-side recalculation
 *
 * States: loading (skeleton), empty/insufficient (<2 retros), error (retry), success.
 */
import { useMemo, useState, useCallback } from "react";
import { motion } from "motion/react";
import { TrendingUp, BarChart3, DollarSign, Activity, GitBranch, FileCode2, ShieldCheck, AlertTriangle, FlaskConical, } from "lucide-react";
import { LineChart, Line, BarChart, Bar, PieChart, Pie, Cell, XAxis, YAxis, CartesianGrid, ResponsiveContainer, Tooltip, Legend, } from "recharts";
import { usePollingData } from "@/data/hooks";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { MetricCard } from "@/components/MetricCard";
import { ChartCard } from "@/components/ChartCard";
import { TimeRangeFilter, filterByTimeRange } from "@/components/TimeRangeFilter";
// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const CHART_HEIGHT = 300;
const PIE_CHART_HEIGHT = 320;
const COLORS = {
    quality: "#BDF000",
    cost: "#B47AFF",
    teal: "#2DD4A8",
    red: "#FF3B3B",
    amber: "#FF9F43",
    pink: "#FF4081",
    lime: "#AEEA00",
    orange: "#FF6D00",
    grid: "#333",
    axisTick: "#999",
    tooltipBg: "#1A1F2E",
    tooltipBorder: "#333",
};
const MODEL_COLORS = {
    haiku: "#2DD4A8",
    sonnet: "#BDF000",
    opus: "#B47AFF",
    unknown: "#666",
};
/** Palette for stacked bars — cycles if more agents than colors */
const AGENT_ROLE_COLORS = [
    "#BDF000",
    "#B47AFF",
    "#2DD4A8",
    "#FF4081",
    "#FF9F43",
    "#FF6D00",
    "#AEEA00",
    "#40C4FF",
    "#E040FB",
    "#69F0AE",
];
const ROUTING_COLORS = {
    generic: "#666",
    learned: "#BDF000",
};
const AXIS_TICK_STYLE = {
    fill: "#999",
    fontFamily: "JetBrains Mono",
    fontSize: 11,
};
const TOOLTIP_STYLE = {
    backgroundColor: COLORS.tooltipBg,
    border: `1px solid ${COLORS.tooltipBorder}`,
    borderRadius: 8,
    fontFamily: "JetBrains Mono",
    fontSize: 11,
    color: "#ccc",
};
const CHART_MARGIN = {
    top: 8,
    right: 16,
    left: 0,
    bottom: 8,
};
const TASK_X_AXIS_PROPS = {
    tick: AXIS_TICK_STYLE,
    axisLine: { stroke: COLORS.grid },
    tickLine: { stroke: COLORS.grid },
    tickMargin: 8,
    minTickGap: 24,
    interval: "preserveStartEnd",
};
const NA_LABEL = "N/A";
/** Default pricing per 1M tokens (USD) */
const DEFAULT_RATES = {
    haiku: 0.25,
    sonnet: 3.0,
    opus: 15.0,
};
// ---------------------------------------------------------------------------
// Data transforms
// ---------------------------------------------------------------------------
function sortByTaskId(retros) {
    return [...retros].sort((a, b) => a.task_id.localeCompare(b.task_id));
}
function shortenTaskId(taskId) {
    const parts = taskId.replace("task-", "").split("-");
    if (parts.length >= 2) {
        const datePart = parts[0];
        const seqPart = parts.slice(1).join("-");
        return `${datePart.slice(-4)}-${seqPart}`;
    }
    return taskId;
}
function shortenLabel(label, max = 14) {
    return label.length > max ? `${label.slice(0, max - 3)}...` : label;
}
function buildModelUsageData(retros) {
    const counts = {};
    for (const retro of retros) {
        if (!retro.model_used_by_agent)
            continue;
        for (const model of Object.values(retro.model_used_by_agent)) {
            const key = model ?? "unknown";
            counts[key] = (counts[key] || 0) + 1;
        }
    }
    return Object.entries(counts).map(([name, value]) => ({
        name,
        value,
        color: MODEL_COLORS[name] ?? MODEL_COLORS.unknown,
    }));
}
function buildRepairData(retros) {
    const totals = {};
    for (const retro of retros) {
        if (!retro.executor_repair_frequency)
            continue;
        for (const [executor, count] of Object.entries(retro.executor_repair_frequency)) {
            totals[executor] = (totals[executor] || 0) + count;
        }
    }
    return Object.entries(totals)
        .map(([executor, repairs]) => ({ executor, repairs }))
        .sort((a, b) => b.repairs - a.repairs);
}
// AC-22(a): Token Cost Breakdown — stacked bar chart data
function buildTokenCostData(retros) {
    const agentSet = new Set();
    const data = [];
    for (const retro of retros) {
        const row = {
            task_id: shortenTaskId(retro.task_id),
        };
        if (retro.token_usage_by_agent && Object.keys(retro.token_usage_by_agent).length > 0) {
            for (const [agent, tokens] of Object.entries(retro.token_usage_by_agent)) {
                agentSet.add(agent);
                row[agent] = tokens;
            }
        }
        data.push(row);
    }
    return { data, agentKeys: Array.from(agentSet).sort() };
}
function buildFindingsData(retros) {
    return retros.map((retro) => {
        if (!retro.findings_by_auditor || Object.keys(retro.findings_by_auditor).length === 0) {
            return { task_id: shortenTaskId(retro.task_id), findings: NA_LABEL };
        }
        const total = Object.values(retro.findings_by_auditor).reduce((sum, val) => sum + val, 0);
        return { task_id: shortenTaskId(retro.task_id), findings: total };
    });
}
function buildRepairRateData(retros) {
    return retros.map((retro) => {
        const totalFindings = retro.findings_by_auditor
            ? Object.values(retro.findings_by_auditor).reduce((s, v) => s + v, 0)
            : 0;
        if (totalFindings === 0 || retro.repair_cycle_count === undefined) {
            return { task_id: shortenTaskId(retro.task_id), success_rate: null };
        }
        // success rate = proportion of findings addressed by repairs
        // clamp to 100% — repairs can exceed findings in iterative cycles
        const rate = Math.min((retro.repair_cycle_count / totalFindings) * 100, 100);
        return { task_id: shortenTaskId(retro.task_id), success_rate: Math.round(rate) };
    });
}
function buildRoutingData(retros) {
    const counts = {};
    for (const retro of retros) {
        if (!retro.agent_source)
            continue;
        for (const source of Object.values(retro.agent_source)) {
            const key = typeof source === "string" && source.startsWith("learned:")
                ? "learned"
                : "generic";
            counts[key] = (counts[key] || 0) + 1;
        }
    }
    return Object.entries(counts).map(([name, value]) => ({
        name,
        value,
        color: ROUTING_COLORS[name] ?? "#666",
    }));
}
function buildTokenIOData(retros) {
    return retros.map((retro) => ({
        task_id: shortenTaskId(retro.task_id),
        input_tokens: retro.total_input_tokens ?? 0,
        output_tokens: retro.total_output_tokens ?? 0,
    }));
}
function buildAgentTokenRows(retros) {
    const totals = {};
    for (const retro of retros) {
        if (!retro.token_usage_by_agent)
            continue;
        for (const [agent, tokens] of Object.entries(retro.token_usage_by_agent)) {
            totals[agent] = (totals[agent] || 0) + tokens;
        }
    }
    return Object.entries(totals)
        .map(([agent, tokens]) => ({ agent, tokens }))
        .sort((a, b) => b.tokens - a.tokens);
}
function formatTokens(n) {
    if (n >= 1_000_000)
        return `${(n / 1_000_000).toFixed(2)}M`;
    if (n >= 1_000)
        return `${(n / 1_000).toFixed(1)}K`;
    return String(n);
}
function formatUsd(n) {
    return `$${n.toFixed(4)}`;
}
// ---------------------------------------------------------------------------
// Skeleton placeholders
// ---------------------------------------------------------------------------
function ChartSkeleton({ label }) {
    return (_jsxs("div", { className: "rounded-xl border border-[#BDF000]/10 bg-[#0D1117]/60 p-6", role: "status", "aria-label": `Loading ${label}`, children: [_jsx(Skeleton, { className: "h-4 w-40 mb-4 bg-white/5" }), _jsx(Skeleton, { className: "h-[260px] w-full bg-white/5" })] }));
}
function LoadingState() {
    return (_jsxs("div", { className: "space-y-6", children: [_jsx(ChartSkeleton, { label: "quality trend chart" }), _jsx(ChartSkeleton, { label: "cost trend chart" }), _jsxs("div", { className: "grid grid-cols-1 md:grid-cols-2 gap-6", children: [_jsx(ChartSkeleton, { label: "model usage chart" }), _jsx(ChartSkeleton, { label: "executor repair chart" })] }), _jsx(ChartSkeleton, { label: "spawn efficiency chart" }), _jsx(ChartSkeleton, { label: "token cost breakdown chart" }), _jsx(ChartSkeleton, { label: "findings per task chart" }), _jsx(ChartSkeleton, { label: "repair success rate chart" }), _jsx("div", { className: "grid grid-cols-1 md:grid-cols-2 gap-6", children: _jsx(ChartSkeleton, { label: "routing distribution chart" }) })] }));
}
function CostsLoadingState() {
    return (_jsxs("div", { className: "space-y-6", children: [_jsxs("div", { className: "rounded-xl border border-[#BDF000]/10 bg-[#0D1117]/60 p-6", role: "status", "aria-label": "Loading cost summary", children: [_jsx(Skeleton, { className: "h-4 w-48 mb-4 bg-white/5" }), _jsx(Skeleton, { className: "h-[200px] w-full bg-white/5" })] }), _jsxs("div", { className: "rounded-xl border border-[#BDF000]/10 bg-[#0D1117]/60 p-6", role: "status", "aria-label": "Loading pricing rates", children: [_jsx(Skeleton, { className: "h-4 w-40 mb-4 bg-white/5" }), _jsx(Skeleton, { className: "h-[120px] w-full bg-white/5" })] })] }));
}
// ---------------------------------------------------------------------------
// Empty / Insufficient state
// ---------------------------------------------------------------------------
function InsufficientDataState({ count }) {
    return (_jsxs(motion.div, { initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, className: "flex flex-col items-center justify-center py-20 text-center", role: "status", children: [_jsx(BarChart3, { className: "w-12 h-12 text-slate-600 mb-4", "aria-hidden": "true" }), _jsx("p", { className: "text-slate-400 font-mono text-sm", children: "Insufficient data for charts" }), _jsx("p", { className: "text-slate-600 font-mono text-xs mt-2", children: count === 0
                    ? "No retrospectives found. Complete tasks to generate analytics."
                    : "At least 2 completed task retrospectives are required." })] }));
}
// ---------------------------------------------------------------------------
// Error state
// ---------------------------------------------------------------------------
function ErrorState({ message, onRetry }) {
    return (_jsxs(motion.div, { initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, className: "flex flex-col items-center justify-center py-20 text-center", role: "alert", children: [_jsx("div", { className: "w-12 h-12 rounded-full bg-[#FF3B3B]/10 flex items-center justify-center mb-4", children: _jsx(BarChart3, { className: "w-6 h-6 text-[#FF3B3B]", "aria-hidden": "true" }) }), _jsx("p", { className: "text-slate-400 font-mono text-sm mb-1", children: "Failed to load analytics data" }), _jsx("p", { className: "text-slate-600 font-mono text-xs mb-4", children: message }), _jsx("button", { onClick: onRetry, className: "px-4 py-2 rounded-lg border border-[#BDF000]/20 text-[#BDF000] font-mono text-xs hover:bg-[#BDF000]/10 transition-colors focus:outline-none focus:ring-2 focus:ring-[#BDF000]/40", "aria-label": "Retry loading analytics data", children: "RETRY" })] }));
}
// ---------------------------------------------------------------------------
// Summary row helpers (AC-5)
// ---------------------------------------------------------------------------
const BLENDED_RATE_PER_MILLION = 9;
function formatSummaryTokens(n) {
    if (n >= 1_000_000)
        return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000)
        return `${(n / 1_000).toFixed(1)}K`;
    return String(n);
}
function formatSummaryCost(n) {
    return `$${n.toFixed(2)}`;
}
function formatPercentage(n) {
    return `${(n * 100).toFixed(1)}%`;
}
function computeTrendPercent(latest, priorMean) {
    if (priorMean === 0)
        return null;
    return ((latest - priorMean) / priorMean) * 100;
}
/** Extract a date string from task_id for time-range filtering */
function dateFromTaskId(retro) {
    // task_id format: task-YYYYMMDD-NNN
    const match = retro.task_id.match(/(\d{4})(\d{2})(\d{2})/);
    if (!match)
        return null;
    return `${match[1]}-${match[2]}-${match[3]}`;
}
// ---------------------------------------------------------------------------
// Costs Tab Content
// ---------------------------------------------------------------------------
function CostsTabContent({ retros, }) {
    const { data: costSummary, loading: costLoading, error: costError, refetch: costRefetch, } = usePollingData("/api/cost-summary", 15000);
    const [rates, setRates] = useState({ ...DEFAULT_RATES });
    const handleRateChange = useCallback((model, value) => {
        const parsed = parseFloat(value);
        if (!isNaN(parsed) && parsed >= 0) {
            setRates((prev) => ({ ...prev, [model]: parsed }));
        }
    }, []);
    const agentRows = useMemo(() => buildAgentTokenRows(retros), [retros]);
    // Compute model-level cost from cost summary + overridden rates
    const modelRows = useMemo(() => {
        if (!costSummary?.by_model)
            return [];
        return Object.entries(costSummary.by_model).map(([model, info]) => {
            const inputTokens = info.input_tokens ?? 0;
            const outputTokens = info.output_tokens ?? 0;
            const totalTokens = info.tokens ?? (inputTokens + outputTokens);
            const rateKey = Object.keys(rates).find((k) => model.toLowerCase().includes(k));
            const ratePerMillion = rateKey ? rates[rateKey] : 0;
            const estimatedUsd = (totalTokens / 1_000_000) * ratePerMillion;
            return { model, inputTokens, outputTokens, tokens: totalTokens, estimatedUsd };
        });
    }, [costSummary, rates]);
    const totalTokens = useMemo(() => modelRows.reduce((s, r) => s + r.tokens, 0), [modelRows]);
    const totalUsd = useMemo(() => modelRows.reduce((s, r) => s + r.estimatedUsd, 0), [modelRows]);
    if (costLoading) {
        return _jsx(CostsLoadingState, {});
    }
    if (costError && !costSummary) {
        return _jsx(ErrorState, { message: costError, onRetry: costRefetch });
    }
    if (!costSummary || Object.keys(costSummary.by_model ?? {}).length === 0) {
        return (_jsxs(motion.div, { initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, className: "flex flex-col items-center justify-center py-20 text-center", role: "status", children: [_jsx(DollarSign, { className: "w-12 h-12 text-slate-600 mb-4", "aria-hidden": "true" }), _jsx("p", { className: "text-slate-400 font-mono text-sm", children: "No cost data available" }), _jsx("p", { className: "text-slate-600 font-mono text-xs mt-2", children: "Cost data will appear after tasks generate token usage metrics." })] }));
    }
    return (_jsxs(motion.div, { initial: { opacity: 0, y: 12 }, animate: { opacity: 1, y: 0 }, className: "space-y-6", children: [_jsxs("div", { className: "rounded-xl border border-[#BDF000]/10 bg-[#0D1117]/60 p-6 card-hover-glow", children: [_jsxs("div", { className: "flex items-center gap-2 mb-4", children: [_jsx(DollarSign, { className: "w-4 h-4 text-[#BDF000]", "aria-hidden": true }), _jsx("h3", { className: "font-mono text-xs font-semibold text-slate-300 tracking-wider uppercase", children: "Cost by Model" })] }), _jsx("div", { className: "overflow-x-auto", children: _jsxs("table", { className: "w-full font-mono text-xs", "aria-label": "Cost breakdown by model", children: [_jsx("thead", { children: _jsxs("tr", { className: "border-b border-white/10", children: [_jsx("th", { className: "text-left text-slate-500 py-2 pr-4", children: "Model" }), _jsx("th", { className: "text-right text-slate-500 py-2 pr-4", children: "Input" }), _jsx("th", { className: "text-right text-slate-500 py-2 pr-4", children: "Output" }), _jsx("th", { className: "text-right text-slate-500 py-2 pr-4", children: "Total" }), _jsx("th", { className: "text-right text-slate-500 py-2", children: "Est. USD" })] }) }), _jsx("tbody", { children: modelRows.map((row) => (_jsxs("tr", { className: "border-b border-white/5", children: [_jsx("td", { className: "text-slate-300 py-2 pr-4", children: row.model }), _jsx("td", { className: "text-right text-[#B47AFF] py-2 pr-4", children: formatTokens(row.inputTokens) }), _jsx("td", { className: "text-right text-[#BDF000] py-2 pr-4", children: formatTokens(row.outputTokens) }), _jsx("td", { className: "text-right text-slate-400 py-2 pr-4", children: formatTokens(row.tokens) }), _jsx("td", { className: "text-right text-[#BDF000] py-2", children: formatUsd(row.estimatedUsd) })] }, row.model))) }), _jsx("tfoot", { children: _jsxs("tr", { className: "border-t border-[#BDF000]/20", children: [_jsx("td", { className: "text-slate-300 font-semibold py-2 pr-4", children: "Total" }), _jsx("td", { className: "text-right text-[#B47AFF] font-semibold py-2 pr-4", children: formatTokens(modelRows.reduce((s, r) => s + r.inputTokens, 0)) }), _jsx("td", { className: "text-right text-[#BDF000] font-semibold py-2 pr-4", children: formatTokens(modelRows.reduce((s, r) => s + r.outputTokens, 0)) }), _jsx("td", { className: "text-right text-slate-300 font-semibold py-2 pr-4", children: formatTokens(totalTokens) }), _jsx("td", { className: "text-right text-[#BDF000] font-semibold py-2", children: formatUsd(totalUsd) })] }) })] }) })] }), _jsxs("div", { className: "rounded-xl border border-[#BDF000]/10 bg-[#0D1117]/60 p-6 card-hover-glow", children: [_jsxs("div", { className: "flex items-center gap-2 mb-4", children: [_jsx(Activity, { className: "w-4 h-4 text-[#BDF000]", "aria-hidden": true }), _jsx("h3", { className: "font-mono text-xs font-semibold text-slate-300 tracking-wider uppercase", children: "Tokens by Agent" })] }), agentRows.length === 0 ? (_jsx("p", { className: "text-slate-600 font-mono text-xs", children: "No per-agent token data available." })) : (_jsx("div", { className: "overflow-x-auto", children: _jsxs("table", { className: "w-full font-mono text-xs", "aria-label": "Token usage by agent", children: [_jsx("thead", { children: _jsxs("tr", { className: "border-b border-white/10", children: [_jsx("th", { className: "text-left text-slate-500 py-2 pr-4", children: "Agent" }), _jsx("th", { className: "text-right text-slate-500 py-2", children: "Tokens" })] }) }), _jsx("tbody", { children: agentRows.map((row) => (_jsxs("tr", { className: "border-b border-white/5", children: [_jsx("td", { className: "text-slate-300 py-2 pr-4 max-w-[200px] truncate", title: row.agent, children: row.agent }), _jsx("td", { className: "text-right text-slate-400 py-2", children: formatTokens(row.tokens) })] }, row.agent))) })] }) }))] }), _jsxs("div", { className: "rounded-xl border border-[#BDF000]/10 bg-[#0D1117]/60 p-6 card-hover-glow", children: [_jsxs("div", { className: "flex items-center gap-2 mb-4", children: [_jsx(DollarSign, { className: "w-4 h-4 text-[#B47AFF]", "aria-hidden": true }), _jsx("h3", { className: "font-mono text-xs font-semibold text-slate-300 tracking-wider uppercase", children: "Pricing Rates ($/1M tokens)" })] }), _jsx("div", { className: "grid grid-cols-1 sm:grid-cols-3 gap-4", children: Object.entries(rates).map(([model, rate]) => (_jsxs("div", { children: [_jsx("label", { htmlFor: `rate-${model}`, className: "block text-slate-500 font-mono text-xs mb-1 uppercase tracking-wider", children: model }), _jsx("input", { id: `rate-${model}`, type: "number", min: "0", step: "0.01", value: rate, onChange: (e) => handleRateChange(model, e.target.value), className: "w-full bg-black/40 border border-white/10 text-slate-200 p-2 font-mono text-xs focus:outline-none focus:border-[#BDF000] transition-colors rounded-none", "aria-label": `Pricing rate for ${model} in USD per million tokens` })] }, model))) }), _jsx("p", { className: "text-slate-600 font-mono text-[10px] mt-4 tracking-wider uppercase", children: "Estimates based on default pricing" })] })] }));
}
function formatCompactNumber(n) {
    if (n >= 1_000_000)
        return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000)
        return `${(n / 1_000).toFixed(1)}K`;
    return `${n}`;
}
function formatDecimal(n, digits = 3) {
    return n.toFixed(digits);
}
function sortEntriesDescending(record) {
    return Object.entries(record).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}
function DataTable({ headers, rows, empty, }) {
    if (rows.length === 0) {
        return _jsx("p", { className: "text-slate-600 font-mono text-xs py-8 text-center", children: empty });
    }
    return (_jsx("div", { className: "overflow-x-auto", children: _jsxs("table", { className: "w-full font-mono text-xs", children: [_jsx("thead", { children: _jsx("tr", { className: "border-b border-white/10", children: headers.map((header) => (_jsx("th", { className: "text-left text-slate-500 py-2 pr-4 uppercase tracking-wider", children: header }, header))) }) }), _jsx("tbody", { children: rows.map((row, rowIndex) => (_jsx("tr", { className: "border-b border-white/5", children: row.map((cell, cellIndex) => (_jsx("td", { className: `py-2 pr-4 ${cellIndex === row.length - 1 ? "text-slate-300" : "text-slate-400"}`, children: cell }, `${rowIndex}-${cellIndex}`))) }, `${row.join("-")}-${rowIndex}`))) })] }) }));
}
function RepoTabContent({ report, state, stateError, projectStats, totalTaskCount, }) {
    const taskTypeRows = useMemo(() => sortEntriesDescending(projectStats?.task_counts_by_type ?? {}).map(([taskType, count]) => [taskType, formatCompactNumber(count)]), [projectStats]);
    const executorRows = useMemo(() => sortEntriesDescending(projectStats?.executor_reliability ?? {}).map(([executor, score]) => [executor, `${(score * 100).toFixed(1)}%`]), [projectStats]);
    const preventionRows = useMemo(() => sortEntriesDescending(projectStats?.prevention_rule_frequencies ?? {})
        .slice(0, 8)
        .map(([rule, count]) => [
        rule.length > 80 ? `${rule.slice(0, 77)}...` : rule,
        projectStats?.prevention_rule_executors?.[rule] ?? "unknown",
        count,
    ]), [projectStats]);
    const routeRows = useMemo(() => [...(report?.active_routes ?? [])]
        .sort((a, b) => b.composite - a.composite)
        .slice(0, 8)
        .map((route) => [route.agent_name, route.role, route.task_type, route.composite.toFixed(3)]), [report]);
    const demotionRows = useMemo(() => (report?.demotions ?? [])
        .slice(0, 8)
        .map((demotion) => {
        const delta = typeof demotion.last_evaluation?.delta_composite === "number"
            ? demotion.last_evaluation.delta_composite.toFixed(3)
            : "n/a";
        return [demotion.agent_name, demotion.role, demotion.task_type, delta];
    }), [report]);
    const gapRows = useMemo(() => (report?.coverage_gaps ?? []).slice(0, 8).map((gap) => [gap.target_name, gap.role, gap.task_type, gap.item_kind]), [report]);
    const recentRunRows = useMemo(() => (report?.recent_runs ?? []).slice().reverse().map((run, index) => [
        typeof run.run_id === "string" ? run.run_id : `run-${index + 1}`,
        typeof run.fixture_id === "string" ? run.fixture_id : "n/a",
        typeof run.timestamp === "string"
            ? run.timestamp
            : typeof run.completed_at === "string"
                ? run.completed_at
                : "n/a",
        typeof run.outcome === "string"
            ? run.outcome
            : typeof run.recommendation === "string"
                ? run.recommendation
                : "n/a",
    ]), [report]);
    const findingCategoryRows = useMemo(() => sortEntriesDescending(state?.recent_findings_by_category ?? {}).map(([category, count]) => [category, count]), [state]);
    return (_jsxs("div", { className: "space-y-6", children: [_jsxs("div", { className: "grid grid-cols-2 lg:grid-cols-4 gap-3", children: [_jsx(MetricCard, { label: "Total Tasks", value: formatCompactNumber(totalTaskCount), icon: _jsx(ShieldCheck, { className: "w-3.5 h-3.5 text-[#BDF000]", "aria-hidden": "true" }) }), _jsx(MetricCard, { label: "Active Routes", value: formatCompactNumber(report?.summary.active_routes ?? 0), icon: _jsx(GitBranch, { className: "w-3.5 h-3.5 text-[#2DD4A8]", "aria-hidden": "true" }) }), _jsx(MetricCard, { label: "Tracked Fixtures", value: formatCompactNumber(report?.summary.tracked_fixtures ?? 0), icon: _jsx(FlaskConical, { className: "w-3.5 h-3.5 text-[#B47AFF]", "aria-hidden": "true" }) }), _jsx(MetricCard, { label: "Code Files", value: formatCompactNumber(state?.file_count ?? 0), icon: _jsx(FileCode2, { className: "w-3.5 h-3.5 text-[#FF9F43]", "aria-hidden": "true" }) })] }), _jsxs("div", { className: "grid grid-cols-1 xl:grid-cols-2 gap-6", children: [_jsx(ChartCard, { title: "Repo Runtime Summary", subtitle: "Learned routes, benchmark coverage, queue pressure, and registry freshness.", children: _jsxs("div", { className: "grid grid-cols-2 gap-3", children: [_jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-4", children: [_jsx("div", { className: "text-[10px] text-[#7A776E] uppercase tracking-[0.12em]", children: "Learned Components" }), _jsx("div", { className: "text-xl font-bold text-[#F0F0E8] mt-2", children: formatCompactNumber(report?.summary.learned_components ?? 0) })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-4", children: [_jsx("div", { className: "text-[10px] text-[#7A776E] uppercase tracking-[0.12em]", children: "Shadow Components" }), _jsx("div", { className: "text-xl font-bold text-[#F0F0E8] mt-2", children: formatCompactNumber(report?.summary.shadow_components ?? 0) })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-4", children: [_jsx("div", { className: "text-[10px] text-[#7A776E] uppercase tracking-[0.12em]", children: "Benchmark Runs" }), _jsx("div", { className: "text-xl font-bold text-[#F0F0E8] mt-2", children: formatCompactNumber(report?.summary.benchmark_runs ?? 0) })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-4", children: [_jsx("div", { className: "text-[10px] text-[#7A776E] uppercase tracking-[0.12em]", children: "Automation Queue" }), _jsx("div", { className: "text-xl font-bold text-[#F0F0E8] mt-2", children: formatCompactNumber(report?.summary.queued_automation_jobs ?? 0) })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-4", children: [_jsx("div", { className: "text-[10px] text-[#7A776E] uppercase tracking-[0.12em]", children: "Coverage Gaps" }), _jsx("div", { className: "text-xl font-bold text-[#F0F0E8] mt-2", children: formatCompactNumber(report?.summary.coverage_gaps ?? 0) })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-4", children: [_jsx("div", { className: "text-[10px] text-[#7A776E] uppercase tracking-[0.12em]", children: "Registry Updated" }), _jsx("div", { className: "text-xs font-mono text-slate-300 mt-2 break-all", children: report?.registry_updated_at ?? "n/a" })] })] }) }), _jsx(ChartCard, { title: "Codebase State", subtitle: "Structural complexity, dependency flux, code volume, and recent finding density.", children: state ? (_jsxs("div", { className: "grid grid-cols-2 gap-3", children: [_jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-4", children: [_jsx("div", { className: "text-[10px] text-[#7A776E] uppercase tracking-[0.12em]", children: "Architecture Complexity" }), _jsx("div", { className: "text-xl font-bold text-[#F0F0E8] mt-2", children: formatDecimal(state.architecture_complexity_score, 2) })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-4", children: [_jsx("div", { className: "text-[10px] text-[#7A776E] uppercase tracking-[0.12em]", children: "Dependency Flux" }), _jsx("div", { className: "text-xl font-bold text-[#F0F0E8] mt-2", children: formatDecimal(state.dependency_flux, 2) })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-4", children: [_jsx("div", { className: "text-[10px] text-[#7A776E] uppercase tracking-[0.12em]", children: "Finding Entropy" }), _jsx("div", { className: "text-xl font-bold text-[#F0F0E8] mt-2", children: formatDecimal(state.finding_entropy, 2) })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-4", children: [_jsx("div", { className: "text-[10px] text-[#7A776E] uppercase tracking-[0.12em]", children: "Line Count" }), _jsx("div", { className: "text-xl font-bold text-[#F0F0E8] mt-2", children: formatCompactNumber(state.line_count) })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-4", children: [_jsx("div", { className: "text-[10px] text-[#7A776E] uppercase tracking-[0.12em]", children: "Import Count" }), _jsx("div", { className: "text-xl font-bold text-[#F0F0E8] mt-2", children: formatCompactNumber(state.import_count) })] }), _jsxs("div", { className: "rounded-xl border border-white/6 bg-black/20 p-4", children: [_jsx("div", { className: "text-[10px] text-[#7A776E] uppercase tracking-[0.12em]", children: "Control Flow Count" }), _jsx("div", { className: "text-xl font-bold text-[#F0F0E8] mt-2", children: formatCompactNumber(state.control_flow_count) })] }), _jsxs("div", { className: "col-span-full rounded-xl border border-white/6 bg-black/20 p-4", children: [_jsx("div", { className: "text-[10px] text-[#7A776E] uppercase tracking-[0.12em] mb-3", children: "Dominant Languages" }), _jsx("div", { className: "flex flex-wrap gap-2", children: state.dominant_languages.length > 0 ? state.dominant_languages.map((language) => (_jsx("span", { className: "rounded-full border border-[#BDF000]/20 bg-[#BDF000]/10 px-3 py-1 text-[11px] font-mono text-[#D8E7A0]", children: language }, language))) : (_jsx("span", { className: "text-slate-600 font-mono text-xs", children: "No code files indexed." })) })] })] })) : (_jsxs("div", { className: "flex items-start gap-3 rounded-xl border border-[#FF9F43]/20 bg-[#FF9F43]/5 p-4", children: [_jsx(AlertTriangle, { className: "w-4 h-4 text-[#FF9F43] mt-0.5", "aria-hidden": "true" }), _jsxs("div", { children: [_jsx("div", { className: "text-xs font-mono text-slate-300", children: "Repo state unavailable" }), _jsx("div", { className: "text-[11px] font-mono text-slate-500 mt-1", children: stateError ?? "State metrics are unavailable for the current selection." })] })] })) })] }), _jsxs("div", { className: "grid grid-cols-1 xl:grid-cols-2 gap-6", children: [_jsx(ChartCard, { title: "Task Mix", subtitle: "Retrospective-backed mix of completed tasks, not all task manifests.", children: _jsx(DataTable, { headers: ["Task Type", "Count"], rows: taskTypeRows, empty: "No task type stats available." }) }), _jsx(ChartCard, { title: "Executor Reliability", subtitle: "Retrospective-derived signal based on repair frequency for completed tasks.", children: _jsx(DataTable, { headers: ["Executor", "Reliability"], rows: executorRows, empty: "No executor reliability stats available." }) })] }), _jsxs("div", { className: "grid grid-cols-1 xl:grid-cols-2 gap-6", children: [_jsx(ChartCard, { title: "Active Routes", subtitle: "Highest-performing routeable learned components by composite benchmark score.", children: _jsx(DataTable, { headers: ["Agent", "Role", "Task Type", "Composite"], rows: routeRows, empty: "No active routes available." }) }), _jsx(ChartCard, { title: "Coverage Gaps", subtitle: "Shadow components that do not have benchmark fixtures yet.", children: _jsx(DataTable, { headers: ["Target", "Role", "Task Type", "Kind"], rows: gapRows, empty: "No coverage gaps detected." }) })] }), _jsxs("div", { className: "grid grid-cols-1 xl:grid-cols-2 gap-6", children: [_jsx(ChartCard, { title: "Demotions", subtitle: "Recently demoted components and their last recorded composite delta.", children: _jsx(DataTable, { headers: ["Agent", "Role", "Task Type", "Delta"], rows: demotionRows, empty: "No demotions recorded." }) }), _jsx(ChartCard, { title: "Recent Benchmark Runs", subtitle: "Most recent benchmark activity visible to the runtime report.", children: _jsx(DataTable, { headers: ["Run", "Fixture", "Timestamp", "Outcome"], rows: recentRunRows, empty: "No benchmark runs recorded." }) })] }), _jsxs("div", { className: "grid grid-cols-1 xl:grid-cols-2 gap-6", children: [_jsx(ChartCard, { title: "Recent Findings by Category", subtitle: "Aggregated from the last five retrospectives used by repo state encoding.", children: _jsx(DataTable, { headers: ["Category", "Findings"], rows: findingCategoryRows, empty: "No recent finding categories available." }) }), _jsx(ChartCard, { title: "Prevention Rules", subtitle: "Most frequent prevention rules extracted from completed-task retrospectives.", children: _jsx(DataTable, { headers: ["Rule", "Executor", "Count"], rows: preventionRows, empty: "No prevention rules recorded." }) })] })] }));
}
// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------
export default function Analytics() {
    const { data: tasks, loading: tasksLoading, error: tasksError, refetch: refetchTasks, } = usePollingData("/api/tasks", 10000);
    const { data, loading, error, refetch } = usePollingData("/api/retrospectives", 10000);
    const { data: repoReport, loading: reportLoading, error: reportError, refetch: refetchReport, } = usePollingData("/api/report", 15000);
    const { data: repoState, loading: stateLoading, error: stateError, refetch: refetchState, } = usePollingData("/api/state", 15000);
    const { data: projectStats, loading: statsLoading, error: statsError, refetch: refetchStats, } = usePollingData("/api/project-stats", 15000);
    // Time range filter state (AC-7)
    const [timeRange, setTimeRange] = useState("All");
    // Sort chronologically once
    const sorted = useMemo(() => (data ? sortByTaskId(data) : []), [data]);
    // Time-filtered subset for charts (AC-7)
    const filtered = useMemo(() => filterByTimeRange(sorted, dateFromTaskId, timeRange), [sorted, timeRange]);
    // ---- AC-5: Summary row metrics (computed from ALL data, not filtered) ----
    const summaryMetrics = useMemo(() => {
        if (!tasks && !projectStats && !repoReport && !repoState && sorted.length === 0)
            return null;
        const totalTasks = tasks?.length ?? projectStats?.total_tasks ?? sorted.length;
        const avgQualityScore = projectStats?.average_quality_score
            ?? (sorted.length > 0
                ? sorted.reduce((sum, retro) => sum + (retro.quality_score ?? 0), 0) / sorted.length
                : 0);
        const activeRoutes = repoReport?.summary.active_routes ?? 0;
        const queueDepth = repoReport?.summary.queued_automation_jobs ?? 0;
        const fileCount = repoState?.file_count ?? 0;
        const totalTokens = sorted.reduce((sum, retro) => sum + (retro.total_token_usage ?? 0), 0);
        const estCost = (totalTokens / 1_000_000) * BLENDED_RATE_PER_MILLION;
        const latest = sorted[sorted.length - 1];
        const prior = sorted.slice(0, -1);
        const priorMeanQuality = prior.length > 0
            ? prior.reduce((sum, retro) => sum + (retro.quality_score ?? 0), 0) / prior.length
            : 0;
        const priorMeanRepairs = prior.length > 0
            ? prior.reduce((sum, retro) => sum + (retro.repair_cycle_count ?? 0), 0) / prior.length
            : 0;
        const latestRepairs = latest?.repair_cycle_count ?? 0;
        return {
            totalTasks: formatCompactNumber(totalTasks),
            avgQuality: formatPercentage(avgQualityScore),
            activeRoutes: formatCompactNumber(activeRoutes),
            fileCount: formatCompactNumber(fileCount),
            queueDepth: formatCompactNumber(queueDepth),
            estCost: formatSummaryCost(estCost),
            trendQuality: latest ? computeTrendPercent(latest.quality_score ?? 0, priorMeanQuality) : null,
            trendRepairs: latest ? computeTrendPercent(latestRepairs, priorMeanRepairs) : null,
        };
    }, [tasks, projectStats, repoReport, repoState, sorted]);
    // Derived chart data — uses filtered subset
    const qualityData = useMemo(() => filtered.map((r) => ({ task_id: shortenTaskId(r.task_id), quality_score: r.quality_score })), [filtered]);
    const costData = useMemo(() => filtered.map((r) => ({ task_id: shortenTaskId(r.task_id), cost_score: r.cost_score })), [filtered]);
    const modelData = useMemo(() => buildModelUsageData(filtered), [filtered]);
    const repairData = useMemo(() => buildRepairData(filtered), [filtered]);
    const spawnData = useMemo(() => filtered.map((r) => ({
        task_id: shortenTaskId(r.task_id),
        total_spawns: r.subagent_spawn_count,
        wasted_spawns: r.wasted_spawns,
    })), [filtered]);
    // AC-22: New chart data
    const tokenCostResult = useMemo(() => buildTokenCostData(filtered), [filtered]);
    const findingsData = useMemo(() => buildFindingsData(filtered), [filtered]);
    const repairRateData = useMemo(() => buildRepairRateData(filtered), [filtered]);
    const routingData = useMemo(() => buildRoutingData(filtered), [filtered]);
    const tokenIOData = useMemo(() => buildTokenIOData(filtered), [filtered]);
    // TimeRangeFilter element reused across chart cards
    const timeRangeAction = (_jsx(TimeRangeFilter, { value: timeRange, onChange: setTimeRange }));
    // ---- Render states ----
    const pageHeader = (_jsxs("div", { className: "flex items-center gap-3 mb-6", children: [_jsx(BarChart3, { className: "w-5 h-5 text-[#BDF000]", "aria-hidden": "true" }), _jsx("h1", { className: "font-mono text-sm font-semibold text-[#BDF000] tracking-widest uppercase", children: "Analytics" })] }));
    if (tasksLoading && loading && reportLoading && statsLoading && stateLoading) {
        return (_jsxs("div", { className: "p-4 sm:p-6 max-w-7xl mx-auto", children: [pageHeader, _jsx(LoadingState, {})] }));
    }
    if (tasksError && !tasks && error && !data && reportError && !repoReport && statsError && !projectStats) {
        return (_jsxs("div", { className: "p-4 sm:p-6 max-w-7xl mx-auto", children: [pageHeader, _jsx(ErrorState, { message: [tasksError, error, reportError, statsError].filter(Boolean).join(" | "), onRetry: () => {
                        refetchTasks();
                        refetch();
                        refetchReport();
                        refetchStats();
                        refetchState();
                    } })] }));
    }
    return (_jsxs("div", { className: "p-4 sm:p-6 max-w-7xl mx-auto", children: [pageHeader, summaryMetrics && (_jsxs("div", { className: "grid grid-cols-2 lg:grid-cols-4 gap-3 mb-6", role: "region", "aria-label": "Summary metrics", children: [_jsx(MetricCard, { label: "Total Tasks", value: summaryMetrics.totalTasks, icon: _jsx(ShieldCheck, { className: "w-3.5 h-3.5 text-[#BDF000]", "aria-hidden": "true" }), delay: 0 }), _jsx(MetricCard, { label: "Avg Quality (Completed)", value: summaryMetrics.avgQuality, trend: summaryMetrics.trendQuality, trendLabel: "latest vs prior mean", icon: _jsx(TrendingUp, { className: "w-3.5 h-3.5 text-[#2DD4A8]", "aria-hidden": "true" }), delay: 0.05 }), _jsx(MetricCard, { label: "Active Routes", value: summaryMetrics.activeRoutes, icon: _jsx(GitBranch, { className: "w-3.5 h-3.5 text-[#B47AFF]", "aria-hidden": "true" }), delay: 0.1 }), _jsx(MetricCard, { label: "Code Files", value: summaryMetrics.fileCount, icon: _jsx(FileCode2, { className: "w-3.5 h-3.5 text-[#FF9F43]", "aria-hidden": "true" }), delay: 0.15 })] })), _jsxs(Tabs, { defaultValue: "charts", children: [_jsxs(TabsList, { className: "bg-[#0D1117]/80 border border-[#BDF000]/10 mb-6", children: [_jsx(TabsTrigger, { value: "charts", className: "font-mono text-xs tracking-wider uppercase data-[state=active]:text-[#BDF000] data-[state=active]:bg-[#BDF000]/10", "aria-label": "View charts", children: "Charts" }), _jsx(TabsTrigger, { value: "costs", className: "font-mono text-xs tracking-wider uppercase data-[state=active]:text-[#BDF000] data-[state=active]:bg-[#BDF000]/10", "aria-label": "View cost analysis", children: "Costs" }), _jsx(TabsTrigger, { value: "repo", className: "font-mono text-xs tracking-wider uppercase data-[state=active]:text-[#BDF000] data-[state=active]:bg-[#BDF000]/10", "aria-label": "View repo analytics", children: "Repo" })] }), _jsx(TabsContent, { value: "charts", children: !data || data.length < 2 ? (_jsx(InsufficientDataState, { count: data?.length ?? 0 })) : (_jsxs("div", { className: "space-y-6", children: [_jsx(ChartCard, { title: "Quality Trend", action: timeRangeAction, children: _jsx(ResponsiveContainer, { width: "100%", height: CHART_HEIGHT, children: _jsxs(LineChart, { data: qualityData, style: { background: "transparent" }, margin: CHART_MARGIN, children: [_jsx(CartesianGrid, { stroke: COLORS.grid, strokeDasharray: "3 3" }), _jsx(XAxis, { dataKey: "task_id", ...TASK_X_AXIS_PROPS }), _jsx(YAxis, { domain: [0, 1], tick: AXIS_TICK_STYLE, axisLine: { stroke: COLORS.grid }, tickLine: { stroke: COLORS.grid } }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE }), _jsx(Line, { type: "monotone", dataKey: "quality_score", stroke: COLORS.quality, strokeWidth: 2, dot: { r: 4, fill: COLORS.quality }, activeDot: { r: 6 }, name: "Quality Score" })] }) }) }), _jsx(ChartCard, { title: "Cost Score Trend", subtitle: "Dynos retrospective cost score, not dollar spend.", action: timeRangeAction, children: _jsx(ResponsiveContainer, { width: "100%", height: CHART_HEIGHT, children: _jsxs(LineChart, { data: costData, style: { background: "transparent" }, margin: CHART_MARGIN, children: [_jsx(CartesianGrid, { stroke: COLORS.grid, strokeDasharray: "3 3" }), _jsx(XAxis, { dataKey: "task_id", ...TASK_X_AXIS_PROPS }), _jsx(YAxis, { domain: [0, 1], tick: AXIS_TICK_STYLE, axisLine: { stroke: COLORS.grid }, tickLine: { stroke: COLORS.grid } }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE }), _jsx(Line, { type: "monotone", dataKey: "cost_score", stroke: COLORS.cost, strokeWidth: 2, dot: { r: 4, fill: COLORS.cost }, activeDot: { r: 6 }, name: "Cost Score" })] }) }) }), _jsxs("div", { className: "grid grid-cols-1 md:grid-cols-2 gap-6", children: [_jsx(ChartCard, { title: "Agent-Model Assignment Mix", subtitle: "Counts model assignments recorded per agent in retrospectives.", action: timeRangeAction, children: _jsx(ResponsiveContainer, { width: "100%", height: PIE_CHART_HEIGHT, children: _jsxs(PieChart, { style: { background: "transparent" }, children: [_jsx(Pie, { data: modelData, dataKey: "value", nameKey: "name", cx: "50%", cy: "44%", innerRadius: "45%", outerRadius: "70%", paddingAngle: 2, label: false, children: modelData.map((entry, index) => (_jsx(Cell, { fill: entry.color }, `model-${index}`))) }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE }), _jsx(Legend, { wrapperStyle: {
                                                                fontFamily: "JetBrains Mono",
                                                                fontSize: 11,
                                                                color: "#999",
                                                            } })] }) }) }), _jsx(ChartCard, { title: "Executor Repair Frequency", action: timeRangeAction, children: _jsx(ResponsiveContainer, { width: "100%", height: CHART_HEIGHT, children: _jsxs(BarChart, { data: repairData, style: { background: "transparent" }, margin: CHART_MARGIN, children: [_jsx(CartesianGrid, { stroke: COLORS.grid, strokeDasharray: "3 3" }), _jsx(XAxis, { dataKey: "executor", tick: AXIS_TICK_STYLE, axisLine: { stroke: COLORS.grid }, tickLine: { stroke: COLORS.grid }, interval: 0, angle: -35, textAnchor: "end", height: 72, tickMargin: 8, tickFormatter: (value) => shortenLabel(value, 18) }), _jsx(YAxis, { tick: AXIS_TICK_STYLE, axisLine: { stroke: COLORS.grid }, tickLine: { stroke: COLORS.grid }, allowDecimals: false }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE }), _jsx(Bar, { dataKey: "repairs", fill: COLORS.teal, radius: [4, 4, 0, 0], name: "Repairs" })] }) }) })] }), _jsx(ChartCard, { title: "Spawn Efficiency", action: timeRangeAction, children: _jsx(ResponsiveContainer, { width: "100%", height: CHART_HEIGHT, children: _jsxs(LineChart, { data: spawnData, style: { background: "transparent" }, margin: CHART_MARGIN, children: [_jsx(CartesianGrid, { stroke: COLORS.grid, strokeDasharray: "3 3" }), _jsx(XAxis, { dataKey: "task_id", ...TASK_X_AXIS_PROPS }), _jsx(YAxis, { tick: AXIS_TICK_STYLE, axisLine: { stroke: COLORS.grid }, tickLine: { stroke: COLORS.grid }, allowDecimals: false }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE }), _jsx(Legend, { wrapperStyle: {
                                                        fontFamily: "JetBrains Mono",
                                                        fontSize: 11,
                                                        color: "#999",
                                                    } }), _jsx(Line, { type: "monotone", dataKey: "total_spawns", stroke: COLORS.quality, strokeWidth: 2, dot: { r: 4, fill: COLORS.quality }, activeDot: { r: 6 }, name: "Total Spawns" }), _jsx(Line, { type: "monotone", dataKey: "wasted_spawns", stroke: COLORS.red, strokeWidth: 2, dot: { r: 4, fill: COLORS.red }, activeDot: { r: 6 }, name: "Wasted Spawns" })] }) }) }), _jsx(ChartCard, { title: "Token Cost Breakdown", action: timeRangeAction, children: tokenCostResult.agentKeys.length === 0 ? (_jsxs("p", { className: "text-slate-600 font-mono text-xs py-8 text-center", children: [NA_LABEL, " \u2014 No token usage data recorded yet."] })) : (_jsx(ResponsiveContainer, { width: "100%", height: CHART_HEIGHT, children: _jsxs(BarChart, { data: tokenCostResult.data, style: { background: "transparent" }, margin: CHART_MARGIN, children: [_jsx(CartesianGrid, { stroke: COLORS.grid, strokeDasharray: "3 3" }), _jsx(XAxis, { dataKey: "task_id", ...TASK_X_AXIS_PROPS }), _jsx(YAxis, { tick: AXIS_TICK_STYLE, axisLine: { stroke: COLORS.grid }, tickLine: { stroke: COLORS.grid }, allowDecimals: false, tickFormatter: (value) => formatTokens(value) }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE, formatter: (value) => [formatTokens(value), undefined] }), _jsx(Legend, { wrapperStyle: {
                                                        fontFamily: "JetBrains Mono",
                                                        fontSize: 11,
                                                        color: "#999",
                                                    } }), tokenCostResult.agentKeys.map((agent, idx) => (_jsx(Bar, { dataKey: agent, stackId: "tokens", fill: AGENT_ROLE_COLORS[idx % AGENT_ROLE_COLORS.length], name: agent, radius: idx === tokenCostResult.agentKeys.length - 1 ? [4, 4, 0, 0] : undefined }, agent)))] }) })) }), _jsx(ChartCard, { title: "Token I/O per Task", action: timeRangeAction, children: tokenIOData.every((d) => d.input_tokens === 0 && d.output_tokens === 0) ? (_jsxs("p", { className: "text-slate-600 font-mono text-xs py-8 text-center", children: [NA_LABEL, " \u2014 No input/output token data recorded yet."] })) : (_jsx(ResponsiveContainer, { width: "100%", height: CHART_HEIGHT, children: _jsxs(BarChart, { data: tokenIOData, style: { background: "transparent" }, margin: CHART_MARGIN, children: [_jsx(CartesianGrid, { stroke: COLORS.grid, strokeDasharray: "3 3" }), _jsx(XAxis, { dataKey: "task_id", ...TASK_X_AXIS_PROPS }), _jsx(YAxis, { tick: AXIS_TICK_STYLE, axisLine: { stroke: COLORS.grid }, tickLine: { stroke: COLORS.grid }, allowDecimals: false, tickFormatter: (v) => formatTokens(v) }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE, formatter: (value) => [formatTokens(value), undefined] }), _jsx(Legend, { wrapperStyle: {
                                                        fontFamily: "JetBrains Mono",
                                                        fontSize: 11,
                                                        color: "#999",
                                                    } }), _jsx(Bar, { dataKey: "input_tokens", stackId: "io", fill: "#B47AFF", name: "Input (uploaded)", radius: [0, 0, 0, 0] }), _jsx(Bar, { dataKey: "output_tokens", stackId: "io", fill: "#BDF000", name: "Output (downloaded)", radius: [4, 4, 0, 0] })] }) })) }), _jsx(ChartCard, { title: "Findings Per Task", action: timeRangeAction, children: _jsx(ResponsiveContainer, { width: "100%", height: CHART_HEIGHT, children: _jsxs(BarChart, { data: findingsData.map((d) => ({
                                                ...d,
                                                findings: typeof d.findings === "number" ? d.findings : 0,
                                                hasData: typeof d.findings === "number",
                                            })), style: { background: "transparent" }, margin: CHART_MARGIN, children: [_jsx(CartesianGrid, { stroke: COLORS.grid, strokeDasharray: "3 3" }), _jsx(XAxis, { dataKey: "task_id", ...TASK_X_AXIS_PROPS }), _jsx(YAxis, { tick: AXIS_TICK_STYLE, axisLine: { stroke: COLORS.grid }, tickLine: { stroke: COLORS.grid }, allowDecimals: false }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE, formatter: ((value, _name, props) => {
                                                        if (props.payload && !props.payload.hasData)
                                                            return [NA_LABEL, "Findings"];
                                                        return [value, "Findings"];
                                                    }) }), _jsx(Bar, { dataKey: "findings", fill: COLORS.amber, radius: [4, 4, 0, 0], name: "Findings" })] }) }) }), _jsx(ChartCard, { title: "Repair Intensity vs Findings", subtitle: "Proxy metric: repair cycles divided by findings, capped at 100%.", action: timeRangeAction, children: _jsx(ResponsiveContainer, { width: "100%", height: CHART_HEIGHT, children: _jsxs(LineChart, { data: repairRateData, style: { background: "transparent" }, margin: CHART_MARGIN, children: [_jsx(CartesianGrid, { stroke: COLORS.grid, strokeDasharray: "3 3" }), _jsx(XAxis, { dataKey: "task_id", ...TASK_X_AXIS_PROPS }), _jsx(YAxis, { domain: [0, 100], tick: AXIS_TICK_STYLE, axisLine: { stroke: COLORS.grid }, tickLine: { stroke: COLORS.grid }, unit: "%" }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE, formatter: ((value) => {
                                                        if (value === null || value === undefined)
                                                            return [NA_LABEL, "Success Rate"];
                                                        return [`${value}%`, "Success Rate"];
                                                    }) }), _jsx(Line, { type: "monotone", dataKey: "success_rate", stroke: COLORS.teal, strokeWidth: 2, dot: { r: 4, fill: COLORS.teal }, activeDot: { r: 6 }, name: "Success Rate", connectNulls: false })] }) }) }), _jsx("div", { className: "grid grid-cols-1 md:grid-cols-2 gap-6", children: _jsx(ChartCard, { title: "Routing Distribution", action: timeRangeAction, children: routingData.length === 0 ? (_jsxs("p", { className: "text-slate-600 font-mono text-xs py-8 text-center", children: [NA_LABEL, " \u2014 No routing data available."] })) : (_jsx(ResponsiveContainer, { width: "100%", height: PIE_CHART_HEIGHT, children: _jsxs(PieChart, { style: { background: "transparent" }, children: [_jsx(Pie, { data: routingData, dataKey: "value", nameKey: "name", cx: "50%", cy: "44%", innerRadius: "45%", outerRadius: "70%", paddingAngle: 2, label: false, children: routingData.map((entry, index) => (_jsx(Cell, { fill: entry.color }, `routing-${index}`))) }), _jsx(Tooltip, { contentStyle: TOOLTIP_STYLE }), _jsx(Legend, { wrapperStyle: {
                                                            fontFamily: "JetBrains Mono",
                                                            fontSize: 11,
                                                            color: "#999",
                                                        } })] }) })) }) })] })) }), _jsx(TabsContent, { value: "costs", children: !data || data.length === 0 ? (_jsx(InsufficientDataState, { count: data?.length ?? 0 })) : (_jsx(CostsTabContent, { retros: sorted })) }), _jsx(TabsContent, { value: "repo", children: reportLoading && !repoReport && statsLoading && !projectStats && stateLoading && !repoState ? (_jsx(LoadingState, {})) : reportError && !repoReport && statsError && !projectStats ? (_jsx(ErrorState, { message: [reportError, statsError, stateError].filter(Boolean).join(" | "), onRetry: () => {
                                refetchReport();
                                refetchStats();
                                refetchState();
                            } })) : (_jsx(RepoTabContent, { report: repoReport, state: repoState, stateError: stateError, projectStats: projectStats, totalTaskCount: tasks?.length ?? projectStats?.total_tasks ?? 0 })) })] })] }));
}
