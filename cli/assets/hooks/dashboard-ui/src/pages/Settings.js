import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { motion, AnimatePresence } from "motion/react";
import { Save, Terminal, RefreshCw, Settings as SettingsIcon, X, Check, AlertTriangle, ScrollText, } from "lucide-react";
import { usePollingData } from "@/data/hooks";
import { useProject } from "@/data/ProjectContext";
import { savePolicy, saveAutofixPolicy, daemonAction, } from "@/data/api";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "sonner";
// ---------------------------------------------------------------------------
// Styling constants
// ---------------------------------------------------------------------------
const INPUT_CLASS = "w-full bg-black/40 border border-white/10 text-slate-200 p-3 font-mono text-xs focus:outline-none focus:border-[#BDF000] transition-colors rounded-none";
const SAVE_BUTTON_CLASS = "px-6 py-2.5 bg-[#BDF000]/10 hover:bg-[#BDF000]/20 border border-[#BDF000]/30 text-[#BDF000] font-mono text-xs transition-colors flex items-center gap-2 disabled:opacity-40 disabled:cursor-not-allowed";
const LABEL_CLASS = "block text-slate-500 font-mono text-xs mb-2 tracking-wider";
const INPUT_ERROR_CLASS = "w-full bg-black/40 border border-red-500 text-slate-200 p-3 font-mono text-xs focus:outline-none focus:border-red-400 transition-colors rounded-none";
const VALIDATION_MSG_CLASS = "text-red-400 font-mono text-[10px] mt-1";
const VALIDATION_MESSAGES = {
    REQUIRED: "Required",
    MUST_BE_POSITIVE: "Must be positive",
    MUST_BE_GREATER_THAN_ZERO: "Must be greater than 0",
};
function validatePolicyField(key, value) {
    if (typeof value === "number") {
        if (key === "token_budget_multiplier") {
            if (value <= 0)
                return VALIDATION_MESSAGES.MUST_BE_GREATER_THAN_ZERO;
            return null;
        }
        if (value < 0)
            return VALIDATION_MESSAGES.MUST_BE_POSITIVE;
    }
    return null;
}
function validateAutofixField(key, value) {
    if (typeof value === "number") {
        if (value < 0)
            return VALIDATION_MESSAGES.MUST_BE_POSITIVE;
    }
    return null;
}
function hasValidationErrors(errors) {
    return Object.values(errors).some((e) => e !== null);
}
function computeDiff(server, local) {
    if (!server || !local)
        return [];
    const entries = [];
    for (const key of Object.keys(local)) {
        const oldVal = JSON.stringify(server[key]);
        const newVal = JSON.stringify(local[key]);
        if (oldVal !== newVal) {
            entries.push({
                field: key,
                oldValue: oldVal ?? "N/A",
                newValue: newVal ?? "N/A",
            });
        }
    }
    return entries;
}
// ---------------------------------------------------------------------------
// Section wrapper matching Sibyl pattern
// ---------------------------------------------------------------------------
function Section({ title, color, delay, corner = "left-0", side = "l", children, }) {
    return (_jsxs(motion.section, { initial: { opacity: 0, y: 20 }, animate: { opacity: 1, y: 0 }, transition: { delay: delay * 0.15 }, className: `border border-[${color}]/20 bg-[#0F1114]/60 backdrop-blur-md p-6 relative`, style: { borderColor: `${color}33` }, children: [_jsx("div", { className: `absolute top-0 ${corner} w-8 h-8`, style: {
                    borderTop: `1px solid ${color}4D`,
                    [`border${side === "l" ? "Left" : "Right"}`]: `1px solid ${color}4D`,
                } }), _jsx("h2", { className: "text-lg font-medium mb-6 tracking-wider font-mono", style: { color }, children: title }), children] }));
}
// ---------------------------------------------------------------------------
// Number input field
// ---------------------------------------------------------------------------
function NumberField({ label, value, onChange, step, min, ariaLabel, error, }) {
    return (_jsxs("div", { children: [_jsx("label", { className: LABEL_CLASS, children: label }), _jsx("input", { type: "number", value: value, step: step, min: min, onChange: (e) => onChange(Number(e.target.value)), className: error ? INPUT_ERROR_CLASS : INPUT_CLASS, "aria-label": ariaLabel, "aria-invalid": !!error, "aria-describedby": error ? `${ariaLabel}-error` : undefined }), error && (_jsx("p", { id: `${ariaLabel}-error`, className: VALIDATION_MSG_CLASS, role: "alert", children: error }))] }));
}
// ---------------------------------------------------------------------------
// Toggle field
// ---------------------------------------------------------------------------
function ToggleField({ label, checked, onChange, ariaLabel, }) {
    return (_jsxs("div", { className: "flex items-center justify-between py-2", children: [_jsx("label", { className: "text-slate-400 font-mono text-xs tracking-wider", children: label }), _jsx(Switch, { checked: checked, onCheckedChange: onChange, "aria-label": ariaLabel })] }));
}
// ---------------------------------------------------------------------------
// Skeleton loader for sections
// ---------------------------------------------------------------------------
function SectionSkeleton({ rows = 4 }) {
    return (_jsx("div", { className: "space-y-4", children: Array.from({ length: rows }).map((_, i) => (_jsxs("div", { children: [_jsx(Skeleton, { className: "h-3 w-32 mb-2 bg-white/5" }), _jsx(Skeleton, { className: "h-10 w-full bg-white/5" })] }, i))) }));
}
// ---------------------------------------------------------------------------
// Error state
// ---------------------------------------------------------------------------
function SectionError({ message, onRetry, }) {
    return (_jsxs("div", { className: "flex flex-col items-center gap-4 py-8 text-center", children: [_jsx("p", { className: "text-red-400 font-mono text-xs", children: "Unable to load data. Please try again." }), _jsx("p", { className: "text-slate-600 font-mono text-[10px] max-w-xs truncate", children: message }), _jsxs("button", { onClick: onRetry, className: "px-4 py-2 border border-red-400/30 text-red-400 font-mono text-xs hover:bg-red-400/10 transition-colors flex items-center gap-2", "aria-label": "Retry loading data", children: [_jsx(RefreshCw, { className: "w-3 h-3" }), "RETRY"] })] }));
}
// ---------------------------------------------------------------------------
// Diff preview overlay
// ---------------------------------------------------------------------------
function DiffPreviewPanel({ title, diffs, onConfirm, onCancel, saving, }) {
    if (diffs.length === 0) {
        return (_jsx(AnimatePresence, { children: _jsx(motion.div, { initial: { opacity: 0 }, animate: { opacity: 1 }, exit: { opacity: 0 }, className: "fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm", role: "dialog", "aria-modal": "true", "aria-label": `${title} diff preview`, children: _jsxs(motion.div, { initial: { scale: 0.95, opacity: 0 }, animate: { scale: 1, opacity: 1 }, exit: { scale: 0.95, opacity: 0 }, className: "border border-[#BDF000]/20 bg-[#0F1114]/95 backdrop-blur-md p-6 max-w-lg w-full mx-4", children: [_jsxs("h3", { className: "text-[#BDF000] font-mono text-sm tracking-wider mb-4 flex items-center gap-2", children: [_jsx(Check, { className: "w-4 h-4", "aria-hidden": "true" }), title] }), _jsx("p", { className: "text-slate-400 font-mono text-xs py-4", children: "No changes detected. All values match the server." }), _jsx("div", { className: "flex justify-end", children: _jsx("button", { onClick: onCancel, className: "px-4 py-2 border border-white/10 text-slate-400 font-mono text-xs hover:bg-white/5 transition-colors", "aria-label": "Close diff preview", children: "CLOSE" }) })] }) }) }));
    }
    return (_jsx(AnimatePresence, { children: _jsx(motion.div, { initial: { opacity: 0 }, animate: { opacity: 1 }, exit: { opacity: 0 }, className: "fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm", role: "dialog", "aria-modal": "true", "aria-label": `${title} diff preview`, children: _jsxs(motion.div, { initial: { scale: 0.95, opacity: 0 }, animate: { scale: 1, opacity: 1 }, exit: { scale: 0.95, opacity: 0 }, className: "border border-[#BDF000]/20 bg-[#0F1114]/95 backdrop-blur-md p-6 max-w-lg w-full mx-4 max-h-[80vh] overflow-y-auto", children: [_jsxs("h3", { className: "text-[#BDF000] font-mono text-sm tracking-wider mb-4 flex items-center gap-2", children: [_jsx(AlertTriangle, { className: "w-4 h-4", "aria-hidden": "true" }), title, " \u2014 REVIEW CHANGES"] }), _jsx("div", { className: "space-y-3 mb-6", children: diffs.map((d) => (_jsxs("div", { className: "border border-white/5 bg-black/40 p-3", children: [_jsx("p", { className: "text-slate-500 font-mono text-[10px] tracking-wider mb-2", children: d.field.toUpperCase() }), _jsxs("div", { className: "flex flex-col sm:flex-row gap-2 font-mono text-xs", children: [_jsxs("div", { className: "flex-1 min-w-0", children: [_jsx("span", { className: "text-red-400/70 text-[10px] block mb-1", children: "OLD" }), _jsx("span", { className: "text-red-400 break-all", children: d.oldValue })] }), _jsx("div", { className: "hidden sm:block text-slate-600 self-center", children: "\u2192" }), _jsxs("div", { className: "flex-1 min-w-0", children: [_jsx("span", { className: "text-[#BDF000]/70 text-[10px] block mb-1", children: "NEW" }), _jsx("span", { className: "text-[#BDF000] break-all", children: d.newValue })] })] })] }, d.field))) }), _jsxs("div", { className: "flex justify-end gap-3", children: [_jsxs("button", { onClick: onCancel, disabled: saving, className: "px-4 py-2 border border-white/10 text-slate-400 font-mono text-xs hover:bg-white/5 transition-colors flex items-center gap-2 disabled:opacity-40", "aria-label": "Cancel save", children: [_jsx(X, { className: "w-3 h-3", "aria-hidden": "true" }), "CANCEL"] }), _jsxs("button", { onClick: onConfirm, disabled: saving, className: "px-4 py-2 bg-[#BDF000]/10 hover:bg-[#BDF000]/20 border border-[#BDF000]/30 text-[#BDF000] font-mono text-xs transition-colors flex items-center gap-2 disabled:opacity-40", "aria-label": "Confirm save", children: [_jsx(Check, { className: "w-3 h-3", "aria-hidden": "true" }), saving ? "SAVING..." : "CONFIRM SAVE"] })] })] }) }) }));
}
// ---------------------------------------------------------------------------
// Daemon log viewer component
// ---------------------------------------------------------------------------
function DaemonLogViewer({ taskId }) {
    const logContainerRef = useRef(null);
    const [logLines, setLogLines] = useState(null);
    const [logLoading, setLogLoading] = useState(true);
    const [logError, setLogError] = useState(null);
    const { selectedProject } = useProject();
    const fetchLog = useCallback(async () => {
        if (!taskId) {
            setLogLines(null);
            setLogLoading(false);
            return;
        }
        try {
            const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/execution-log?project=${encodeURIComponent(selectedProject)}`);
            if (!res.ok) {
                setLogError("Unable to fetch daemon log.");
                return;
            }
            const text = await res.text();
            let lines;
            try {
                const parsed = JSON.parse(text);
                lines = Array.isArray(parsed) ? parsed : text.split("\n");
            }
            catch {
                lines = text.split("\n");
            }
            const last50 = lines.slice(-50);
            setLogLines(last50);
            setLogError(null);
        }
        catch {
            setLogError("Network error loading daemon log.");
        }
        finally {
            setLogLoading(false);
        }
    }, [taskId, selectedProject]);
    useEffect(() => {
        fetchLog();
        const interval = setInterval(fetchLog, 5000);
        return () => clearInterval(interval);
    }, [fetchLog]);
    // Auto-scroll to bottom on new lines
    useEffect(() => {
        if (logContainerRef.current) {
            logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
        }
    }, [logLines]);
    if (!taskId) {
        return (_jsx("div", { className: "py-4", children: _jsx("p", { className: "text-slate-500 font-mono text-xs", children: "No active task found. Daemon log will appear when a task is running." }) }));
    }
    if (logLoading && !logLines) {
        return (_jsxs("div", { className: "space-y-2", children: [_jsx(Skeleton, { className: "h-3 w-full bg-white/5" }), _jsx(Skeleton, { className: "h-3 w-4/5 bg-white/5" }), _jsx(Skeleton, { className: "h-3 w-3/5 bg-white/5" }), _jsx(Skeleton, { className: "h-3 w-full bg-white/5" })] }));
    }
    if (logError && !logLines) {
        return (_jsxs("div", { className: "flex flex-col items-center gap-3 py-6 text-center", children: [_jsx("p", { className: "text-red-400 font-mono text-xs", children: "Unable to load daemon log. Please try again." }), _jsxs("button", { onClick: fetchLog, className: "px-4 py-2 border border-red-400/30 text-red-400 font-mono text-xs hover:bg-red-400/10 transition-colors flex items-center gap-2", "aria-label": "Retry loading daemon log", children: [_jsx(RefreshCw, { className: "w-3 h-3" }), "RETRY"] })] }));
    }
    if (!logLines || logLines.length === 0) {
        return (_jsx("div", { className: "py-4", children: _jsx("p", { className: "text-slate-500 font-mono text-xs", children: "Daemon log is empty. Output will appear as the task progresses." }) }));
    }
    return (_jsx("pre", { ref: logContainerRef, className: "bg-black/80 border border-white/10 p-4 font-mono text-xs text-[#BDF000] max-h-64 overflow-y-auto whitespace-pre-wrap", "aria-label": "Daemon execution log", children: logLines.join("\n") }));
}
// ---------------------------------------------------------------------------
// Main Settings page
// ---------------------------------------------------------------------------
export default function Settings() {
    const { selectedProject, isGlobal } = useProject();
    // Fetch policy data
    const { data: policyData, loading: policyLoading, error: policyError, refetch: refetchPolicy, } = usePollingData("/api/policy", 10000);
    const { data: autofixData, loading: autofixLoading, error: autofixError, refetch: refetchAutofix, } = usePollingData("/api/autofix-policy", 10000);
    const { data: registryData, loading: registryLoading, error: registryError, refetch: refetchRegistry, } = usePollingData("/api/registry", 15000);
    // ---- Fetch task list for daemon log viewer ----
    const { data: tasksData, } = usePollingData("/api/tasks", 10000);
    const mostRecentTaskId = useMemo(() => {
        if (!tasksData || tasksData.length === 0)
            return null;
        const sorted = [...tasksData].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
        return sorted[0].task_id;
    }, [tasksData]);
    // ---- Task Policy local state ----
    const [policy, setPolicy] = useState(null);
    const [policySaving, setPolicySaving] = useState(false);
    const [policyErrors, setPolicyErrors] = useState({});
    const [showPolicyDiff, setShowPolicyDiff] = useState(false);
    useEffect(() => {
        if (policyData)
            setPolicy({ ...policyData });
    }, [policyData]);
    const updatePolicy = useCallback((key, value) => {
        setPolicy((prev) => (prev ? { ...prev, [key]: value } : prev));
        // Run validation onChange
        const error = validatePolicyField(key, value);
        setPolicyErrors((prev) => ({ ...prev, [key]: error }));
    }, []);
    const handleSavePolicyClick = useCallback(() => {
        if (!policy || isGlobal)
            return;
        if (hasValidationErrors(policyErrors)) {
            toast.error("Fix validation errors before saving.");
            return;
        }
        setShowPolicyDiff(true);
    }, [policy, isGlobal, policyErrors]);
    const handleConfirmSavePolicy = useCallback(async () => {
        if (!policy || isGlobal)
            return;
        setPolicySaving(true);
        try {
            const res = await savePolicy(selectedProject, policy);
            if (res.ok) {
                toast.success("Policy saved");
                setShowPolicyDiff(false);
            }
            else {
                toast.error("Save failed: unexpected response");
            }
        }
        catch (err) {
            const message = err instanceof Error ? err.message : "Unknown error";
            toast.error(`Save failed: ${message}`);
        }
        finally {
            setPolicySaving(false);
        }
    }, [policy, selectedProject, isGlobal]);
    const policyDiffs = useMemo(() => computeDiff(policyData, policy), [policyData, policy]);
    // ---- Autofix Policy local state ----
    const [autofix, setAutofix] = useState(null);
    const [autofixSaving, setAutofixSaving] = useState(false);
    const [autofixErrors, setAutofixErrors] = useState({});
    const [showAutofixDiff, setShowAutofixDiff] = useState(false);
    useEffect(() => {
        if (autofixData)
            setAutofix({ ...autofixData });
    }, [autofixData]);
    const updateAutofix = useCallback((key, value) => {
        setAutofix((prev) => (prev ? { ...prev, [key]: value } : prev));
        const error = validateAutofixField(key, value);
        setAutofixErrors((prev) => ({ ...prev, [key]: error }));
    }, []);
    const updateCategory = useCallback((cat, field, value) => {
        setAutofix((prev) => {
            if (!prev)
                return prev;
            const existing = prev.categories[cat] ?? {
                enabled: false,
                mode: "issue-only",
            };
            return {
                ...prev,
                categories: {
                    ...prev.categories,
                    [cat]: { ...existing, [field]: value },
                },
            };
        });
    }, []);
    const handleSaveAutofixClick = useCallback(() => {
        if (!autofix || isGlobal)
            return;
        if (hasValidationErrors(autofixErrors)) {
            toast.error("Fix validation errors before saving.");
            return;
        }
        setShowAutofixDiff(true);
    }, [autofix, isGlobal, autofixErrors]);
    const handleConfirmSaveAutofix = useCallback(async () => {
        if (!autofix || isGlobal)
            return;
        setAutofixSaving(true);
        try {
            const res = await saveAutofixPolicy(selectedProject, autofix);
            if (res.ok) {
                toast.success("Policy saved");
                setShowAutofixDiff(false);
            }
            else {
                toast.error("Save failed: unexpected response");
            }
        }
        catch (err) {
            const message = err instanceof Error ? err.message : "Unknown error";
            toast.error(`Save failed: ${message}`);
        }
        finally {
            setAutofixSaving(false);
        }
    }, [autofix, selectedProject, isGlobal]);
    const autofixDiffs = useMemo(() => computeDiff(autofixData, autofix), [autofixData, autofix]);
    // ---- Daemon Controls state ----
    const [daemonOutput, setDaemonOutput] = useState("");
    const [daemonLoading, setDaemonLoading] = useState(false);
    const [taskDirInput, setTaskDirInput] = useState("");
    const handleDaemonAction = useCallback(async (action, taskDir) => {
        if (isGlobal)
            return;
        setDaemonLoading(true);
        setDaemonOutput("");
        try {
            const res = await daemonAction(selectedProject, action, taskDir);
            const output = [res.stdout, res.stderr].filter(Boolean).join("\n");
            setDaemonOutput(output || (res.ok ? "Command completed successfully." : "No output returned."));
        }
        catch (err) {
            const message = err instanceof Error ? err.message : "Unknown error";
            setDaemonOutput(`Error: ${message}`);
            toast.error(`Daemon action failed: ${message}`);
        }
        finally {
            setDaemonLoading(false);
        }
    }, [selectedProject, isGlobal]);
    return (_jsxs("div", { className: "p-8 max-w-4xl mx-auto h-full overflow-y-auto pb-24", children: [_jsxs("header", { className: "mb-12", children: [_jsxs("h1", { className: "text-3xl font-mono font-light tracking-[0.2em] text-slate-300 flex items-center gap-4", children: [_jsx(SettingsIcon, { className: "w-8 h-8 text-[#BDF000]", "aria-hidden": "true" }), "SETTINGS"] }), _jsx("p", { className: "text-slate-500 font-mono text-xs mt-2", children: "// POLICY CONFIGURATION & DAEMON CONTROLS" })] }), isGlobal && (_jsx(motion.div, { initial: { opacity: 0 }, animate: { opacity: 1 }, className: "mb-8 border border-[#BDF000]/20 bg-[#BDF000]/5 p-4 font-mono text-xs text-[#BDF000]", role: "alert", children: "Select a specific project to edit settings." })), _jsxs("div", { className: "space-y-8", children: [_jsx(Section, { title: "TASK POLICY", color: "#BDF000", delay: 1, corner: "left-0", side: "l", children: policyLoading ? (_jsx(SectionSkeleton, { rows: 7 })) : policyError && !policy ? (_jsx(SectionError, { message: policyError, onRetry: refetchPolicy })) : policy ? (_jsxs("div", { className: "space-y-4", children: [_jsx(NumberField, { label: "FRESHNESS_TASK_WINDOW", value: policy.freshness_task_window, onChange: (v) => updatePolicy("freshness_task_window", v), min: 1, ariaLabel: "Freshness task window", error: policyErrors.freshness_task_window }), _jsx(NumberField, { label: "ACTIVE_REBENCHMARK_TASK_WINDOW", value: policy.active_rebenchmark_task_window, onChange: (v) => updatePolicy("active_rebenchmark_task_window", v), min: 1, ariaLabel: "Active rebenchmark task window", error: policyErrors.active_rebenchmark_task_window }), _jsx(NumberField, { label: "SHADOW_REBENCHMARK_TASK_WINDOW", value: policy.shadow_rebenchmark_task_window, onChange: (v) => updatePolicy("shadow_rebenchmark_task_window", v), min: 1, ariaLabel: "Shadow rebenchmark task window", error: policyErrors.shadow_rebenchmark_task_window }), _jsx(ToggleField, { label: "MAINTAINER_AUTOSTART", checked: policy.maintainer_autostart, onChange: (v) => updatePolicy("maintainer_autostart", v), ariaLabel: "Maintainer autostart toggle" }), _jsx(NumberField, { label: "MAINTAINER_POLL_SECONDS", value: policy.maintainer_poll_seconds, onChange: (v) => updatePolicy("maintainer_poll_seconds", v), min: 10, ariaLabel: "Maintainer poll seconds", error: policyErrors.maintainer_poll_seconds }), _jsx(ToggleField, { label: "FAST_TRACK_SKIP_PLAN_AUDIT", checked: policy.fast_track_skip_plan_audit, onChange: (v) => updatePolicy("fast_track_skip_plan_audit", v), ariaLabel: "Fast track skip plan audit toggle" }), _jsx(NumberField, { label: "TOKEN_BUDGET_MULTIPLIER", value: policy.token_budget_multiplier, onChange: (v) => updatePolicy("token_budget_multiplier", v), step: 0.1, min: 0.1, ariaLabel: "Token budget multiplier", error: policyErrors.token_budget_multiplier }), _jsx("div", { className: "flex justify-end pt-4", children: _jsxs("button", { onClick: handleSavePolicyClick, disabled: isGlobal || policySaving || hasValidationErrors(policyErrors), className: SAVE_BUTTON_CLASS, "aria-label": "Save task policy", children: [_jsx(Save, { className: "w-4 h-4", "aria-hidden": "true" }), policySaving ? "SAVING..." : "SAVE TASK POLICY"] }) })] })) : (_jsx("p", { className: "text-slate-500 font-mono text-xs py-4", children: "No policy data available. Ensure the daemon is running." })) }), _jsx(Section, { title: "AUTOFIX POLICY", color: "#B47AFF", delay: 2, corner: "right-0", side: "r", children: autofixLoading ? (_jsx(SectionSkeleton, { rows: 5 })) : autofixError && !autofix ? (_jsx(SectionError, { message: autofixError, onRetry: refetchAutofix })) : autofix ? (_jsxs("div", { className: "space-y-4", children: [_jsx(NumberField, { label: "MAX_PRS_PER_DAY", value: autofix.max_prs_per_day, onChange: (v) => updateAutofix("max_prs_per_day", v), min: 0, ariaLabel: "Maximum PRs per day", error: autofixErrors.max_prs_per_day }), _jsx(NumberField, { label: "MAX_OPEN_PRS", value: autofix.max_open_prs, onChange: (v) => updateAutofix("max_open_prs", v), min: 0, ariaLabel: "Maximum open PRs", error: autofixErrors.max_open_prs }), _jsx(NumberField, { label: "COOLDOWN_AFTER_FAILURES", value: autofix.cooldown_after_failures, onChange: (v) => updateAutofix("cooldown_after_failures", v), min: 0, ariaLabel: "Cooldown after failures", error: autofixErrors.cooldown_after_failures }), _jsx(ToggleField, { label: "ALLOW_DEPENDENCY_FILE_CHANGES", checked: autofix.allow_dependency_file_changes, onChange: (v) => updateAutofix("allow_dependency_file_changes", v), ariaLabel: "Allow dependency file changes toggle" }), Object.keys(autofix.categories).length > 0 && (_jsxs("div", { className: "mt-6", children: [_jsx("h3", { className: "text-slate-500 font-mono text-xs tracking-wider mb-4 border-b border-white/5 pb-2", children: "CATEGORY CONTROLS" }), _jsx("div", { className: "space-y-4", children: Object.entries(autofix.categories).map(([category, config]) => (_jsxs("div", { className: "flex items-center gap-4 py-2 border-b border-white/5 last:border-b-0", children: [_jsx("span", { className: "text-slate-300 font-mono text-xs flex-1 min-w-0 truncate", title: category, children: category }), _jsx(Switch, { checked: config.enabled, onCheckedChange: (v) => updateCategory(category, "enabled", v), "aria-label": `Enable ${category}` }), _jsxs("select", { value: config.mode, onChange: (e) => updateCategory(category, "mode", e.target.value), className: "bg-black/40 border border-white/10 text-slate-200 font-mono text-xs p-2 focus:outline-none focus:border-[#B47AFF] transition-colors", "aria-label": `Mode for ${category}`, children: [_jsx("option", { value: "autofix", children: "autofix" }), _jsx("option", { value: "issue-only", children: "issue-only" })] })] }, category))) })] })), _jsx("div", { className: "flex justify-end pt-4", children: _jsxs("button", { onClick: handleSaveAutofixClick, disabled: isGlobal || autofixSaving || hasValidationErrors(autofixErrors), className: SAVE_BUTTON_CLASS, "aria-label": "Save autofix policy", children: [_jsx(Save, { className: "w-4 h-4", "aria-hidden": "true" }), autofixSaving ? "SAVING..." : "SAVE AUTOFIX POLICY"] }) })] })) : (_jsx("p", { className: "text-slate-500 font-mono text-xs py-4", children: "No autofix policy data available. Ensure the daemon is running." })) }), _jsx(Section, { title: "REGISTERED PROJECTS", color: "#2DD4A8", delay: 3, corner: "left-0", side: "l", children: registryLoading ? (_jsx(SectionSkeleton, { rows: 3 })) : registryError && !registryData ? (_jsx(SectionError, { message: registryError, onRetry: refetchRegistry })) : registryData && registryData.projects.length > 0 ? (_jsx("div", { className: "space-y-3", children: registryData.projects.map((project) => (_jsxs("div", { className: "flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-4 py-3 border-b border-white/5 last:border-b-0", children: [_jsx("span", { className: "text-slate-200 font-mono text-xs flex-1 min-w-0 truncate", title: project.path, children: project.path }), _jsxs("div", { className: "flex items-center gap-4 text-slate-500 font-mono text-[10px]", children: [_jsx("span", { className: `px-2 py-0.5 border ${project.status === "active"
                                                    ? "border-[#2DD4A8]/30 text-[#2DD4A8]"
                                                    : "border-white/10 text-slate-500"}`, children: project.status.toUpperCase() }), _jsx("span", { title: "Last active at", children: project.last_active_at })] })] }, project.path))) })) : (_jsx("p", { className: "text-slate-500 font-mono text-xs py-4", children: "No projects registered. Use the CLI to register a project." })) }), !isGlobal && (_jsx(Section, { title: "DAEMON CONTROLS", color: "#BDF000", delay: 4, corner: "right-0", side: "r", children: _jsxs("div", { className: "space-y-4", children: [_jsxs("div", { className: "flex flex-wrap gap-3", children: [_jsxs("button", { onClick: () => handleDaemonAction("status"), disabled: daemonLoading, className: SAVE_BUTTON_CLASS, "aria-label": "Check daemon status", children: [_jsx(RefreshCw, { className: `w-4 h-4 ${daemonLoading ? "animate-spin" : ""}`, "aria-hidden": "true" }), "CHECK STATUS"] }), _jsxs("div", { className: "flex items-center gap-2", children: [_jsx("input", { type: "text", value: taskDirInput, onChange: (e) => setTaskDirInput(e.target.value), placeholder: "task dir (optional)", className: `${INPUT_CLASS} max-w-[200px]`, "aria-label": "Task directory for validation" }), _jsxs("button", { onClick: () => handleDaemonAction("validate", taskDirInput || undefined), disabled: daemonLoading, className: SAVE_BUTTON_CLASS, "aria-label": "Validate current task", children: [_jsx(Terminal, { className: "w-4 h-4", "aria-hidden": "true" }), "VALIDATE CURRENT TASK"] })] })] }), (daemonOutput || daemonLoading) && (_jsx("div", { className: "mt-4", children: daemonLoading ? (_jsxs("div", { className: "bg-black/60 border border-white/10 p-4 flex items-center gap-2", children: [_jsx(RefreshCw, { className: "w-3 h-3 animate-spin text-[#BDF000]", "aria-hidden": "true" }), _jsx("span", { className: "text-slate-500 font-mono text-xs", children: "Running command..." })] })) : (_jsx("pre", { className: "bg-black/60 border border-white/10 p-4 text-slate-300 font-mono text-xs overflow-x-auto whitespace-pre-wrap max-h-64 overflow-y-auto", "aria-label": "Daemon command output", children: daemonOutput })) }))] }) })), !isGlobal && (_jsxs(Section, { title: "DAEMON LOG", color: "#BDF000", delay: 5, corner: "left-0", side: "l", children: [_jsxs("div", { className: "flex items-center gap-2 mb-4", children: [_jsx(ScrollText, { className: "w-4 h-4 text-[#BDF000]", "aria-hidden": "true" }), _jsx("span", { className: "text-slate-500 font-mono text-[10px] tracking-wider", children: mostRecentTaskId
                                            ? `TASK: ${mostRecentTaskId}`
                                            : "WAITING FOR TASK" })] }), _jsx(DaemonLogViewer, { taskId: mostRecentTaskId })] }))] }), showPolicyDiff && (_jsx(DiffPreviewPanel, { title: "TASK POLICY", diffs: policyDiffs, onConfirm: handleConfirmSavePolicy, onCancel: () => setShowPolicyDiff(false), saving: policySaving })), showAutofixDiff && (_jsx(DiffPreviewPanel, { title: "AUTOFIX POLICY", diffs: autofixDiffs, onConfirm: handleConfirmSaveAutofix, onCancel: () => setShowAutofixDiff(false), saving: autofixSaving }))] }));
}
