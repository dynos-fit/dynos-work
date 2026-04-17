import { jsx as _jsx, Fragment as _Fragment, jsxs as _jsxs } from "react/jsx-runtime";
import { useState, useEffect, useRef, useMemo, useCallback, } from "react";
import { motion, AnimatePresence } from "motion/react";
import { Search, Terminal as TerminalIcon, ArrowDown, X, AlertCircle } from "lucide-react";
import { usePollingData } from "@/data/hooks";
import { Skeleton } from "@/components/ui/skeleton";
const LINE_RULES = [
    { pattern: /\[FAIL\]|\[ERROR\]/, className: "text-red-400" },
    { pattern: /\[WARN\]/, className: "text-amber-400" },
    { pattern: /\[SPAWN\]|\[ROUTE\]/, className: "text-[#B47AFF]" },
    { pattern: /\[HUMAN\]/, className: "text-emerald-400" },
    { pattern: /\[STAGE\]|\[ADVANCE\]/, className: "text-[#2DD4A8]" },
    { pattern: /\[DONE\]/, className: "text-[#2DD4A8]" },
];
const TIMESTAMP_REGEX = /^(\d{4}-\d{2}-\d{2}T[\d:.]+Z?)/;
const SEGMENT_ID_REGEX = /(seg-\d+)/g;
/**
 * Determine the dominant color class for a log line based on bracket tags.
 * Falls back to "text-slate-400" for unclassified lines.
 */
function getLineClass(line) {
    for (const rule of LINE_RULES) {
        if (rule.pattern.test(line))
            return rule.className;
    }
    return "text-slate-400";
}
/**
 * Render a single log line with syntax highlighting applied:
 * - Timestamps at line start get cyan
 * - Segment IDs get purple
 * - Bracket tags set the line's dominant color
 * - Search matches get a <mark> highlight
 */
function renderHighlightedLine(line, searchTerm) {
    const lineClass = getLineClass(line);
    // Split line into: [timestamp, rest]
    const tsMatch = line.match(TIMESTAMP_REGEX);
    const timestamp = tsMatch ? tsMatch[1] : null;
    const rest = timestamp ? line.slice(timestamp.length) : line;
    // Build fragments from the rest, splitting on seg-N patterns
    const fragments = [];
    let lastIndex = 0;
    let matchArr;
    const segRegex = new RegExp(SEGMENT_ID_REGEX.source, "g");
    while ((matchArr = segRegex.exec(rest)) !== null) {
        if (matchArr.index > lastIndex) {
            fragments.push(_jsx("span", { className: lineClass, children: rest.slice(lastIndex, matchArr.index) }, `t-${lastIndex}`));
        }
        fragments.push(_jsx("span", { className: "text-[#B47AFF]", children: matchArr[0] }, `s-${matchArr.index}`));
        lastIndex = matchArr.index + matchArr[0].length;
    }
    if (lastIndex < rest.length) {
        fragments.push(_jsx("span", { className: lineClass, children: rest.slice(lastIndex) }, `t-${lastIndex}`));
    }
    const fullLine = (_jsxs(_Fragment, { children: [timestamp && (_jsx("span", { className: "text-[#BDF000]", children: timestamp })), fragments] }));
    // If no search, return directly
    if (!searchTerm)
        return fullLine;
    // With search active, wrap the whole rendered line and apply <mark> on the raw text
    // We need to re-render with mark highlights
    return _jsx(SearchHighlightedLine, { line: line, searchTerm: searchTerm, lineClass: lineClass });
}
/**
 * Renders a line with search term highlighted via <mark> tags.
 * Still applies syntax coloring per-segment.
 */
function SearchHighlightedLine({ line, searchTerm, lineClass, }) {
    const parts = [];
    const lowerLine = line.toLowerCase();
    const lowerSearch = searchTerm.toLowerCase();
    let cursor = 0;
    while (cursor < line.length) {
        const idx = lowerLine.indexOf(lowerSearch, cursor);
        if (idx === -1) {
            parts.push(_jsx(HighlightedSegment, { text: line.slice(cursor), lineClass: lineClass }, `r-${cursor}`));
            break;
        }
        if (idx > cursor) {
            parts.push(_jsx(HighlightedSegment, { text: line.slice(cursor, idx), lineClass: lineClass }, `r-${cursor}`));
        }
        parts.push(_jsx("mark", { className: "bg-[#BDF000]/30 text-[#BDF000] rounded-sm px-0.5", children: line.slice(idx, idx + searchTerm.length) }, `m-${idx}`));
        cursor = idx + searchTerm.length;
    }
    return _jsx(_Fragment, { children: parts });
}
/**
 * A text segment with syntax highlighting (timestamp, seg-ID, or default line class).
 */
function HighlightedSegment({ text, lineClass }) {
    const tsMatch = text.match(TIMESTAMP_REGEX);
    const timestamp = tsMatch ? tsMatch[1] : null;
    const rest = timestamp ? text.slice(timestamp.length) : text;
    const fragments = [];
    let lastIndex = 0;
    let matchArr;
    const segRegex = new RegExp(SEGMENT_ID_REGEX.source, "g");
    while ((matchArr = segRegex.exec(rest)) !== null) {
        if (matchArr.index > lastIndex) {
            fragments.push(_jsx("span", { className: lineClass, children: rest.slice(lastIndex, matchArr.index) }, `t-${lastIndex}`));
        }
        fragments.push(_jsx("span", { className: "text-[#B47AFF]", children: matchArr[0] }, `s-${matchArr.index}`));
        lastIndex = matchArr.index + matchArr[0].length;
    }
    if (lastIndex < rest.length) {
        fragments.push(_jsx("span", { className: lineClass, children: rest.slice(lastIndex) }, `t-${lastIndex}`));
    }
    return (_jsxs(_Fragment, { children: [timestamp && _jsx("span", { className: "text-[#BDF000]", children: timestamp }), fragments] }));
}
// ---- Skeleton / Loading State ----
function TerminalSkeleton() {
    return (_jsx("div", { className: "flex-1 p-4 space-y-2 overflow-hidden", role: "status", "aria-label": "Loading execution log", children: Array.from({ length: 20 }, (_, i) => (_jsx(Skeleton, { className: "h-4 bg-[#BDF000]/5", style: { width: `${40 + Math.random() * 55}%`, opacity: 1 - i * 0.03 } }, i))) }));
}
// ---- Error State ----
function TerminalError({ message, onRetry }) {
    return (_jsx("div", { className: "flex-1 flex items-center justify-center p-8", children: _jsxs("div", { className: "flex flex-col items-center justify-center py-16 px-8 bg-red-500/10 border border-red-500/30 rounded-lg max-w-md w-full", role: "alert", children: [_jsx(AlertCircle, { className: "w-10 h-10 text-red-400 mb-4", "aria-hidden": "true" }), _jsx("p", { className: "text-red-400 font-mono text-sm mb-2", children: "Unable to load execution log" }), _jsx("p", { className: "text-slate-500 font-mono text-xs mb-6 text-center max-w-xs truncate", children: message }), _jsx("button", { onClick: onRetry, className: "px-4 py-2 bg-red-500/20 hover:bg-red-500/30 text-red-400 border border-red-500/30 font-mono text-xs rounded transition-colors", "aria-label": "Retry loading execution log", children: "RETRY" })] }) }));
}
// ---- Empty State ----
function TerminalEmpty() {
    return (_jsx("div", { className: "flex-1 flex items-center justify-center p-8", role: "status", children: _jsxs("div", { className: "flex flex-col items-center", children: [_jsx(TerminalIcon, { className: "w-10 h-10 text-slate-600 mb-4", "aria-hidden": "true" }), _jsx("p", { className: "text-slate-400 font-mono text-sm", children: "No execution logs found" }), _jsx("p", { className: "text-slate-600 font-mono text-xs mt-2 text-center max-w-xs", children: "Start a task via the CLI to see real-time execution output here." })] }) }));
}
// ---- Scroll-to-bottom Button ----
function ScrollToBottomButton({ onClick }) {
    return (_jsxs(motion.button, { initial: { opacity: 0, y: 10 }, animate: { opacity: 1, y: 0 }, exit: { opacity: 0, y: 10 }, transition: { duration: 0.2 }, onClick: onClick, className: "fixed bottom-6 right-6 z-50 px-4 py-2 bg-[#0D1321] border border-[#BDF000]/30 text-[#BDF000] font-mono text-xs rounded hover:bg-[#BDF000]/10 transition-colors shadow-lg shadow-[#BDF000]/10 flex items-center gap-2", "aria-label": "Scroll to bottom of log", children: [_jsx(ArrowDown, { className: "w-3 h-3", "aria-hidden": "true" }), "SCROLL TO BOTTOM"] }));
}
// ---- Main Terminal Page ----
const POLL_INTERVAL_MS = 3000;
export default function Terminal() {
    const [search, setSearch] = useState("");
    const [autoScroll, setAutoScroll] = useState(true);
    const scrollRef = useRef(null);
    const prevLineCountRef = useRef(0);
    // Fetch tasks to find most recent
    const tasks = usePollingData("/api/tasks", POLL_INTERVAL_MS);
    const mostRecentTask = useMemo(() => {
        if (!tasks.data || tasks.data.length === 0)
            return null;
        return [...tasks.data].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())[0];
    }, [tasks.data]);
    // Fetch execution log for most recent task
    const logUrl = mostRecentTask
        ? `/api/tasks/${mostRecentTask.task_id}/execution-log`
        : "";
    const execLog = usePollingData(logUrl || "/api/tasks/__none__/execution-log", logUrl ? POLL_INTERVAL_MS : 999999);
    const allLines = execLog.data?.lines ?? [];
    // Filter lines by search
    const filteredLines = useMemo(() => {
        if (!search.trim())
            return allLines;
        const q = search.toLowerCase();
        return allLines.filter((line) => line.toLowerCase().includes(q));
    }, [allLines, search]);
    const matchCount = search.trim() ? filteredLines.length : -1;
    // Auto-scroll when new lines appear
    useEffect(() => {
        if (autoScroll && scrollRef.current && allLines.length > prevLineCountRef.current) {
            scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
        }
        prevLineCountRef.current = allLines.length;
    }, [allLines.length, autoScroll, filteredLines]);
    // Detect user scroll position
    const handleScroll = useCallback(() => {
        const el = scrollRef.current;
        if (!el)
            return;
        const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
        // If user is within 50px of bottom, resume auto-scroll
        if (distanceFromBottom < 50) {
            setAutoScroll(true);
        }
        else {
            setAutoScroll(false);
        }
    }, []);
    const scrollToBottom = useCallback(() => {
        if (scrollRef.current) {
            scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
            setAutoScroll(true);
        }
    }, []);
    const clearSearch = useCallback(() => {
        setSearch("");
    }, []);
    // Determine UI state
    const isInitialLoading = (tasks.loading && tasks.data === null) || (execLog.loading && execLog.data === null && !!logUrl);
    const hasTaskError = tasks.error !== null && tasks.data === null;
    const hasLogError = execLog.error !== null && execLog.data === null && !!logUrl;
    const isError = hasTaskError || hasLogError;
    const errorMessage = tasks.error ?? execLog.error ?? "Unable to connect to the daemon.";
    const isEmpty = !isInitialLoading && !isError && allLines.length === 0;
    const handleRetry = useCallback(() => {
        tasks.refetch();
        if (logUrl)
            execLog.refetch();
    }, [tasks, execLog, logUrl]);
    return (_jsxs("div", { className: "flex flex-col h-full bg-[#0F1114]", children: [_jsxs("header", { className: "flex items-center gap-4 px-4 py-3 border-b border-[#BDF000]/10 bg-[#0F1114]/80 backdrop-blur-sm shrink-0", children: [_jsxs("div", { className: "flex items-center gap-2 shrink-0", children: [_jsx(TerminalIcon, { className: "w-4 h-4 text-[#BDF000]", "aria-hidden": "true" }), _jsx("h1", { className: "font-mono text-xs text-[#BDF000] tracking-[0.15em]", children: "EXECUTION LOG" })] }), mostRecentTask && (_jsx("span", { className: "font-mono text-[10px] text-slate-500 truncate max-w-xs hidden sm:inline", children: mostRecentTask.task_id })), _jsx("div", { className: "flex-1" }), _jsxs("div", { className: "relative max-w-xs w-full sm:w-64", children: [_jsx(Search, { className: "absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-[#BDF000]/50", "aria-hidden": "true" }), _jsx("input", { type: "text", value: search, onChange: (e) => setSearch(e.target.value), placeholder: "Filter log lines...", "aria-label": "Search execution log lines", className: "w-full bg-[#0F1114]/60 border border-[#BDF000]/20 text-slate-200 placeholder-slate-600 pl-8 pr-16 py-1.5 font-mono text-xs focus:outline-none focus:border-[#BDF000] transition-colors rounded" }), search && (_jsxs("div", { className: "absolute right-1 top-1/2 -translate-y-1/2 flex items-center gap-1", children: [matchCount >= 0 && (_jsxs("span", { className: "font-mono text-[10px] text-[#BDF000]/70", children: [matchCount, " ", matchCount === 1 ? "match" : "matches"] })), _jsx("button", { onClick: clearSearch, className: "p-0.5 text-slate-500 hover:text-slate-300 transition-colors", "aria-label": "Clear search filter", children: _jsx(X, { className: "w-3 h-3" }) })] }))] })] }), isInitialLoading && _jsx(TerminalSkeleton, {}), isError && _jsx(TerminalError, { message: errorMessage, onRetry: handleRetry }), isEmpty && _jsx(TerminalEmpty, {}), !isInitialLoading && !isError && !isEmpty && (_jsx("div", { ref: scrollRef, onScroll: handleScroll, className: "flex-1 overflow-auto p-4 scan-line", role: "log", "aria-label": "Execution log output", "aria-live": "polite", children: _jsxs("div", { className: "font-mono text-xs leading-relaxed space-y-px", children: [filteredLines.map((line, idx) => (_jsx("div", { className: "whitespace-pre-wrap break-all py-px", children: renderHighlightedLine(line, search.trim()) }, `${idx}-${line.slice(0, 40)}`))), !search && (_jsx("div", { className: "text-[#BDF000] animate-pulse pt-1", "aria-hidden": "true", children: "_" }))] }) })), _jsx(AnimatePresence, { children: !autoScroll && !isInitialLoading && !isError && !isEmpty && (_jsx(ScrollToBottomButton, { onClick: scrollToBottom })) })] }));
}
