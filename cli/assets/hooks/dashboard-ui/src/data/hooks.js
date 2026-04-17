/**
 * Data-fetching hooks for dynos-work dashboard.
 * usePollingData<T> wraps fetch with a polling interval and integrates
 * with ProjectContext for automatic project-scoped queries.
 */
import { useState, useCallback, useEffect, useContext } from "react";
import { ProjectContext } from "./ProjectContext";
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
export function usePollingData(url, intervalMs = 5000) {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const context = useContext(ProjectContext);
    const fullUrl = `${url}${url.includes("?") ? "&" : "?"}project=${encodeURIComponent(context.selectedProject)}`;
    const fetchData = useCallback(async () => {
        try {
            const res = await fetch(fullUrl);
            if (!res.ok) {
                const body = await res.json().catch(() => ({ error: "Request failed" }));
                setError(body.error ?? "Request failed");
                return;
            }
            const json = (await res.json());
            setData(json);
            setError(null);
        }
        catch {
            setError("Network error");
        }
        finally {
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
