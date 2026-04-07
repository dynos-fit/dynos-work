import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
import { useState, useCallback, useEffect, useMemo } from "react";
import { Link } from "react-router";
import { motion } from "motion/react";
import { Search, Terminal, ChevronDown, ChevronRight, ChevronUp, AlertCircle, FileText, GitBranch, Shield, DollarSign, ListChecks, Play, CheckCircle2, XCircle, ExternalLink } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { usePollingData } from "@/data/hooks";
import { useProject } from "@/data/ProjectContext";
import { Skeleton } from "@/components/ui/skeleton";
import { MetricCard } from "@/components/MetricCard";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Collapsible, CollapsibleTrigger, CollapsibleContent, } from "@/components/ui/collapsible";
// ---- Constants ----
const STAGE_COLORS = {
    DONE: "#2DD4A8",
    FAILED: "#FF3B3B",
    BLOCKED: "#FF3B3B",
};
const RISK_COLORS = {
    low: "#BDF000",
    medium: "#2DD4A8",
    high: "#B47AFF",
    critical: "#FF3B3B",
};
const EXECUTOR_COLORS = {
    ui: "#BDF000",
    backend: "#2DD4A8",
    ml: "#B47AFF",
    test: "#FF9F43",
    infra: "#FF3B3B",
    db: "#FF7043",
};
const SEVERITY_COLORS = {
    critical: "#FF3B3B",
    high: "#B47AFF",
    medium: "#FF9F43",
    low: "#BDF000",
};
const TIMELINE_STAGES = ["DISCOVERY", "SPEC_REVIEW", "PLANNING", "PLAN_REVIEW", "EXECUTION", "AUDITING", "DONE"];
// Map actual manifest stages to timeline position
const STAGE_TO_TIMELINE = {
    FOUNDRY_INITIALIZED: 0,
    DISCOVERY: 0,
    SPEC_NORMALIZATION: 1,
    SPEC_REVIEW: 1,
    PLANNING: 2,
    PLAN_REVIEW: 3,
    PLAN_AUDIT: 3,
    PRE_EXECUTION_SNAPSHOT: 4,
    EXECUTION: 4,
    TEST_EXECUTION: 4,
    CHECKPOINT_AUDIT: 5,
    AUDITING: 5,
    FINAL_AUDIT: 5,
    DONE: 6,
};
const TIMELINE_STAGE_COLORS = {
    DISCOVERY: "#B47AFF",
    SPEC_REVIEW: "#BDF000",
    PLANNING: "#FF9F43",
    PLAN_REVIEW: "#FF7043",
    EXECUTION: "#2DD4A8",
    AUDITING: "#BDF000",
    DONE: "#2DD4A8",
    FAILED: "#FF3B3B",
    BLOCKED: "#FF3B3B",
};
const IN_PROGRESS_COLOR = "#FF9F43";
const FALLBACK_RISK_COLOR = "#999";
// ---- Project-scoped fetch helper ----
function useProjectFetchUrl() {
    const { selectedProject } = useProject();
    return (path) => {
        const sep = path.includes("?") ? "&" : "?";
        return `${path}${sep}project=${encodeURIComponent(selectedProject)}`;
    };
}
// ---- Helpers ----
function getStageColor(stage) {
    if (stage === "DONE")
        return STAGE_COLORS.DONE;
    if (stage.includes("FAIL"))
        return STAGE_COLORS.FAILED;
    if (stage.includes("BLOCKED"))
        return STAGE_COLORS.BLOCKED;
    return IN_PROGRESS_COLOR;
}
function getRiskColor(risk) {
    return RISK_COLORS[risk] ?? FALLBACK_RISK_COLOR;
}
function basename(path) {
    return path.split("/").pop() ?? path;
}
function formatQualityScore(retros, taskId) {
    if (!retros)
        return "--";
    const retro = retros.find((r) => r.task_id === taskId);
    if (!retro || retro.quality_score === undefined)
        return "--";
    return Math.round(retro.quality_score * 100) + "%";
}
function getExecutorColor(executor) {
    const lower = executor.toLowerCase();
    for (const [key, color] of Object.entries(EXECUTOR_COLORS)) {
        if (lower.includes(key))
            return color;
    }
    return "#999";
}
function getSeverityColor(severity) {
    return SEVERITY_COLORS[severity.toLowerCase()] ?? "#999";
}
function getTimelineProgress(stage) {
    if (stage.includes("FAIL") || stage.includes("BLOCKED"))
        return -1;
    const mapped = STAGE_TO_TIMELINE[stage];
    if (mapped !== undefined)
        return mapped;
    // Fallback: try direct match
    const idx = TIMELINE_STAGES.indexOf(stage);
    return idx === -1 ? 0 : idx;
}
// ---- Sub-components ----
function SkeletonTable({ rows }) {
    return (_jsx("div", { className: "space-y-3", role: "status", "aria-label": "Loading tasks", children: Array.from({ length: rows }, (_, i) => (_jsxs("div", { className: "flex gap-4 px-4 py-3", children: [_jsx(Skeleton, { className: "h-4 w-36" }), _jsx(Skeleton, { className: "h-4 w-48" }), _jsx(Skeleton, { className: "h-4 w-20" }), _jsx(Skeleton, { className: "h-4 w-20" }), _jsx(Skeleton, { className: "h-4 w-16" }), _jsx(Skeleton, { className: "h-4 w-16" }), _jsx(Skeleton, { className: "h-4 w-20" }), _jsx(Skeleton, { className: "h-4 w-24" })] }, i))) }));
}
function ErrorCard({ message, onRetry }) {
    return (_jsxs("div", { className: "flex flex-col items-center justify-center py-16 px-4 bg-red-500/10 border border-red-500/30 rounded-lg", role: "alert", children: [_jsx("p", { className: "text-red-400 font-mono text-sm mb-4", children: "Unable to load tasks. Please try again." }), _jsx("p", { className: "text-slate-500 font-mono text-xs mb-6 max-w-md text-center truncate", children: message }), _jsx("button", { onClick: onRetry, className: "px-4 py-2 bg-red-500/20 hover:bg-red-500/30 text-red-400 border border-red-500/30 font-mono text-xs rounded transition-colors", "aria-label": "Retry loading tasks", children: "RETRY" })] }));
}
function EmptyState() {
    return (_jsxs("div", { className: "flex flex-col items-center justify-center py-20 px-4", role: "status", children: [_jsx(Terminal, { className: "w-10 h-10 text-slate-600 mb-4", "aria-hidden": "true" }), _jsx("p", { className: "text-slate-400 font-mono text-sm", children: "No tasks found" }), _jsx("p", { className: "text-slate-600 font-mono text-xs mt-2", children: "Tasks will appear here once created via the CLI." })] }));
}
function StaleErrorBanner({ message, onRetry }) {
    return (_jsxs("div", { className: "flex items-center justify-between gap-4 px-4 py-2 mb-4 bg-red-500/10 border border-red-500/30 rounded text-xs font-mono", role: "alert", children: [_jsxs("span", { className: "text-red-400 truncate", children: ["Update failed: ", message] }), _jsx("button", { onClick: onRetry, className: "text-red-400 hover:text-red-300 underline shrink-0", "aria-label": "Retry loading tasks", children: "Retry" })] }));
}
// ---- AC-14: Timeline indicator ----
function TimelineIndicator({ stage }) {
    const progress = getTimelineProgress(stage);
    const isFailed = progress === -1;
    const totalStages = TIMELINE_STAGES.length;
    return (_jsx("div", { className: "flex items-center gap-px h-3 w-20", "aria-label": `Stage progression: ${stage}`, title: stage, children: TIMELINE_STAGES.map((s, i) => {
            let bgColor;
            if (isFailed) {
                bgColor = i === 0 ? "#FF3B3B" : "#333";
            }
            else if (i <= progress) {
                bgColor = TIMELINE_STAGE_COLORS[s] ?? IN_PROGRESS_COLOR;
            }
            else {
                bgColor = "#333";
            }
            const isFirst = i === 0;
            const isLast = i === totalStages - 1;
            return (_jsx("div", { className: `h-full flex-1 ${isFirst ? "rounded-l" : ""} ${isLast ? "rounded-r" : ""}`, style: { backgroundColor: bgColor }, "aria-hidden": "true" }, s));
        }) }));
}
// ---- AC-11: Markdown section ----
function MarkdownSection({ taskId, endpoint, label }) {
    const [content, setContent] = useState(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const [open, setOpen] = useState(false);
    const projectUrl = useProjectFetchUrl();
    useEffect(() => {
        if (!open || content !== null)
            return;
        let cancelled = false;
        async function fetchContent() {
            setLoading(true);
            try {
                const res = await fetch(projectUrl(`/api/tasks/${encodeURIComponent(taskId)}/${endpoint}`));
                if (!res.ok)
                    throw new Error(`Failed to load ${label.toLowerCase()}`);
                const data = await res.json();
                if (!cancelled) {
                    setContent(data.content ?? data.markdown ?? data.text ?? "");
                    setError(null);
                }
            }
            catch (err) {
                if (!cancelled)
                    setError(err instanceof Error ? err.message : "Failed to load");
            }
            finally {
                if (!cancelled)
                    setLoading(false);
            }
        }
        fetchContent();
        return () => { cancelled = true; };
    }, [open, taskId, endpoint, label, content]);
    return (_jsxs(Collapsible, { open: open, onOpenChange: setOpen, children: [_jsx(CollapsibleTrigger, { asChild: true, children: _jsxs("button", { className: "flex items-center gap-2 text-slate-400 hover:text-slate-200 font-mono text-xs transition-colors", "aria-expanded": open, "aria-label": `Toggle ${label} section`, children: [open ? (_jsx(ChevronDown, { className: "w-3 h-3", "aria-hidden": "true" })) : (_jsx(ChevronRight, { className: "w-3 h-3", "aria-hidden": "true" })), _jsx(FileText, { className: "w-3 h-3", "aria-hidden": "true" }), label] }) }), _jsx(CollapsibleContent, { children: _jsxs("div", { className: "mt-2 bg-black/40 rounded p-3 max-h-64 overflow-auto font-mono text-xs", children: [loading && (_jsxs("div", { className: "space-y-2", role: "status", "aria-label": `Loading ${label.toLowerCase()}`, children: [_jsx(Skeleton, { className: "h-3 w-full" }), _jsx(Skeleton, { className: "h-3 w-3/4" }), _jsx(Skeleton, { className: "h-3 w-1/2" })] })), error && (_jsx("p", { className: "text-red-400", role: "alert", children: error })), content !== null && !loading && !error && content.length === 0 && (_jsxs("p", { className: "text-slate-600", children: ["No ", label.toLowerCase(), " content available."] })), content !== null && !loading && !error && content.length > 0 && (_jsx("div", { className: "prose prose-invert prose-xs max-w-none text-slate-300 [&_pre]:bg-black/30 [&_pre]:p-2 [&_pre]:rounded [&_code]:text-[#BDF000] [&_h1]:text-sm [&_h2]:text-xs [&_h3]:text-xs [&_table]:text-xs [&_a]:text-[#BDF000]", children: _jsx(ReactMarkdown, { remarkPlugins: [remarkGfm], skipHtml: true, children: content }) }))] }) })] }));
}
// ---- AC-12: Execution graph mini-visualization ----
function ExecutionGraphMini({ segments }) {
    if (segments.length === 0) {
        return _jsx("p", { className: "text-slate-600 font-mono text-xs", children: "No segments defined." });
    }
    // Group by dependency depth for layout
    const depthMap = new Map();
    function getDepth(seg) {
        if (depthMap.has(seg.id))
            return depthMap.get(seg.id);
        if (seg.depends_on.length === 0) {
            depthMap.set(seg.id, 0);
            return 0;
        }
        const parentDepths = seg.depends_on.map((depId) => {
            const parent = segments.find((s) => s.id === depId);
            return parent ? getDepth(parent) : 0;
        });
        const depth = Math.max(...parentDepths) + 1;
        depthMap.set(seg.id, depth);
        return depth;
    }
    segments.forEach(getDepth);
    const maxDepth = Math.max(...Array.from(depthMap.values()));
    const layers = Array.from({ length: maxDepth + 1 }, () => []);
    for (const seg of segments) {
        layers[depthMap.get(seg.id)].push(seg);
    }
    return (_jsx("div", { className: "flex items-start gap-2 overflow-x-auto pb-2", role: "img", "aria-label": `Execution graph with ${segments.length} segments across ${layers.length} layers`, children: layers.map((layer, layerIdx) => (_jsxs("div", { className: "flex flex-col gap-1 items-center shrink-0", children: [layer.map((seg) => {
                    const color = getExecutorColor(seg.executor);
                    return (_jsx("div", { className: "px-2 py-1 rounded border text-[10px] font-mono whitespace-nowrap max-w-[120px] truncate", style: {
                            borderColor: color + "4D",
                            backgroundColor: color + "1A",
                            color: color,
                        }, title: `${seg.id}: ${seg.description}`, children: seg.id }, seg.id));
                }), layerIdx < layers.length - 1 && (_jsx("div", { className: "text-slate-600 text-xs", "aria-hidden": "true", children: "\u2192" }))] }, layerIdx))) }));
}
// ---- AC-13: Audit findings summary ----
function AuditFindingsSummary({ taskId }) {
    const [reports, setReports] = useState(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const projectUrl = useProjectFetchUrl();
    useEffect(() => {
        let cancelled = false;
        async function fetchReports() {
            setLoading(true);
            try {
                const res = await fetch(projectUrl(`/api/tasks/${encodeURIComponent(taskId)}/audit-reports`));
                if (!res.ok)
                    throw new Error("Failed to load audit reports");
                const data = (await res.json());
                if (!cancelled) {
                    setReports(data);
                    setError(null);
                }
            }
            catch (err) {
                if (!cancelled)
                    setError(err instanceof Error ? err.message : "Failed to load");
            }
            finally {
                if (!cancelled)
                    setLoading(false);
            }
        }
        fetchReports();
        return () => { cancelled = true; };
    }, [taskId]);
    const allFindings = useMemo(() => {
        if (!reports)
            return [];
        return reports.flatMap((r) => r.findings);
    }, [reports]);
    return (_jsxs("div", { children: [_jsxs("h4", { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider mb-2 flex items-center gap-1.5", children: [_jsx(Shield, { className: "w-3 h-3", "aria-hidden": "true" }), "Audit Findings"] }), loading && (_jsxs("div", { className: "space-y-1", role: "status", "aria-label": "Loading audit findings", children: [_jsx(Skeleton, { className: "h-3 w-64" }), _jsx(Skeleton, { className: "h-3 w-48" })] })), error && (_jsx("p", { className: "text-red-400 font-mono text-xs", role: "alert", children: error })), reports !== null && !loading && !error && allFindings.length === 0 && (_jsx("p", { className: "text-slate-600 font-mono text-xs", children: "No audit findings recorded." })), reports !== null && !loading && !error && allFindings.length > 0 && (_jsx("div", { className: "space-y-1 max-h-40 overflow-y-auto", children: allFindings.map((finding) => {
                    const color = getSeverityColor(finding.severity);
                    return (_jsxs("div", { className: "flex items-center gap-2 text-xs font-mono", children: [_jsx("span", { className: "w-2 h-2 rounded-full shrink-0", style: { backgroundColor: color }, "aria-label": `${finding.severity} severity` }), _jsx("span", { className: "text-slate-300 truncate max-w-md", title: finding.description, children: finding.title || finding.description }), _jsx("span", { className: "text-slate-600 shrink-0", children: finding.category })] }, finding.id));
                }) }))] }));
}
// ---- Token Usage Summary per task ----
function formatTokens(n) {
    if (n >= 1_000_000)
        return `${(n / 1_000_000).toFixed(2)}M`;
    if (n >= 1_000)
        return `${(n / 1_000).toFixed(1)}K`;
    return String(n);
}
const PHASE_COLORS = {
    planning: "#B47AFF",
    execution: "#2DD4A8",
    audit: "#BDF000",
    tdd: "#FF9F43",
    repair: "#FF3B3B",
};
const EVENT_TYPE_STYLES = {
    spawn: { color: "#00E5FF", label: "SPAWN" },
    deterministic: { color: "#FFB300", label: "DETERMINISTIC" },
    inline: { color: "#FF9F43", label: "INLINE" },
};
const MODEL_BADGE_COLORS = {
    opus: "#7C4DFF",
    sonnet: "#00E5FF",
    haiku: "#00BFA5",
    none: "#555",
};
function formatTime(iso) {
    try {
        const d = new Date(iso);
        return d.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
    }
    catch {
        return iso.slice(11, 19);
    }
}
function Badge({ label, color }) {
    return (_jsx("span", { className: "px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider shrink-0", style: { color, borderColor: color + "4D", backgroundColor: color + "1A", border: `1px solid ${color}4D` }, children: label }));
}
function TokenUsageSummary({ taskId }) {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const [showEvents, setShowEvents] = useState(false);
    const projectUrl = useProjectFetchUrl();
    useEffect(() => {
        let cancelled = false;
        async function fetchTokens() {
            setLoading(true);
            try {
                const res = await fetch(projectUrl(`/api/tasks/${encodeURIComponent(taskId)}/token-usage`));
                if (!res.ok)
                    throw new Error("No token data");
                const json = await res.json();
                if (!cancelled) {
                    setData(json);
                    setError(null);
                }
            }
            catch (err) {
                if (!cancelled)
                    setError(err instanceof Error ? err.message : "Failed to load");
            }
            finally {
                if (!cancelled)
                    setLoading(false);
            }
        }
        fetchTokens();
        return () => { cancelled = true; };
    }, [taskId]);
    const byAgent = data?.by_agent;
    const events = (data?.events ?? []);
    const totalInput = data?.total_input_tokens ?? 0;
    const totalOutput = data?.total_output_tokens ?? 0;
    const total = data?.total ?? 0;
    return (_jsxs("div", { children: [_jsxs("h4", { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider mb-2 flex items-center gap-1.5", children: [_jsx(DollarSign, { className: "w-3 h-3", "aria-hidden": "true" }), "Token Usage"] }), loading && (_jsxs("div", { className: "space-y-1", role: "status", "aria-label": "Loading token usage", children: [_jsx(Skeleton, { className: "h-3 w-64" }), _jsx(Skeleton, { className: "h-3 w-48" })] })), error && (_jsx("p", { className: "text-slate-600 font-mono text-xs", children: "No token usage data recorded." })), data && !loading && !error && (_jsxs("div", { className: "space-y-3", children: [byAgent && Object.keys(byAgent).length > 0 && (_jsx("div", { className: "overflow-x-auto", children: _jsxs("table", { className: "w-full font-mono text-xs", "aria-label": "Token usage by agent", children: [_jsx("thead", { children: _jsxs("tr", { className: "border-b border-white/10", children: [_jsx("th", { className: "text-left text-slate-500 py-1 pr-3", children: "Agent" }), _jsx("th", { className: "text-left text-slate-500 py-1 pr-3", children: "Model" }), _jsx("th", { className: "text-right text-slate-500 py-1 pr-3", children: "Input" }), _jsx("th", { className: "text-right text-slate-500 py-1 pr-3", children: "Output" }), _jsx("th", { className: "text-right text-slate-500 py-1", children: "Total" })] }) }), _jsx("tbody", { children: Object.entries(byAgent)
                                        .sort(([, a], [, b]) => b.tokens - a.tokens)
                                        .map(([agent, info]) => (_jsxs("tr", { className: "border-b border-white/5", children: [_jsx("td", { className: "text-slate-300 py-1 pr-3 max-w-[180px] truncate", title: agent, children: agent }), _jsx("td", { className: "py-1 pr-3", children: _jsx("span", { className: "text-[10px] font-mono font-medium", style: { color: MODEL_BADGE_COLORS[info.model] ?? "#999" }, children: info.model }) }), _jsx("td", { className: "text-right text-[#7C4DFF] py-1 pr-3", children: formatTokens(info.input_tokens) }), _jsx("td", { className: "text-right text-[#00E5FF] py-1 pr-3", children: formatTokens(info.output_tokens) }), _jsx("td", { className: "text-right text-slate-400 py-1", children: formatTokens(info.tokens) })] }, agent))) }), _jsx("tfoot", { children: _jsxs("tr", { className: "border-t border-[#00E5FF]/20", children: [_jsx("td", { colSpan: 2, className: "text-slate-300 font-semibold py-1 pr-3", children: "Total" }), _jsx("td", { className: "text-right text-[#7C4DFF] font-semibold py-1 pr-3", children: formatTokens(totalInput) }), _jsx("td", { className: "text-right text-[#00E5FF] font-semibold py-1 pr-3", children: formatTokens(totalOutput) }), _jsx("td", { className: "text-right text-slate-300 font-semibold py-1", children: formatTokens(total) })] }) })] }) })), events.length > 0 && (_jsxs(Collapsible, { open: showEvents, onOpenChange: setShowEvents, children: [_jsx(CollapsibleTrigger, { asChild: true, children: _jsxs("button", { className: "flex items-center gap-1.5 text-slate-400 hover:text-slate-200 font-mono text-[10px] uppercase tracking-wider transition-colors", "aria-expanded": showEvents, children: [showEvents ? (_jsx(ChevronDown, { className: "w-3 h-3", "aria-hidden": "true" })) : (_jsx(ChevronRight, { className: "w-3 h-3", "aria-hidden": "true" })), "Event Log (", events.length, " events)"] }) }), _jsx(CollapsibleContent, { children: _jsx("div", { className: "mt-2 max-h-80 overflow-y-auto bg-black/30 rounded p-2", children: events.map((evt, i) => {
                                        const typeStyle = EVENT_TYPE_STYLES[evt.type] ?? { color: "#666", label: evt.type };
                                        const phaseColor = PHASE_COLORS[evt.phase] ?? "#666";
                                        const modelColor = MODEL_BADGE_COLORS[evt.model] ?? "#555";
                                        return (_jsxs("div", { className: "flex items-center gap-2 py-1.5 border-b border-white/5 last:border-0 flex-wrap", children: [_jsx("span", { className: "text-slate-600 text-[10px] font-mono shrink-0 w-[52px]", children: formatTime(evt.timestamp) }), _jsx(Badge, { label: typeStyle.label, color: typeStyle.color }), _jsx(Badge, { label: evt.phase, color: phaseColor }), evt.stage && (_jsx("span", { className: "text-slate-600 text-[9px] font-mono shrink-0", children: evt.stage })), _jsx("span", { className: "text-slate-300 text-[11px] font-mono shrink-0 max-w-[200px] truncate", title: evt.agent, children: evt.agent }), evt.model !== "none" && (_jsx("span", { className: "text-[10px] font-mono font-medium shrink-0", style: { color: modelColor }, children: evt.model })), evt.segment && (_jsxs("span", { className: "text-slate-600 text-[10px] font-mono shrink-0", children: ["[", evt.segment, "]"] })), evt.tokens > 0 && (_jsxs("span", { className: "text-[10px] font-mono shrink-0", children: [_jsx("span", { className: "text-[#7C4DFF]", children: formatTokens(evt.input_tokens) }), _jsx("span", { className: "text-slate-600", children: " / " }), _jsx("span", { className: "text-[#00E5FF]", children: formatTokens(evt.output_tokens) })] })), evt.detail && (_jsx("span", { className: "text-slate-500 text-[10px] font-mono truncate max-w-[300px]", title: evt.detail, children: evt.detail }))] }, i));
                                    }) }) })] })), (!byAgent || Object.keys(byAgent).length === 0) && events.length === 0 && (_jsx("p", { className: "text-slate-600 font-mono text-xs", children: "No token usage data available." }))] }))] }));
}
function ExpandedDetail({ taskId }) {
    const [graph, setGraph] = useState(null);
    const [graphLoading, setGraphLoading] = useState(false);
    const [graphError, setGraphError] = useState(null);
    const [logLines, setLogLines] = useState(null);
    const [logLoading, setLogLoading] = useState(false);
    const [logError, setLogError] = useState(null);
    const projectUrl = useProjectFetchUrl();
    useEffect(() => {
        let cancelled = false;
        async function fetchGraph() {
            setGraphLoading(true);
            try {
                const res = await fetch(projectUrl(`/api/tasks/${encodeURIComponent(taskId)}/execution-graph`));
                if (!res.ok)
                    throw new Error("Failed to load execution graph");
                const data = (await res.json());
                if (!cancelled) {
                    setGraph(data);
                    setGraphError(null);
                }
            }
            catch (err) {
                if (!cancelled)
                    setGraphError(err instanceof Error ? err.message : "Failed to load");
            }
            finally {
                if (!cancelled)
                    setGraphLoading(false);
            }
        }
        async function fetchLog() {
            setLogLoading(true);
            try {
                const res = await fetch(projectUrl(`/api/tasks/${encodeURIComponent(taskId)}/execution-log`));
                if (!res.ok)
                    throw new Error("Failed to load execution log");
                const data = (await res.json());
                if (!cancelled) {
                    setLogLines((data.lines ?? []).slice(-10));
                    setLogError(null);
                }
            }
            catch (err) {
                if (!cancelled)
                    setLogError(err instanceof Error ? err.message : "Failed to load");
            }
            finally {
                if (!cancelled)
                    setLogLoading(false);
            }
        }
        fetchGraph();
        fetchLog();
        return () => {
            cancelled = true;
        };
    }, [taskId]);
    return (_jsxs("div", { className: "px-6 py-4 bg-[#0F1114]/40 border-t border-white/5 space-y-4", children: [_jsxs("div", { className: "flex gap-6", children: [_jsx(MarkdownSection, { taskId: taskId, endpoint: "spec", label: "SPEC" }), _jsx(MarkdownSection, { taskId: taskId, endpoint: "plan", label: "PLAN" })] }), _jsxs("div", { children: [_jsxs("h4", { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider mb-2 flex items-center gap-1.5", children: [_jsx(GitBranch, { className: "w-3 h-3", "aria-hidden": "true" }), "Execution Graph"] }), graphLoading && (_jsxs("div", { className: "space-y-2", role: "status", "aria-label": "Loading execution graph", children: [_jsx(Skeleton, { className: "h-3 w-64" }), _jsx(Skeleton, { className: "h-3 w-48" })] })), graphError && (_jsx("p", { className: "text-red-400 font-mono text-xs", role: "alert", children: graphError })), graph && _jsx(ExecutionGraphMini, { segments: graph.segments })] }), _jsx(AuditFindingsSummary, { taskId: taskId }), _jsx(TokenUsageSummary, { taskId: taskId }), _jsxs("div", { children: [_jsx("h4", { className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider mb-2", children: "Execution Log (last 10 lines)" }), logLoading && (_jsxs("div", { className: "space-y-1", role: "status", "aria-label": "Loading execution log", children: [_jsx(Skeleton, { className: "h-3 w-full" }), _jsx(Skeleton, { className: "h-3 w-3/4" })] })), logError && (_jsx("p", { className: "text-red-400 font-mono text-xs", role: "alert", children: logError })), logLines && logLines.length > 0 && (_jsx("pre", { className: "text-slate-400 font-mono text-[11px] leading-relaxed bg-black/30 rounded p-3 overflow-x-auto max-h-40", children: logLines.join("\n") })), logLines && logLines.length === 0 && (_jsx("p", { className: "text-slate-600 font-mono text-xs", children: "No log output available." }))] })] }));
}
function TaskRow({ task, qualityDisplay, isGlobal, index }) {
    const [open, setOpen] = useState(false);
    const stageColor = getStageColor(task.stage);
    const riskColor = getRiskColor(task.classification?.risk_level);
    const colSpan = isGlobal ? 9 : 8;
    return (_jsx(Collapsible, { asChild: true, open: open, onOpenChange: setOpen, children: _jsxs(_Fragment, { children: [_jsx(CollapsibleTrigger, { asChild: true, children: _jsxs(motion.tr, { initial: { opacity: 0, x: -10 }, animate: { opacity: 1, x: 0 }, transition: { delay: index * 0.1 }, className: `border-b border-white/5 hover:bg-white/[0.04] transition-colors cursor-pointer ${index % 2 === 0 ? "" : "bg-white/[0.02]"}`, role: "row", "aria-expanded": open, "aria-label": `Task ${task.task_id}: ${task.title}`, tabIndex: 0, onKeyDown: (e) => {
                            if (e.key === "Enter" || e.key === " ") {
                                e.preventDefault();
                                setOpen(!open);
                            }
                        }, children: [isGlobal && (_jsx("td", { className: "p-4 text-slate-400 font-mono text-xs", children: task.project_path ? basename(task.project_path) : "--" })), _jsx("td", { className: "p-4 text-[#BDF000] font-mono text-xs whitespace-nowrap", children: _jsxs("span", { className: "inline-flex items-center gap-1.5", children: [open ? (_jsx(ChevronDown, { className: "w-3 h-3 text-slate-500", "aria-hidden": "true" })) : (_jsx(ChevronRight, { className: "w-3 h-3 text-slate-500", "aria-hidden": "true" })), _jsxs(Link, { to: `/tasks/${task.task_id}`, className: "hover:underline hover:text-[#d4ff4d] transition-colors inline-flex items-center gap-1", onClick: (e) => e.stopPropagation(), title: "Open task detail page", children: [task.task_id, _jsx(ExternalLink, { className: "w-2.5 h-2.5 opacity-50", "aria-hidden": "true" })] })] }) }), _jsx("td", { className: "p-4 text-slate-300 text-sm max-w-xs truncate", children: task.title }), _jsx("td", { className: "p-4", children: _jsx("span", { className: "font-mono text-[10px] font-medium rounded-full px-2.5 py-0.5", style: {
                                        color: stageColor,
                                        backgroundColor: stageColor + "1A",
                                    }, children: task.stage }) }), _jsx("td", { className: "p-4 text-slate-400 text-xs", children: task.classification?.type ?? "\u2014" }), _jsx("td", { className: "p-4", children: _jsx("span", { className: "font-mono text-[10px] rounded-full px-2.5 py-0.5", style: {
                                        color: riskColor,
                                        backgroundColor: riskColor + "1A",
                                    }, children: task.classification?.risk_level ?? "\u2014" }) }), _jsx("td", { className: "p-4 text-slate-300 font-mono text-xs", children: qualityDisplay }), _jsx("td", { className: "p-4", children: _jsx(TimelineIndicator, { stage: task.stage }) }), _jsx("td", { className: "p-4", children: _jsx("span", { className: "rounded-full px-2.5 py-0.5 text-[10px] border font-mono", style: {
                                        color: stageColor,
                                        borderColor: stageColor + "4D",
                                        backgroundColor: stageColor + "1A",
                                    }, children: task.blocked_reason ? "BLOCKED" : task.stage }) })] }) }), _jsx(CollapsibleContent, { asChild: true, children: _jsx("tr", { className: "border-b border-white/5", children: _jsx("td", { colSpan: colSpan, className: "p-0", children: _jsx(ExpandedDetail, { taskId: task.task_id }) }) }) })] }) }));
}
// ---- AC-15: Compare Tab ----
function CompareTab({ tasks, retrospectives }) {
    const [taskAId, setTaskAId] = useState("");
    const [taskBId, setTaskBId] = useState("");
    const [retroA, setRetroA] = useState(null);
    const [retroB, setRetroB] = useState(null);
    const [loadingA, setLoadingA] = useState(false);
    const [loadingB, setLoadingB] = useState(false);
    const [errorA, setErrorA] = useState(null);
    const [errorB, setErrorB] = useState(null);
    const projectUrl = useProjectFetchUrl();
    const taskOptions = useMemo(() => tasks.map((t) => t.task_id), [tasks]);
    useEffect(() => {
        if (!taskAId) {
            setRetroA(null);
            return;
        }
        // Try local retrospectives first
        const local = retrospectives?.find((r) => r.task_id === taskAId);
        if (local) {
            setRetroA(local);
            setErrorA(null);
            return;
        }
        let cancelled = false;
        setLoadingA(true);
        fetch(projectUrl(`/api/tasks/${encodeURIComponent(taskAId)}/retrospective`))
            .then((res) => {
            if (!res.ok)
                throw new Error("Failed to load retrospective");
            return res.json();
        })
            .then((data) => {
            if (!cancelled) {
                setRetroA(data);
                setErrorA(null);
            }
        })
            .catch((err) => {
            if (!cancelled)
                setErrorA(err instanceof Error ? err.message : "Failed to load");
        })
            .finally(() => { if (!cancelled)
            setLoadingA(false); });
        return () => { cancelled = true; };
    }, [taskAId, retrospectives]);
    useEffect(() => {
        if (!taskBId) {
            setRetroB(null);
            return;
        }
        const local = retrospectives?.find((r) => r.task_id === taskBId);
        if (local) {
            setRetroB(local);
            setErrorB(null);
            return;
        }
        let cancelled = false;
        setLoadingB(true);
        fetch(projectUrl(`/api/tasks/${encodeURIComponent(taskBId)}/retrospective`))
            .then((res) => {
            if (!res.ok)
                throw new Error("Failed to load retrospective");
            return res.json();
        })
            .then((data) => {
            if (!cancelled) {
                setRetroB(data);
                setErrorB(null);
            }
        })
            .catch((err) => {
            if (!cancelled)
                setErrorB(err instanceof Error ? err.message : "Failed to load");
        })
            .finally(() => { if (!cancelled)
            setLoadingB(false); });
        return () => { cancelled = true; };
    }, [taskBId, retrospectives]);
    function findTask(id) {
        return tasks.find((t) => t.task_id === id);
    }
    function renderSide(taskId, task, retro, isLoading, err) {
        if (!taskId) {
            return (_jsx("div", { className: "flex items-center justify-center h-48 text-slate-600 font-mono text-xs", children: "Select a task to compare" }));
        }
        if (isLoading) {
            return (_jsxs("div", { className: "space-y-3 p-4", role: "status", "aria-label": "Loading comparison data", children: [_jsx(Skeleton, { className: "h-4 w-40" }), _jsx(Skeleton, { className: "h-4 w-32" }), _jsx(Skeleton, { className: "h-4 w-36" }), _jsx(Skeleton, { className: "h-4 w-28" })] }));
        }
        if (err) {
            return (_jsx("div", { className: "p-4", role: "alert", children: _jsx("p", { className: "text-red-400 font-mono text-xs", children: err }) }));
        }
        const totalFindings = retro
            ? Object.values(retro.findings_by_category).reduce((a, b) => a + b, 0)
            : 0;
        return (_jsxs("div", { className: "space-y-3 p-4 font-mono text-xs", children: [_jsxs("div", { className: "flex justify-between", children: [_jsx("span", { className: "text-slate-500", children: "Quality Score" }), _jsx("span", { className: "text-[#BDF000]", children: retro ? Math.round(retro.quality_score * 100) + "%" : "--" })] }), _jsxs("div", { className: "flex justify-between", children: [_jsx("span", { className: "text-slate-500", children: "Cost Score" }), _jsx("span", { className: "text-[#B47AFF]", children: retro ? Math.round(retro.cost_score * 100) + "%" : "--" })] }), _jsxs("div", { className: "flex justify-between", children: [_jsx("span", { className: "text-slate-500", children: "Findings" }), _jsx("span", { className: "text-slate-300", children: retro ? totalFindings : "--" })] }), _jsxs("div", { className: "flex justify-between", children: [_jsx("span", { className: "text-slate-500", children: "Stage" }), _jsx("span", { style: { color: getStageColor(task?.stage ?? "") }, children: task?.stage ?? "--" })] }), _jsxs("div", { className: "flex justify-between", children: [_jsx("span", { className: "text-slate-500", children: "Type" }), _jsx("span", { className: "text-slate-300", children: task?.classification?.type ?? "--" })] }), _jsxs("div", { className: "flex justify-between", children: [_jsx("span", { className: "text-slate-500", children: "Risk" }), _jsx("span", { style: { color: getRiskColor(task?.classification?.risk_level ?? "") }, children: task?.classification?.risk_level ?? "--" })] }), _jsxs("div", { className: "flex justify-between", children: [_jsx("span", { className: "text-slate-500", children: "Repair Cycles" }), _jsx("span", { className: "text-slate-300", children: retro ? retro.repair_cycle_count : "--" })] })] }));
    }
    return (_jsxs("div", { className: "space-y-6", children: [_jsxs("div", { className: "grid grid-cols-1 md:grid-cols-2 gap-6", children: [_jsxs("div", { className: "space-y-3", children: [_jsx("label", { htmlFor: "compare-task-a", className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Task A" }), _jsxs("select", { id: "compare-task-a", value: taskAId, onChange: (e) => setTaskAId(e.target.value), "aria-label": "Select first task to compare", className: "w-full bg-[#0F1114]/60 border border-[#BDF000]/20 text-slate-200 px-3 py-2 font-mono text-xs focus:outline-none focus:border-[#BDF000] transition-colors rounded", children: [_jsx("option", { value: "", children: "Select a task" }), taskOptions.map((id) => (_jsx("option", { value: id, children: id }, id)))] }), _jsx("div", { className: "bg-[#0F1114]/40 border border-[#BDF000]/10 rounded-lg min-h-[200px]", children: renderSide(taskAId, findTask(taskAId), retroA, loadingA, errorA) })] }), _jsxs("div", { className: "space-y-3", children: [_jsx("label", { htmlFor: "compare-task-b", className: "text-slate-500 font-mono text-[10px] uppercase tracking-wider", children: "Task B" }), _jsxs("select", { id: "compare-task-b", value: taskBId, onChange: (e) => setTaskBId(e.target.value), "aria-label": "Select second task to compare", className: "w-full bg-[#0F1114]/60 border border-[#BDF000]/20 text-slate-200 px-3 py-2 font-mono text-xs focus:outline-none focus:border-[#BDF000] transition-colors rounded", children: [_jsx("option", { value: "", children: "Select a task" }), taskOptions.map((id) => (_jsx("option", { value: id, children: id }, id)))] }), _jsx("div", { className: "bg-[#0F1114]/40 border border-[#BDF000]/10 rounded-lg min-h-[200px]", children: renderSide(taskBId, findTask(taskBId), retroB, loadingB, errorB) })] })] }), !taskAId && !taskBId && tasks.length === 0 && (_jsxs("div", { className: "flex flex-col items-center justify-center py-12", role: "status", children: [_jsx(AlertCircle, { className: "w-8 h-8 text-slate-600 mb-3", "aria-hidden": "true" }), _jsx("p", { className: "text-slate-400 font-mono text-sm", children: "No tasks available to compare" }), _jsx("p", { className: "text-slate-600 font-mono text-xs mt-1", children: "Tasks will appear here once created via the CLI." })] }))] }));
}
// ---- Main Page ----
export default function TaskPipeline() {
    const { isGlobal } = useProject();
    const [search, setSearch] = useState("");
    const { data: tasks, loading, error, refetch, } = usePollingData("/api/tasks");
    const { data: retrospectives, } = usePollingData("/api/retrospectives", 10000);
    const handleSearch = useCallback((e) => {
        setSearch(e.target.value);
    }, []);
    // Sort by created_at descending, then filter by search
    const sortedAndFiltered = (() => {
        if (!tasks)
            return [];
        const sorted = [...tasks].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
        if (!search.trim())
            return sorted;
        const q = search.toLowerCase();
        return sorted.filter((t) => t.task_id.toLowerCase().includes(q) ||
            t.title.toLowerCase().includes(q));
    })();
    // Determine UI state
    const isInitialLoading = loading && tasks === null;
    const isError = error !== null && tasks === null;
    const isStaleError = error !== null && tasks !== null;
    const isEmpty = !loading && !error && tasks !== null && tasks.length === 0;
    const isSearchEmpty = sortedAndFiltered.length === 0 && !isEmpty && !isInitialLoading && !isError;
    // AC-3: Summary metrics
    const totalTasks = tasks ? tasks.length : 0;
    const activeTasks = tasks
        ? tasks.filter((t) => t.stage !== "DONE" && !t.stage.includes("FAIL") && !t.stage.includes("BLOCKED")).length
        : 0;
    const completedTasks = tasks
        ? tasks.filter((t) => t.stage === "DONE").length
        : 0;
    const failedTasks = tasks
        ? tasks.filter((t) => t.stage.includes("FAIL") || t.stage.includes("BLOCKED")).length
        : 0;
    const pipelineContent = (_jsxs(_Fragment, { children: [tasks !== null && (_jsxs("div", { className: "grid grid-cols-2 lg:grid-cols-4 gap-3 mb-5", role: "region", "aria-label": "Task summary metrics", children: [_jsx(MetricCard, { label: "Total Tasks", value: totalTasks, trend: null, icon: _jsx(ListChecks, { className: "w-3.5 h-3.5 text-[#7A776E]", "aria-hidden": "true" }), delay: 0 }), _jsx(MetricCard, { label: "Active", value: activeTasks, trend: null, icon: _jsx(Play, { className: "w-3.5 h-3.5 text-[#FF9F43]", "aria-hidden": "true" }), delay: 0.05 }), _jsx(MetricCard, { label: "Completed", value: completedTasks, trend: null, icon: _jsx(CheckCircle2, { className: "w-3.5 h-3.5 text-[#2DD4A8]", "aria-hidden": "true" }), delay: 0.1 }), _jsx(MetricCard, { label: "Failed", value: failedTasks, trend: null, icon: _jsx(XCircle, { className: "w-3.5 h-3.5 text-[#FF3B3B]", "aria-hidden": "true" }), delay: 0.15 })] })), _jsx("div", { className: "flex gap-4 mb-6 relative z-10", children: _jsxs("div", { className: "relative flex-1 max-w-sm", children: [_jsx(Search, { className: "absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[#BDF000]/50", "aria-hidden": "true" }), _jsx("input", { type: "text", placeholder: "Search task ID or title...", value: search, onChange: handleSearch, "aria-label": "Search tasks by ID or title", className: "w-full bg-[#0F1114]/60 border border-[#BDF000]/20 text-slate-200 placeholder-slate-600 px-10 py-2 font-mono text-xs focus:outline-none focus:border-[#BDF000] transition-colors rounded" })] }) }), isStaleError && _jsx(StaleErrorBanner, { message: error, onRetry: refetch }), isInitialLoading && _jsx(SkeletonTable, { rows: 6 }), isError && _jsx(ErrorCard, { message: error, onRetry: refetch }), isEmpty && _jsx(EmptyState, {}), isSearchEmpty && (_jsxs("div", { className: "flex flex-col items-center justify-center py-16", role: "status", children: [_jsx(Search, { className: "w-8 h-8 text-slate-600 mb-3", "aria-hidden": "true" }), _jsx("p", { className: "text-slate-400 font-mono text-sm", children: "No tasks found" }), _jsx("p", { className: "text-slate-600 font-mono text-xs mt-1", children: "No tasks match the current search filter." })] })), sortedAndFiltered.length > 0 && (_jsx("div", { className: "flex-1 overflow-auto bg-[#0F1114]/40 border border-[#BDF000]/10 backdrop-blur-sm rounded relative", children: _jsxs("table", { className: "w-full text-left border-collapse", children: [_jsx("thead", { children: _jsxs("tr", { className: "border-b border-white/5 bg-white/5 font-mono text-xs text-slate-500", children: [isGlobal && (_jsx("th", { className: "p-4 font-normal", scope: "col", children: _jsxs("span", { className: "inline-flex items-center gap-1", children: ["PROJECT ", _jsx(ChevronUp, { className: "w-2.5 h-2.5 text-slate-600", "aria-hidden": "true" })] }) })), _jsx("th", { className: "p-4 font-normal", scope: "col", children: _jsxs("span", { className: "inline-flex items-center gap-1", children: ["TASK ID ", _jsx(ChevronUp, { className: "w-2.5 h-2.5 text-slate-600", "aria-hidden": "true" })] }) }), _jsx("th", { className: "p-4 font-normal", scope: "col", children: _jsxs("span", { className: "inline-flex items-center gap-1", children: ["TITLE ", _jsx(ChevronUp, { className: "w-2.5 h-2.5 text-slate-600", "aria-hidden": "true" })] }) }), _jsx("th", { className: "p-4 font-normal", scope: "col", children: _jsxs("span", { className: "inline-flex items-center gap-1", children: ["STAGE ", _jsx(ChevronUp, { className: "w-2.5 h-2.5 text-slate-600", "aria-hidden": "true" })] }) }), _jsx("th", { className: "p-4 font-normal", scope: "col", children: _jsxs("span", { className: "inline-flex items-center gap-1", children: ["TYPE ", _jsx(ChevronUp, { className: "w-2.5 h-2.5 text-slate-600", "aria-hidden": "true" })] }) }), _jsx("th", { className: "p-4 font-normal", scope: "col", children: _jsxs("span", { className: "inline-flex items-center gap-1", children: ["RISK ", _jsx(ChevronUp, { className: "w-2.5 h-2.5 text-slate-600", "aria-hidden": "true" })] }) }), _jsx("th", { className: "p-4 font-normal", scope: "col", children: _jsxs("span", { className: "inline-flex items-center gap-1", children: ["QUALITY SCORE ", _jsx(ChevronUp, { className: "w-2.5 h-2.5 text-slate-600", "aria-hidden": "true" })] }) }), _jsx("th", { className: "p-4 font-normal", scope: "col", children: "TIMELINE" }), _jsx("th", { className: "p-4 font-normal", scope: "col", children: "STATUS" })] }) }), _jsx("tbody", { className: "font-mono text-sm", children: sortedAndFiltered.map((task, idx) => (_jsx(TaskRow, { task: task, qualityDisplay: formatQualityScore(retrospectives, task.task_id), isGlobal: isGlobal, index: idx }, task.task_id))) })] }) }))] }));
    return (_jsxs("div", { className: "p-6 sm:p-8 h-full flex flex-col", children: [_jsxs("header", { className: "mb-8", children: [_jsx("h1", { className: "text-3xl font-mono font-light tracking-[0.2em] text-[#B47AFF]", children: "TASK PIPELINE" }), _jsx("p", { className: "text-slate-500 font-mono text-xs mt-2", children: "// LIFECYCLE TRACKING & EXECUTION STATUS" })] }), _jsxs(Tabs, { defaultValue: "pipeline", className: "flex-1 flex flex-col", children: [_jsxs(TabsList, { className: "bg-[#0F1114]/60 border border-[#BDF000]/10 mb-6", children: [_jsx(TabsTrigger, { value: "pipeline", className: "data-[state=active]:bg-[#BDF000]/10 data-[state=active]:text-[#BDF000] text-slate-500 font-mono text-xs tracking-wider", children: "PIPELINE" }), _jsx(TabsTrigger, { value: "compare", className: "data-[state=active]:bg-[#B47AFF]/10 data-[state=active]:text-[#B47AFF] text-slate-500 font-mono text-xs tracking-wider", children: "COMPARE" })] }), _jsx(TabsContent, { value: "pipeline", className: "flex-1 flex flex-col", children: pipelineContent }), _jsxs(TabsContent, { value: "compare", children: [isInitialLoading && (_jsxs("div", { className: "space-y-3", role: "status", "aria-label": "Loading tasks for comparison", children: [_jsx(Skeleton, { className: "h-8 w-64" }), _jsx(Skeleton, { className: "h-8 w-64" })] })), isError && _jsx(ErrorCard, { message: error, onRetry: refetch }), tasks !== null && (_jsx(CompareTab, { tasks: tasks, retrospectives: retrospectives }))] })] })] }));
}
