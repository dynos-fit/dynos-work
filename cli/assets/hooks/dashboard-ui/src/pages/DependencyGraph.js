import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useState, useCallback, useMemo, useEffect } from "react";
import { ReactFlow, Background, Controls, MiniMap, useNodesState, useEdgesState, } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { X, GitBranch, Terminal, ChevronDown } from "lucide-react";
import { usePollingData } from "@/data/hooks";
import { Skeleton } from "@/components/ui/skeleton";
// ---- Constants ----
const EXECUTOR_COLORS = {
    "ui-executor": "#BDF000",
    "backend-executor": "#2DD4A8",
    "ml-executor": "#B47AFF",
    "test-executor": "#FF9F43",
    "infra-executor": "#FF3B3B",
};
const FALLBACK_EXECUTOR_COLOR = "#64748b";
const NODE_WIDTH = 200;
const NODE_HEIGHT = 80;
const HORIZONTAL_GAP = 60;
const VERTICAL_GAP = 100;
const MAX_DESCRIPTION_LENGTH = 50;
// ---- Helpers ----
function getExecutorColor(executor) {
    return EXECUTOR_COLORS[executor] ?? FALLBACK_EXECUTOR_COLOR;
}
function truncateText(text, maxLength) {
    if (text.length <= maxLength)
        return text;
    return text.slice(0, maxLength - 1) + "\u2026";
}
// ---- Auto-layout: simple topological layering ----
function computeLayout(segments) {
    const positions = new Map();
    if (segments.length === 0)
        return positions;
    const idSet = new Set(segments.map((s) => s.id));
    const layers = [];
    const assigned = new Set();
    // Assign layers via topological sort
    // Layer 0: segments with no dependencies (or deps outside the graph)
    while (assigned.size < segments.length) {
        const layer = [];
        for (const seg of segments) {
            if (assigned.has(seg.id))
                continue;
            const unmetDeps = (seg.depends_on ?? []).filter((d) => idSet.has(d) && !assigned.has(d));
            if (unmetDeps.length === 0) {
                layer.push(seg.id);
            }
        }
        // Safety: if no progress, push all remaining (cyclic guard)
        if (layer.length === 0) {
            for (const seg of segments) {
                if (!assigned.has(seg.id))
                    layer.push(seg.id);
            }
        }
        layers.push(layer);
        for (const id of layer)
            assigned.add(id);
    }
    // Position: center each layer horizontally
    for (let layerIdx = 0; layerIdx < layers.length; layerIdx++) {
        const layer = layers[layerIdx];
        const totalWidth = layer.length * NODE_WIDTH + (layer.length - 1) * HORIZONTAL_GAP;
        const startX = -totalWidth / 2;
        for (let nodeIdx = 0; nodeIdx < layer.length; nodeIdx++) {
            positions.set(layer[nodeIdx], {
                x: startX + nodeIdx * (NODE_WIDTH + HORIZONTAL_GAP),
                y: layerIdx * (NODE_HEIGHT + VERTICAL_GAP),
            });
        }
    }
    return positions;
}
// ---- Custom Node Component ----
function SegmentNode({ data }) {
    return (_jsxs("div", { className: "border-2 bg-[#0F1114]/90 backdrop-blur p-3 rounded min-w-[180px] max-w-[200px] cursor-pointer transition-shadow hover:shadow-[0_0_12px_rgba(189,240,0,0.15)]", style: { borderColor: data.color }, children: [_jsxs("div", { className: "flex items-center gap-1.5 mb-1", children: [_jsx("span", { className: "w-2 h-2 rounded-full shrink-0", style: { backgroundColor: data.color }, "aria-hidden": "true" }), _jsx("span", { className: "text-xs font-bold font-mono truncate", style: { color: data.color }, children: data.segmentId })] }), _jsx("div", { className: "text-[10px] text-slate-500 font-mono", children: data.executor }), _jsx("div", { className: "text-[10px] text-slate-400 font-mono truncate max-w-[160px]", children: truncateText(data.description, MAX_DESCRIPTION_LENGTH) })] }));
}
const nodeTypes = {
    segment: SegmentNode,
};
// ---- Skeleton Loading ----
function GraphSkeleton() {
    return (_jsxs("div", { className: "flex-1 flex flex-col items-center justify-center gap-6", role: "status", "aria-label": "Loading dependency graph", children: [_jsxs("div", { className: "flex gap-8", children: [_jsx(Skeleton, { className: "h-20 w-48 bg-[#BDF000]/5 rounded" }), _jsx(Skeleton, { className: "h-20 w-48 bg-[#BDF000]/5 rounded" })] }), _jsx(Skeleton, { className: "h-1 w-24 bg-[#BDF000]/10" }), _jsxs("div", { className: "flex gap-8", children: [_jsx(Skeleton, { className: "h-20 w-48 bg-[#B47AFF]/5 rounded" }), _jsx(Skeleton, { className: "h-20 w-48 bg-[#B47AFF]/5 rounded" }), _jsx(Skeleton, { className: "h-20 w-48 bg-[#B47AFF]/5 rounded" })] }), _jsx(Skeleton, { className: "h-1 w-24 bg-[#B47AFF]/10" }), _jsx(Skeleton, { className: "h-20 w-48 bg-[#2DD4A8]/5 rounded" })] }));
}
// ---- Error Card ----
function ErrorCard({ message, onRetry }) {
    return (_jsxs("div", { className: "flex flex-col items-center justify-center py-16 px-4 bg-red-500/10 border border-red-500/30 rounded-lg mx-auto max-w-md", role: "alert", children: [_jsx("p", { className: "text-red-400 font-mono text-sm mb-4", children: "Unable to load the dependency graph." }), _jsx("p", { className: "text-slate-500 font-mono text-xs mb-6 max-w-md text-center truncate", children: message }), _jsx("button", { onClick: onRetry, className: "px-4 py-2 bg-red-500/20 hover:bg-red-500/30 text-red-400 border border-red-500/30 font-mono text-xs rounded transition-colors", "aria-label": "Retry loading dependency graph", children: "RETRY" })] }));
}
// ---- Empty State ----
function EmptyState() {
    return (_jsxs("div", { className: "flex flex-col items-center justify-center flex-1 py-20 px-4", role: "status", children: [_jsx(GitBranch, { className: "w-10 h-10 text-slate-600 mb-4", "aria-hidden": "true" }), _jsx("p", { className: "text-slate-400 font-mono text-sm", children: "No execution graph found for this task" }), _jsx("p", { className: "text-slate-600 font-mono text-xs mt-2", children: "The execution graph is generated during the planning phase." })] }));
}
function DetailPanel({ segment, color, onClose }) {
    const dependsOn = segment.depends_on ?? [];
    const criteriaIds = segment.criteria_ids ?? [];
    const filesExpected = segment.files_expected ?? [];
    return (_jsxs("aside", { className: "w-80 border-l border-[#BDF000]/10 bg-[#0F1114]/80 backdrop-blur-sm p-5 overflow-y-auto flex flex-col shrink-0", "aria-label": `Detail panel for segment ${segment.id}`, children: [_jsxs("div", { className: "flex items-center justify-between mb-4", children: [_jsx("h3", { className: "text-sm font-mono font-bold", style: { color }, children: segment.id }), _jsx("button", { onClick: onClose, className: "p-1 text-slate-500 hover:text-slate-300 transition-colors rounded", "aria-label": "Close detail panel", children: _jsx(X, { className: "w-4 h-4" }) })] }), _jsxs("div", { className: "mb-4", children: [_jsx("span", { className: "text-[10px] text-slate-500 font-mono tracking-[0.15em] block mb-1", children: "EXECUTOR" }), _jsx("span", { className: "text-xs font-mono", style: { color }, children: segment.executor })] }), _jsxs("div", { className: "mb-4", children: [_jsx("span", { className: "text-[10px] text-slate-500 font-mono tracking-[0.15em] block mb-1", children: "DESCRIPTION" }), _jsx("p", { className: "text-xs text-slate-300 font-mono leading-relaxed break-words", children: segment.description })] }), _jsxs("div", { className: "mb-4", children: [_jsx("span", { className: "text-[10px] text-slate-500 font-mono tracking-[0.15em] block mb-1", children: "PARALLELIZABLE" }), _jsx("span", { className: `text-xs font-mono ${segment.parallelizable ? "text-[#2DD4A8]" : "text-slate-400"}`, children: segment.parallelizable ? "YES" : "NO" })] }), _jsxs("div", { className: "mb-4", children: [_jsx("span", { className: "text-[10px] text-slate-500 font-mono tracking-[0.15em] block mb-1", children: "DEPENDS ON" }), dependsOn.length === 0 ? (_jsx("span", { className: "text-xs text-slate-600 font-mono", children: "None (root node)" })) : (_jsx("div", { className: "flex flex-wrap gap-1.5", children: dependsOn.map((dep) => (_jsx("span", { className: "px-2 py-0.5 text-[10px] font-mono text-[#BDF000] border border-[#BDF000]/20 bg-[#BDF000]/5 rounded", children: dep }, dep))) }))] }), _jsxs("div", { className: "mb-4", children: [_jsx("span", { className: "text-[10px] text-slate-500 font-mono tracking-[0.15em] block mb-1", children: "CRITERIA IDS" }), criteriaIds.length === 0 ? (_jsx("span", { className: "text-xs text-slate-600 font-mono", children: "None assigned" })) : (_jsx("div", { className: "flex flex-wrap gap-1.5", children: criteriaIds.map((cid) => (_jsx("span", { className: "px-2 py-0.5 text-[10px] font-mono text-[#B47AFF] border border-[#B47AFF]/20 bg-[#B47AFF]/5 rounded", children: cid }, cid))) }))] }), _jsxs("div", { className: "mb-4", children: [_jsx("span", { className: "text-[10px] text-slate-500 font-mono tracking-[0.15em] block mb-1", children: "FILES EXPECTED" }), filesExpected.length === 0 ? (_jsx("span", { className: "text-xs text-slate-600 font-mono", children: "None specified" })) : (_jsx("div", { className: "space-y-1 max-h-48 overflow-y-auto", children: filesExpected.map((file) => (_jsx("div", { className: "text-[10px] font-mono text-slate-400 bg-white/5 px-2 py-1 rounded truncate", title: file, children: file }, file))) }))] })] }));
}
function TaskSelector({ tasks, selectedTaskId, onSelect }) {
    return (_jsxs("div", { className: "relative", children: [_jsx("label", { htmlFor: "task-selector", className: "sr-only", children: "Select task to view dependency graph" }), _jsx("select", { id: "task-selector", value: selectedTaskId, onChange: (e) => onSelect(e.target.value), "aria-label": "Select task to view dependency graph", className: "appearance-none bg-[#0F1114]/60 border border-[#BDF000]/20 text-slate-200 font-mono text-xs pl-3 pr-8 py-2 rounded focus:outline-none focus:border-[#BDF000] transition-colors cursor-pointer min-w-[220px]", children: tasks.map((task) => (_jsxs("option", { value: task.task_id, children: [task.task_id, " - ", truncateText(task.title, 40)] }, task.task_id))) }), _jsx(ChevronDown, { className: "absolute right-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-[#BDF000]/50 pointer-events-none", "aria-hidden": "true" })] }));
}
// ---- Graph Builder ----
function buildNodesAndEdges(segments) {
    const positions = computeLayout(segments);
    const nodes = segments.map((seg) => {
        const color = getExecutorColor(seg.executor);
        const pos = positions.get(seg.id) ?? { x: 0, y: 0 };
        return {
            id: seg.id,
            type: "segment",
            position: pos,
            data: {
                segmentId: seg.id,
                executor: seg.executor,
                description: seg.description,
                color,
            },
        };
    });
    const edges = [];
    for (const seg of segments) {
        for (const dep of seg.depends_on ?? []) {
            edges.push({
                id: `${dep}->${seg.id}`,
                source: dep,
                target: seg.id,
                animated: true,
                style: { stroke: "#BDF000", strokeWidth: 1.5, opacity: 0.5 },
            });
        }
    }
    return { nodes, edges };
}
// ---- Main Page ----
export default function DependencyGraph() {
    const { data: tasks, loading: tasksLoading, error: tasksError, refetch: refetchTasks, } = usePollingData("/api/tasks", 15000);
    // Sort tasks newest first
    const sortedTasks = useMemo(() => {
        if (!tasks || tasks.length === 0)
            return [];
        return [...tasks].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
    }, [tasks]);
    // Default to most recent task
    const [selectedTaskId, setSelectedTaskId] = useState("");
    useEffect(() => {
        if (sortedTasks.length > 0 && !selectedTaskId) {
            setSelectedTaskId(sortedTasks[0].task_id);
        }
    }, [sortedTasks, selectedTaskId]);
    // Fetch execution graph for selected task
    const graphUrl = selectedTaskId
        ? `/api/tasks/${encodeURIComponent(selectedTaskId)}/execution-graph`
        : "";
    const { data: graph, loading: graphLoading, error: graphError, refetch: refetchGraph, } = usePollingData(graphUrl || "/api/tasks/__none__/execution-graph", graphUrl ? 10000 : 999999);
    // Build nodes/edges from graph
    const { initialNodes, initialEdges } = useMemo(() => {
        if (!graph || !graph.segments || graph.segments.length === 0) {
            return { initialNodes: [], initialEdges: [] };
        }
        const { nodes, edges } = buildNodesAndEdges(graph.segments);
        return { initialNodes: nodes, initialEdges: edges };
    }, [graph]);
    const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
    const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
    // Sync when graph data changes
    useEffect(() => {
        setNodes(initialNodes);
        setEdges(initialEdges);
    }, [initialNodes, initialEdges, setNodes, setEdges]);
    // Selected segment detail
    const [selectedSegmentId, setSelectedSegmentId] = useState(null);
    const selectedSegment = useMemo(() => {
        if (!selectedSegmentId || !graph)
            return null;
        return graph.segments.find((s) => s.id === selectedSegmentId) ?? null;
    }, [selectedSegmentId, graph]);
    const selectedSegmentColor = useMemo(() => {
        if (!selectedSegment)
            return FALLBACK_EXECUTOR_COLOR;
        return getExecutorColor(selectedSegment.executor);
    }, [selectedSegment]);
    const handleNodeClick = useCallback((_event, node) => {
        setSelectedSegmentId(node.id);
    }, []);
    const handlePaneClick = useCallback(() => {
        setSelectedSegmentId(null);
    }, []);
    const handleTaskSelect = useCallback((taskId) => {
        setSelectedTaskId(taskId);
        setSelectedSegmentId(null);
    }, []);
    // Clear selection when task changes
    useEffect(() => {
        setSelectedSegmentId(null);
    }, [selectedTaskId]);
    // ---- Determine UI state ----
    const isInitialTasksLoading = tasksLoading && tasks === null;
    const isTasksError = tasksError !== null && tasks === null;
    const isNoTasks = !tasksLoading && !tasksError && tasks !== null && tasks.length === 0;
    const isGraphLoading = graphLoading && graph === null && selectedTaskId !== "";
    const isGraphError = graphError !== null && graph === null && selectedTaskId !== "";
    const isGraphEmpty = !graphLoading &&
        !graphError &&
        graph !== null &&
        (!graph.segments || graph.segments.length === 0);
    return (_jsxs("div", { className: "flex flex-col h-full", children: [_jsxs("header", { className: "flex items-center justify-between gap-4 px-6 py-4 border-b border-[#BDF000]/10 shrink-0 flex-wrap", children: [_jsxs("div", { className: "flex items-center gap-3", children: [_jsx(GitBranch, { className: "w-5 h-5 text-[#BDF000]", "aria-hidden": "true" }), _jsx("h1", { className: "text-xl font-mono font-light tracking-[0.2em] text-[#B47AFF]", children: "DEPENDENCY GRAPH" })] }), sortedTasks.length > 0 && (_jsx(TaskSelector, { tasks: sortedTasks, selectedTaskId: selectedTaskId, onSelect: handleTaskSelect }))] }), _jsxs("div", { className: "flex flex-1 min-h-0", children: [_jsxs("div", { className: "flex-1 relative", children: [isInitialTasksLoading && _jsx(GraphSkeleton, {}), isTasksError && (_jsx("div", { className: "flex items-center justify-center h-full p-8", children: _jsx(ErrorCard, { message: tasksError, onRetry: refetchTasks }) })), isNoTasks && (_jsxs("div", { className: "flex flex-col items-center justify-center h-full py-20 px-4", role: "status", children: [_jsx(Terminal, { className: "w-10 h-10 text-slate-600 mb-4", "aria-hidden": "true" }), _jsx("p", { className: "text-slate-400 font-mono text-sm", children: "No tasks available" }), _jsx("p", { className: "text-slate-600 font-mono text-xs mt-2", children: "Create a task via the CLI to view its dependency graph." })] })), !isInitialTasksLoading && !isTasksError && !isNoTasks && isGraphLoading && (_jsx(GraphSkeleton, {})), !isInitialTasksLoading && !isTasksError && !isNoTasks && isGraphError && (_jsx("div", { className: "flex items-center justify-center h-full p-8", children: _jsx(ErrorCard, { message: graphError, onRetry: refetchGraph }) })), !isInitialTasksLoading && !isTasksError && !isNoTasks && !isGraphLoading && !isGraphError && isGraphEmpty && (_jsx(EmptyState, {})), nodes.length > 0 && (_jsxs(ReactFlow, { nodes: nodes, edges: edges, onNodesChange: onNodesChange, onEdgesChange: onEdgesChange, onNodeClick: handleNodeClick, onPaneClick: handlePaneClick, nodeTypes: nodeTypes, fitView: true, fitViewOptions: { padding: 0.3 }, minZoom: 0.2, maxZoom: 2, proOptions: { hideAttribution: true }, "aria-label": "Dependency graph visualization", children: [_jsx(Background, { color: "#BDF000", gap: 20, size: 1, style: { opacity: 0.06 } }), _jsx(Controls, { showInteractive: false, className: "[&>button]:bg-[#0F1114]/80 [&>button]:border-[#BDF000]/20 [&>button]:text-slate-400 [&>button:hover]:bg-[#BDF000]/10 [&>button:hover]:text-[#BDF000]" }), _jsx(MiniMap, { nodeColor: (node) => {
                                            const nd = node.data;
                                            return nd?.color ?? FALLBACK_EXECUTOR_COLOR;
                                        }, maskColor: "rgba(10, 14, 23, 0.8)", style: { backgroundColor: "#0F1114" } })] }))] }), selectedSegment && (_jsx(DetailPanel, { segment: selectedSegment, color: selectedSegmentColor, onClose: () => setSelectedSegmentId(null) }))] })] }));
}
