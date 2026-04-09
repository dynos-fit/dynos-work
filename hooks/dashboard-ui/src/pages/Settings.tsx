import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { motion, AnimatePresence } from "motion/react";
import {
  Save,
  Terminal,
  RefreshCw,
  Settings as SettingsIcon,
  X,
  Check,
  AlertTriangle,
  ScrollText,
} from "lucide-react";
import { usePollingData } from "@/data/hooks";
import { useProject } from "@/data/ProjectContext";
import {
  savePolicy,
  daemonAction,
} from "@/data/api";
import type {
  PolicyConfig,
  ProjectRegistry,
  TaskManifest,
} from "@/data/types";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "sonner";

// ---------------------------------------------------------------------------
// Styling constants
// ---------------------------------------------------------------------------

const INPUT_CLASS =
  "w-full bg-black/40 border border-white/10 text-slate-200 p-3 font-mono text-xs focus:outline-none focus:border-[#BDF000] transition-colors rounded-none";

const SAVE_BUTTON_CLASS =
  "px-6 py-2.5 bg-[#BDF000]/10 hover:bg-[#BDF000]/20 border border-[#BDF000]/30 text-[#BDF000] font-mono text-xs transition-colors flex items-center gap-2 disabled:opacity-40 disabled:cursor-not-allowed";

const LABEL_CLASS = "block text-slate-500 font-mono text-xs mb-2 tracking-wider";

const INPUT_ERROR_CLASS =
  "w-full bg-black/40 border border-red-500 text-slate-200 p-3 font-mono text-xs focus:outline-none focus:border-red-400 transition-colors rounded-none";

const VALIDATION_MSG_CLASS = "text-red-400 font-mono text-[10px] mt-1";

// ---------------------------------------------------------------------------
// Validation helpers
// ---------------------------------------------------------------------------

type ValidationErrors = Record<string, string | null>;

const VALIDATION_MESSAGES = {
  REQUIRED: "Required",
  MUST_BE_POSITIVE: "Must be positive",
  MUST_BE_GREATER_THAN_ZERO: "Must be greater than 0",
} as const;

function validatePolicyField(key: keyof PolicyConfig, value: unknown): string | null {
  if (typeof value === "number") {
    if (key === "token_budget_multiplier") {
      if (value <= 0) return VALIDATION_MESSAGES.MUST_BE_GREATER_THAN_ZERO;
      return null;
    }
    if (value < 0) return VALIDATION_MESSAGES.MUST_BE_POSITIVE;
  }
  return null;
}

function hasValidationErrors(errors: ValidationErrors): boolean {
  return Object.values(errors).some((e) => e !== null);
}

// ---------------------------------------------------------------------------
// Diff preview types
// ---------------------------------------------------------------------------

interface DiffEntry {
  field: string;
  oldValue: string;
  newValue: string;
}

function computeDiff<T extends Record<string, unknown>>(
  server: T | null,
  local: T | null,
): DiffEntry[] {
  if (!server || !local) return [];
  const entries: DiffEntry[] = [];
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

function Section({
  title,
  color,
  delay,
  corner = "left-0",
  side = "l",
  children,
}: {
  title: string;
  color: string;
  delay: number;
  corner?: string;
  side?: string;
  children: React.ReactNode;
}) {
  return (
    <motion.section
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: delay * 0.15 }}
      className={`border border-[${color}]/20 bg-[#0F1114]/60 backdrop-blur-md p-6 relative`}
      style={{ borderColor: `${color}33` }}
    >
      <div
        className={`absolute top-0 ${corner} w-8 h-8`}
        style={{
          borderTop: `1px solid ${color}4D`,
          [`border${side === "l" ? "Left" : "Right"}`]: `1px solid ${color}4D`,
        }}
      />
      <h2
        className="text-lg font-medium mb-6 tracking-wider font-mono"
        style={{ color }}
      >
        {title}
      </h2>
      {children}
    </motion.section>
  );
}

// ---------------------------------------------------------------------------
// Number input field
// ---------------------------------------------------------------------------

function NumberField({
  label,
  value,
  onChange,
  step,
  min,
  ariaLabel,
  error,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  step?: number;
  min?: number;
  ariaLabel: string;
  error?: string | null;
}) {
  return (
    <div>
      <label className={LABEL_CLASS}>{label}</label>
      <input
        type="number"
        value={value}
        step={step}
        min={min}
        onChange={(e) => onChange(Number(e.target.value))}
        className={error ? INPUT_ERROR_CLASS : INPUT_CLASS}
        aria-label={ariaLabel}
        aria-invalid={!!error}
        aria-describedby={error ? `${ariaLabel}-error` : undefined}
      />
      {error && (
        <p
          id={`${ariaLabel}-error`}
          className={VALIDATION_MSG_CLASS}
          role="alert"
        >
          {error}
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Toggle field
// ---------------------------------------------------------------------------

function ToggleField({
  label,
  checked,
  onChange,
  ariaLabel,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  ariaLabel: string;
}) {
  return (
    <div className="flex items-center justify-between py-2">
      <label className="text-slate-400 font-mono text-xs tracking-wider">
        {label}
      </label>
      <Switch
        checked={checked}
        onCheckedChange={onChange}
        aria-label={ariaLabel}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Skeleton loader for sections
// ---------------------------------------------------------------------------

function SectionSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-4">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i}>
          <Skeleton className="h-3 w-32 mb-2 bg-white/5" />
          <Skeleton className="h-10 w-full bg-white/5" />
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Error state
// ---------------------------------------------------------------------------

function SectionError({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="flex flex-col items-center gap-4 py-8 text-center">
      <p className="text-red-400 font-mono text-xs">
        Unable to load data. Please try again.
      </p>
      <p className="text-slate-600 font-mono text-[10px] max-w-xs truncate">
        {message}
      </p>
      <button
        onClick={onRetry}
        className="px-4 py-2 border border-red-400/30 text-red-400 font-mono text-xs hover:bg-red-400/10 transition-colors flex items-center gap-2"
        aria-label="Retry loading data"
      >
        <RefreshCw className="w-3 h-3" />
        RETRY
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Diff preview overlay
// ---------------------------------------------------------------------------

function DiffPreviewPanel({
  title,
  diffs,
  onConfirm,
  onCancel,
  saving,
}: {
  title: string;
  diffs: DiffEntry[];
  onConfirm: () => void;
  onCancel: () => void;
  saving: boolean;
}) {
  if (diffs.length === 0) {
    return (
      <AnimatePresence>
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
          role="dialog"
          aria-modal="true"
          aria-label={`${title} diff preview`}
        >
          <motion.div
            initial={{ scale: 0.95, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.95, opacity: 0 }}
            className="border border-[#BDF000]/20 bg-[#0F1114]/95 backdrop-blur-md p-6 max-w-lg w-full mx-4"
          >
            <h3 className="text-[#BDF000] font-mono text-sm tracking-wider mb-4 flex items-center gap-2">
              <Check className="w-4 h-4" aria-hidden="true" />
              {title}
            </h3>
            <p className="text-slate-400 font-mono text-xs py-4">
              No changes detected. All values match the server.
            </p>
            <div className="flex justify-end">
              <button
                onClick={onCancel}
                className="px-4 py-2 border border-white/10 text-slate-400 font-mono text-xs hover:bg-white/5 transition-colors"
                aria-label="Close diff preview"
              >
                CLOSE
              </button>
            </div>
          </motion.div>
        </motion.div>
      </AnimatePresence>
    );
  }

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
        role="dialog"
        aria-modal="true"
        aria-label={`${title} diff preview`}
      >
        <motion.div
          initial={{ scale: 0.95, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.95, opacity: 0 }}
          className="border border-[#BDF000]/20 bg-[#0F1114]/95 backdrop-blur-md p-6 max-w-lg w-full mx-4 max-h-[80vh] overflow-y-auto"
        >
          <h3 className="text-[#BDF000] font-mono text-sm tracking-wider mb-4 flex items-center gap-2">
            <AlertTriangle className="w-4 h-4" aria-hidden="true" />
            {title} &mdash; REVIEW CHANGES
          </h3>

          <div className="space-y-3 mb-6">
            {diffs.map((d) => (
              <div
                key={d.field}
                className="border border-white/5 bg-black/40 p-3"
              >
                <p className="text-slate-500 font-mono text-[10px] tracking-wider mb-2">
                  {d.field.toUpperCase()}
                </p>
                <div className="flex flex-col sm:flex-row gap-2 font-mono text-xs">
                  <div className="flex-1 min-w-0">
                    <span className="text-red-400/70 text-[10px] block mb-1">OLD</span>
                    <span className="text-red-400 break-all">{d.oldValue}</span>
                  </div>
                  <div className="hidden sm:block text-slate-600 self-center">&rarr;</div>
                  <div className="flex-1 min-w-0">
                    <span className="text-[#BDF000]/70 text-[10px] block mb-1">NEW</span>
                    <span className="text-[#BDF000] break-all">{d.newValue}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>

          <div className="flex justify-end gap-3">
            <button
              onClick={onCancel}
              disabled={saving}
              className="px-4 py-2 border border-white/10 text-slate-400 font-mono text-xs hover:bg-white/5 transition-colors flex items-center gap-2 disabled:opacity-40"
              aria-label="Cancel save"
            >
              <X className="w-3 h-3" aria-hidden="true" />
              CANCEL
            </button>
            <button
              onClick={onConfirm}
              disabled={saving}
              className="px-4 py-2 bg-[#BDF000]/10 hover:bg-[#BDF000]/20 border border-[#BDF000]/30 text-[#BDF000] font-mono text-xs transition-colors flex items-center gap-2 disabled:opacity-40"
              aria-label="Confirm save"
            >
              <Check className="w-3 h-3" aria-hidden="true" />
              {saving ? "SAVING..." : "CONFIRM SAVE"}
            </button>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

// ---------------------------------------------------------------------------
// Daemon log viewer component
// ---------------------------------------------------------------------------

function DaemonLogViewer({ taskId }: { taskId: string | null }) {
  const logContainerRef = useRef<HTMLPreElement>(null);
  const [logLines, setLogLines] = useState<string[] | null>(null);
  const [logLoading, setLogLoading] = useState(true);
  const [logError, setLogError] = useState<string | null>(null);
  const { selectedProject } = useProject();

  const fetchLog = useCallback(async () => {
    if (!taskId) {
      setLogLines(null);
      setLogLoading(false);
      return;
    }
    try {
      const res = await fetch(
        `/api/tasks/${encodeURIComponent(taskId)}/execution-log?project=${encodeURIComponent(selectedProject)}`,
      );
      if (!res.ok) {
        setLogError("Unable to fetch daemon log.");
        return;
      }
      const text = await res.text();
      let lines: string[];
      try {
        const parsed = JSON.parse(text);
        lines = Array.isArray(parsed) ? parsed : text.split("\n");
      } catch {
        lines = text.split("\n");
      }
      const last50 = lines.slice(-50);
      setLogLines(last50);
      setLogError(null);
    } catch {
      setLogError("Network error loading daemon log.");
    } finally {
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
    return (
      <div className="py-4">
        <p className="text-slate-500 font-mono text-xs">
          No active task found. Daemon log will appear when a task is running.
        </p>
      </div>
    );
  }

  if (logLoading && !logLines) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-3 w-full bg-white/5" />
        <Skeleton className="h-3 w-4/5 bg-white/5" />
        <Skeleton className="h-3 w-3/5 bg-white/5" />
        <Skeleton className="h-3 w-full bg-white/5" />
      </div>
    );
  }

  if (logError && !logLines) {
    return (
      <div className="flex flex-col items-center gap-3 py-6 text-center">
        <p className="text-red-400 font-mono text-xs">
          Unable to load daemon log. Please try again.
        </p>
        <button
          onClick={fetchLog}
          className="px-4 py-2 border border-red-400/30 text-red-400 font-mono text-xs hover:bg-red-400/10 transition-colors flex items-center gap-2"
          aria-label="Retry loading daemon log"
        >
          <RefreshCw className="w-3 h-3" />
          RETRY
        </button>
      </div>
    );
  }

  if (!logLines || logLines.length === 0) {
    return (
      <div className="py-4">
        <p className="text-slate-500 font-mono text-xs">
          Daemon log is empty. Output will appear as the task progresses.
        </p>
      </div>
    );
  }

  return (
    <pre
      ref={logContainerRef}
      className="bg-black/80 border border-white/10 p-4 font-mono text-xs text-[#BDF000] max-h-64 overflow-y-auto whitespace-pre-wrap"
      aria-label="Daemon execution log"
    >
      {logLines.join("\n")}
    </pre>
  );
}

// ---------------------------------------------------------------------------
// Main Settings page
// ---------------------------------------------------------------------------

export default function Settings() {
  const { selectedProject, isGlobal } = useProject();

  // Fetch policy data
  const {
    data: policyData,
    loading: policyLoading,
    error: policyError,
    refetch: refetchPolicy,
  } = usePollingData<PolicyConfig>("/api/policy", 10000);

  const {
    data: registryData,
    loading: registryLoading,
    error: registryError,
    refetch: refetchRegistry,
  } = usePollingData<ProjectRegistry>("/api/registry", 15000);

  // ---- Fetch task list for daemon log viewer ----
  const {
    data: tasksData,
  } = usePollingData<TaskManifest[]>("/api/tasks", 10000);

  const mostRecentTaskId = useMemo(() => {
    if (!tasksData || tasksData.length === 0) return null;
    const sorted = [...tasksData].sort(
      (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
    );
    return sorted[0].task_id;
  }, [tasksData]);

  // ---- Task Policy local state ----
  const [policy, setPolicy] = useState<PolicyConfig | null>(null);
  const [policySaving, setPolicySaving] = useState(false);
  const [policyErrors, setPolicyErrors] = useState<ValidationErrors>({});
  const [showPolicyDiff, setShowPolicyDiff] = useState(false);

  useEffect(() => {
    if (policyData) setPolicy({ ...policyData });
  }, [policyData]);

  const updatePolicy = useCallback(
    <K extends keyof PolicyConfig>(key: K, value: PolicyConfig[K]) => {
      setPolicy((prev) => (prev ? { ...prev, [key]: value } : prev));
      // Run validation onChange
      const error = validatePolicyField(key, value);
      setPolicyErrors((prev) => ({ ...prev, [key]: error }));
    },
    [],
  );

  const handleSavePolicyClick = useCallback(() => {
    if (!policy || isGlobal) return;
    if (hasValidationErrors(policyErrors)) {
      toast.error("Fix validation errors before saving.");
      return;
    }
    setShowPolicyDiff(true);
  }, [policy, isGlobal, policyErrors]);

  const handleConfirmSavePolicy = useCallback(async () => {
    if (!policy || isGlobal) return;
    setPolicySaving(true);
    try {
      const res = await savePolicy(selectedProject, policy);
      if (res.ok) {
        toast.success("Policy saved");
        setShowPolicyDiff(false);
      } else {
        toast.error("Save failed: unexpected response");
      }
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Unknown error";
      toast.error(`Save failed: ${message}`);
    } finally {
      setPolicySaving(false);
    }
  }, [policy, selectedProject, isGlobal]);

  const policyDiffs = useMemo(
    () => computeDiff(policyData as Record<string, unknown> | null, policy as Record<string, unknown> | null),
    [policyData, policy],
  );

  // ---- Daemon Controls state ----
  const [daemonOutput, setDaemonOutput] = useState<string>("");
  const [daemonLoading, setDaemonLoading] = useState(false);
  const [taskDirInput, setTaskDirInput] = useState("");

  const handleDaemonAction = useCallback(
    async (action: string, taskDir?: string) => {
      if (isGlobal) return;
      setDaemonLoading(true);
      setDaemonOutput("");
      try {
        const res = await daemonAction(selectedProject, action, taskDir);
        const output = [res.stdout, res.stderr].filter(Boolean).join("\n");
        setDaemonOutput(output || (res.ok ? "Command completed successfully." : "No output returned."));
      } catch (err: unknown) {
        const message =
          err instanceof Error ? err.message : "Unknown error";
        setDaemonOutput(`Error: ${message}`);
        toast.error(`Daemon action failed: ${message}`);
      } finally {
        setDaemonLoading(false);
      }
    },
    [selectedProject, isGlobal],
  );

  return (
    <div className="p-8 max-w-4xl mx-auto h-full overflow-y-auto pb-24">
      <header className="mb-12">
        <h1 className="text-3xl font-mono font-light tracking-[0.2em] text-slate-300 flex items-center gap-4">
          <SettingsIcon className="w-8 h-8 text-[#BDF000]" aria-hidden="true" />
          SETTINGS
        </h1>
        <p className="text-slate-500 font-mono text-xs mt-2">
          // POLICY CONFIGURATION & DAEMON CONTROLS
        </p>
      </header>

      {isGlobal && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="mb-8 border border-[#BDF000]/20 bg-[#BDF000]/5 p-4 font-mono text-xs text-[#BDF000]"
          role="alert"
        >
          Select a specific project to edit settings.
        </motion.div>
      )}

      <div className="space-y-8">
        {/* ================================================================
            Section 1: TASK POLICY
           ================================================================ */}
        <Section title="TASK POLICY" color="#BDF000" delay={1} corner="left-0" side="l">
          {policyLoading ? (
            <SectionSkeleton rows={7} />
          ) : policyError && !policy ? (
            <SectionError message={policyError} onRetry={refetchPolicy} />
          ) : policy ? (
            <div className="space-y-4">
              <NumberField
                label="FRESHNESS_TASK_WINDOW"
                value={policy.freshness_task_window}
                onChange={(v) => updatePolicy("freshness_task_window", v)}
                min={1}
                ariaLabel="Freshness task window"
                error={policyErrors.freshness_task_window}
              />
              <NumberField
                label="ACTIVE_REBENCHMARK_TASK_WINDOW"
                value={policy.active_rebenchmark_task_window}
                onChange={(v) => updatePolicy("active_rebenchmark_task_window", v)}
                min={1}
                ariaLabel="Active rebenchmark task window"
                error={policyErrors.active_rebenchmark_task_window}
              />
              <NumberField
                label="SHADOW_REBENCHMARK_TASK_WINDOW"
                value={policy.shadow_rebenchmark_task_window}
                onChange={(v) => updatePolicy("shadow_rebenchmark_task_window", v)}
                min={1}
                ariaLabel="Shadow rebenchmark task window"
                error={policyErrors.shadow_rebenchmark_task_window}
              />
              <ToggleField
                label="MAINTAINER_AUTOSTART"
                checked={policy.maintainer_autostart}
                onChange={(v) => updatePolicy("maintainer_autostart", v)}
                ariaLabel="Maintainer autostart toggle"
              />
              <NumberField
                label="MAINTAINER_POLL_SECONDS"
                value={policy.maintainer_poll_seconds}
                onChange={(v) => updatePolicy("maintainer_poll_seconds", v)}
                min={10}
                ariaLabel="Maintainer poll seconds"
                error={policyErrors.maintainer_poll_seconds}
              />
              <ToggleField
                label="FAST_TRACK_SKIP_PLAN_AUDIT"
                checked={policy.fast_track_skip_plan_audit}
                onChange={(v) => updatePolicy("fast_track_skip_plan_audit", v)}
                ariaLabel="Fast track skip plan audit toggle"
              />
              <NumberField
                label="TOKEN_BUDGET_MULTIPLIER"
                value={policy.token_budget_multiplier}
                onChange={(v) => updatePolicy("token_budget_multiplier", v)}
                step={0.1}
                min={0.1}
                ariaLabel="Token budget multiplier"
                error={policyErrors.token_budget_multiplier}
              />
              <div className="flex justify-end pt-4">
                <button
                  onClick={handleSavePolicyClick}
                  disabled={isGlobal || policySaving || hasValidationErrors(policyErrors)}
                  className={SAVE_BUTTON_CLASS}
                  aria-label="Save task policy"
                >
                  <Save className="w-4 h-4" aria-hidden="true" />
                  {policySaving ? "SAVING..." : "SAVE TASK POLICY"}
                </button>
              </div>
            </div>
          ) : (
            <p className="text-slate-500 font-mono text-xs py-4">
              No policy data available. Ensure the daemon is running.
            </p>
          )}
        </Section>

        {/* ================================================================
            Section 3: REGISTERED PROJECTS
           ================================================================ */}
        <Section title="REGISTERED PROJECTS" color="#2DD4A8" delay={3} corner="left-0" side="l">
          {registryLoading ? (
            <SectionSkeleton rows={3} />
          ) : registryError && !registryData ? (
            <SectionError message={registryError} onRetry={refetchRegistry} />
          ) : registryData && registryData.projects.length > 0 ? (
            <div className="space-y-3">
              {registryData.projects.map((project) => (
                <div
                  key={project.path}
                  className="flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-4 py-3 border-b border-white/5 last:border-b-0"
                >
                  <span
                    className="text-slate-200 font-mono text-xs flex-1 min-w-0 truncate"
                    title={project.path}
                  >
                    {project.path}
                  </span>
                  <div className="flex items-center gap-4 text-slate-500 font-mono text-[10px]">
                    <span
                      className={`px-2 py-0.5 border ${
                        project.status === "active"
                          ? "border-[#2DD4A8]/30 text-[#2DD4A8]"
                          : "border-white/10 text-slate-500"
                      }`}
                    >
                      {project.status.toUpperCase()}
                    </span>
                    <span title="Last active at">
                      {project.last_active_at}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-slate-500 font-mono text-xs py-4">
              No projects registered. Use the CLI to register a project.
            </p>
          )}
        </Section>

        {/* ================================================================
            Section 4: DAEMON CONTROLS (hidden in global mode)
           ================================================================ */}
        {!isGlobal && (
          <Section title="DAEMON CONTROLS" color="#BDF000" delay={4} corner="right-0" side="r">
            <div className="space-y-4">
              <div className="flex flex-wrap gap-3">
                <button
                  onClick={() => handleDaemonAction("status")}
                  disabled={daemonLoading}
                  className={SAVE_BUTTON_CLASS}
                  aria-label="Check daemon status"
                >
                  <RefreshCw
                    className={`w-4 h-4 ${daemonLoading ? "animate-spin" : ""}`}
                    aria-hidden="true"
                  />
                  CHECK STATUS
                </button>
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    value={taskDirInput}
                    onChange={(e) => setTaskDirInput(e.target.value)}
                    placeholder="task dir (optional)"
                    className={`${INPUT_CLASS} max-w-[200px]`}
                    aria-label="Task directory for validation"
                  />
                  <button
                    onClick={() =>
                      handleDaemonAction(
                        "validate",
                        taskDirInput || undefined,
                      )
                    }
                    disabled={daemonLoading}
                    className={SAVE_BUTTON_CLASS}
                    aria-label="Validate current task"
                  >
                    <Terminal className="w-4 h-4" aria-hidden="true" />
                    VALIDATE CURRENT TASK
                  </button>
                </div>
              </div>

              {(daemonOutput || daemonLoading) && (
                <div className="mt-4">
                  {daemonLoading ? (
                    <div className="bg-black/60 border border-white/10 p-4 flex items-center gap-2">
                      <RefreshCw className="w-3 h-3 animate-spin text-[#BDF000]" aria-hidden="true" />
                      <span className="text-slate-500 font-mono text-xs">
                        Running command...
                      </span>
                    </div>
                  ) : (
                    <pre
                      className="bg-black/60 border border-white/10 p-4 text-slate-300 font-mono text-xs overflow-x-auto whitespace-pre-wrap max-h-64 overflow-y-auto"
                      aria-label="Daemon command output"
                    >
                      {daemonOutput}
                    </pre>
                  )}
                </div>
              )}
            </div>
          </Section>
        )}

        {/* ================================================================
            Section 5: DAEMON LOG (project mode only)
           ================================================================ */}
        {!isGlobal && (
          <Section title="DAEMON LOG" color="#BDF000" delay={5} corner="left-0" side="l">
            <div className="flex items-center gap-2 mb-4">
              <ScrollText className="w-4 h-4 text-[#BDF000]" aria-hidden="true" />
              <span className="text-slate-500 font-mono text-[10px] tracking-wider">
                {mostRecentTaskId
                  ? `TASK: ${mostRecentTaskId}`
                  : "WAITING FOR TASK"}
              </span>
            </div>
            <DaemonLogViewer taskId={mostRecentTaskId} />
          </Section>
        )}
      </div>

      {/* Diff preview modals */}
      {showPolicyDiff && (
        <DiffPreviewPanel
          title="TASK POLICY"
          diffs={policyDiffs}
          onConfirm={handleConfirmSavePolicy}
          onCancel={() => setShowPolicyDiff(false)}
          saving={policySaving}
        />
      )}
    </div>
  );
}
