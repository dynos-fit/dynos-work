/**
 * Data-fetching hooks for dynos-work dashboard.
 * usePollingData<T> wraps fetch with a polling interval and integrates
 * with ProjectContext for automatic project-scoped queries.
 */

import { useState, useCallback, useEffect, useRef, useContext } from "react";
import { ProjectContext } from "./ProjectContext";
import type {
  ProjectSummary,
  AuditSummaryData,
  RepairLogData,
  HandoffData,
  AuditPlanData,
  OptionalFileResponse,
} from "./types";

export interface UsePollingDataResult<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

/**
 * Generic polling data hook.
 *
 * Behavior:
 * - loading=true only on initial fetch (when data is still null)
 * - Stale-while-revalidate: error after success preserves data
 * - Re-fetches immediately when project context changes (URL changes)
 * - Appends ?project=<selectedProject> from ProjectContext
 * - Cleans up interval on unmount
 *
 * @param url - API endpoint path (e.g. "/api/tasks")
 * @param intervalMs - Polling interval in milliseconds (default 5000)
 */
export function usePollingData<T>(
  url: string,
  intervalMs = 5000,
): UsePollingDataResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const context = useContext(ProjectContext);

  const fullUrl = `${url}${url.includes("?") ? "&" : "?"}project=${encodeURIComponent(context.selectedProject)}`;

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(fullUrl);
      if (!res.ok) {
        const body = await res.json().catch(() => ({ error: "Request failed" }));
        setError((body as { error?: string }).error ?? "Request failed");
        return;
      }
      const json = (await res.json()) as T;
      setData(json);
      setError(null);
    } catch {
      setError("Network error");
    } finally {
      setLoading(false);
    }
  }, [fullUrl]);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, intervalMs);
    return () => clearInterval(interval);
  }, [fetchData, intervalMs]);

  return { data, loading, error, refetch: fetchData };
}

// ---------------------------------------------------------------------------
// Terminal-stage constant — shared by useAutoRefresh and consumers
// ---------------------------------------------------------------------------

export const TERMINAL_STAGES = ["DONE", "FAILED", "CALIBRATED"] as const;

// ---------------------------------------------------------------------------
// useAutoRefresh
//
// Like usePollingData but does NOT append a ?project= query param and
// is aware of terminal stages so it stops polling automatically.
//
// AC 22 behaviour:
//   (a) If stage is already terminal on first render, setInterval is never
//       called at all.
//   (b) When the stage parameter changes to a terminal value on a re-render,
//       the existing interval is cleared immediately.
//   (c) The useEffect cleanup always calls clearInterval to handle unmount.
// ---------------------------------------------------------------------------

export function useAutoRefresh<T>(
  url: string,
  stage: string | null | undefined,
  intervalMs = 5000,
): UsePollingDataResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Keep a stable ref to the interval id so we can clear it from inside the
  // fetch callback without needing it in any dependency array.
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const isTerminal = (s: string | null | undefined): boolean =>
    s != null && (TERMINAL_STAGES as readonly string[]).includes(s);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(url);
      if (!res.ok) {
        const body = await res.json().catch(() => ({ error: "Request failed" }));
        setError((body as { error?: string }).error ?? "Request failed");
        return;
      }
      const json = (await res.json()) as T;
      setData(json);
      setError(null);
    } catch {
      setError("Network error");
    } finally {
      setLoading(false);
    }
  }, [url]);

  useEffect(() => {
    // Initial fetch regardless of stage.
    fetchData();

    // (a) Never start the interval when stage is already terminal.
    if (isTerminal(stage)) {
      return;
    }

    intervalRef.current = setInterval(fetchData, intervalMs);

    // (c) Always clear on unmount.
    return () => {
      if (intervalRef.current !== null) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetchData, intervalMs]);

  // (b) When stage transitions to terminal during the component's lifetime,
  //     clear the running interval immediately.
  useEffect(() => {
    if (isTerminal(stage) && intervalRef.current !== null) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage]);

  return { data, loading, error, refetch: fetchData };
}

// ---------------------------------------------------------------------------
// useProjectsSummary — GET /api/projects-summary (no ?project= param)
// ---------------------------------------------------------------------------

export function useProjectsSummary(
  intervalMs = 5000,
): UsePollingDataResult<ProjectSummary[]> {
  const [data, setData] = useState<ProjectSummary[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch("/api/projects-summary");
      if (!res.ok) {
        const body = await res.json().catch(() => ({ error: "Request failed" }));
        setError((body as { error?: string }).error ?? "Request failed");
        return;
      }
      const json = (await res.json()) as ProjectSummary[];
      setData(json);
      setError(null);
    } catch {
      setError("Network error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, intervalMs);
    return () => clearInterval(interval);
  }, [fetchData, intervalMs]);

  return { data, loading, error, refetch: fetchData };
}

// ---------------------------------------------------------------------------
// Shared helper for task-scoped endpoints that take an explicit projectPath.
// Does NOT use ProjectContext — the caller supplies the project path directly.
// ---------------------------------------------------------------------------

function useTaskScopedData<T>(
  taskId: string,
  projectPath: string,
  endpoint: string,
  intervalMs: number,
): UsePollingDataResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const url = `/api/tasks/${encodeURIComponent(taskId)}/${endpoint}?project=${encodeURIComponent(projectPath)}`;

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(url);
      if (!res.ok) {
        const body = await res.json().catch(() => ({ error: "Request failed" }));
        setError((body as { error?: string }).error ?? "Request failed");
        return;
      }
      const json = (await res.json()) as T;
      setData(json);
      setError(null);
    } catch {
      setError("Network error");
    } finally {
      setLoading(false);
    }
  }, [url]);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, intervalMs);
    return () => clearInterval(interval);
  }, [fetchData, intervalMs]);

  return { data, loading, error, refetch: fetchData };
}

// ---------------------------------------------------------------------------
// useAuditSummary — GET /api/tasks/{taskId}/audit-summary?project={projectPath}
// ---------------------------------------------------------------------------

export function useAuditSummary(
  taskId: string,
  projectPath: string,
  intervalMs = 5000,
): UsePollingDataResult<OptionalFileResponse<AuditSummaryData>> {
  return useTaskScopedData<OptionalFileResponse<AuditSummaryData>>(
    taskId,
    projectPath,
    "audit-summary",
    intervalMs,
  );
}

// ---------------------------------------------------------------------------
// useRepairLog — GET /api/tasks/{taskId}/repair-log?project={projectPath}
// ---------------------------------------------------------------------------

export function useRepairLog(
  taskId: string,
  projectPath: string,
  intervalMs = 5000,
): UsePollingDataResult<OptionalFileResponse<RepairLogData>> {
  return useTaskScopedData<OptionalFileResponse<RepairLogData>>(
    taskId,
    projectPath,
    "repair-log",
    intervalMs,
  );
}

// ---------------------------------------------------------------------------
// useHandoff — GET /api/tasks/{taskId}/handoff?project={projectPath}
// ---------------------------------------------------------------------------

export function useHandoff(
  taskId: string,
  projectPath: string,
  intervalMs = 5000,
): UsePollingDataResult<OptionalFileResponse<HandoffData>> {
  return useTaskScopedData<OptionalFileResponse<HandoffData>>(
    taskId,
    projectPath,
    "handoff",
    intervalMs,
  );
}

// ---------------------------------------------------------------------------
// useAuditPlan — GET /api/tasks/{taskId}/audit-plan?project={projectPath}
// ---------------------------------------------------------------------------

export function useAuditPlan(
  taskId: string,
  projectPath: string,
  intervalMs = 5000,
): UsePollingDataResult<OptionalFileResponse<AuditPlanData>> {
  return useTaskScopedData<OptionalFileResponse<AuditPlanData>>(
    taskId,
    projectPath,
    "audit-plan",
    intervalMs,
  );
}
