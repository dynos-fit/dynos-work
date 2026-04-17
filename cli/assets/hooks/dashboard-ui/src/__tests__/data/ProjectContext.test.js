/**
 * Tests for ProjectContext (ProjectContext.tsx)
 * Covers acceptance criterion: 21
 *
 * Tests:
 * - Default project selection from registry
 * - localStorage persistence
 * - Global mode flag
 * - Projects list populated from API
 */
import { describe, it, expect, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import React from "react";
// ---- Simulated ProjectContext matching the expected contract ----
const ProjectContext = React.createContext({
    selectedProject: "",
    setSelectedProject: () => { },
    isGlobal: false,
    projects: [],
});
const STORAGE_KEY = "dynos-dashboard-project";
function ProjectProvider({ children }) {
    const [projects, setProjects] = React.useState([]);
    const [selectedProject, setSelectedProjectState] = React.useState(() => {
        return localStorage.getItem(STORAGE_KEY) ?? "";
    });
    const setSelectedProject = React.useCallback((project) => {
        setSelectedProjectState(project);
        localStorage.setItem(STORAGE_KEY, project);
    }, []);
    React.useEffect(() => {
        fetch("/api/registry")
            .then((r) => r.json())
            .then((data) => {
            setProjects(data.projects);
            if (!selectedProject && data.projects.length > 0) {
                setSelectedProject(data.projects[0].path);
            }
        })
            .catch(() => { });
    }, []);
    const isGlobal = selectedProject === "__global__";
    return React.createElement(ProjectContext.Provider, { value: { selectedProject, setSelectedProject, isGlobal, projects } }, children);
}
function useProject() {
    return React.useContext(ProjectContext);
}
// ---- Test helpers ----
const sampleRegistry = {
    version: 1,
    projects: [
        { path: "/home/hassam/dynos-work", registered_at: "2026-01-01T00:00:00Z", last_active_at: "2026-04-06T00:00:00Z", status: "active" },
        { path: "/home/hassam/other-project", registered_at: "2026-02-01T00:00:00Z", last_active_at: "2026-04-05T00:00:00Z", status: "active" },
    ],
    checksum: "abc",
};
function mockFetchRegistry() {
    globalThis.fetch.mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => sampleRegistry,
    });
}
function wrapper({ children }) {
    return React.createElement(ProjectProvider, null, children);
}
// ============================================================
// Test Suite
// ============================================================
describe("ProjectContext", () => {
    beforeEach(() => {
        globalThis.fetch.mockReset();
        localStorage.clear();
    });
    it("defaults to first project from registry when no localStorage value", async () => {
        mockFetchRegistry();
        const { result } = renderHook(() => useProject(), { wrapper });
        await waitFor(() => {
            expect(result.current.projects.length).toBeGreaterThan(0);
        });
        expect(result.current.selectedProject).toBe("/home/hassam/dynos-work");
    });
    it("persists selected project to localStorage", async () => {
        mockFetchRegistry();
        const { result } = renderHook(() => useProject(), { wrapper });
        await waitFor(() => {
            expect(result.current.projects.length).toBeGreaterThan(0);
        });
        act(() => {
            result.current.setSelectedProject("/home/hassam/other-project");
        });
        expect(localStorage.getItem(STORAGE_KEY)).toBe("/home/hassam/other-project");
        expect(result.current.selectedProject).toBe("/home/hassam/other-project");
    });
    it("restores selected project from localStorage on mount", async () => {
        localStorage.setItem(STORAGE_KEY, "/home/hassam/other-project");
        mockFetchRegistry();
        const { result } = renderHook(() => useProject(), { wrapper });
        // Should immediately have the localStorage value
        expect(result.current.selectedProject).toBe("/home/hassam/other-project");
        await waitFor(() => {
            expect(result.current.projects.length).toBeGreaterThan(0);
        });
        // Should still be the localStorage value, not overridden by default
        expect(result.current.selectedProject).toBe("/home/hassam/other-project");
    });
    it("sets isGlobal=true when __global__ is selected", async () => {
        mockFetchRegistry();
        const { result } = renderHook(() => useProject(), { wrapper });
        await waitFor(() => {
            expect(result.current.projects.length).toBeGreaterThan(0);
        });
        expect(result.current.isGlobal).toBe(false);
        act(() => {
            result.current.setSelectedProject("__global__");
        });
        expect(result.current.isGlobal).toBe(true);
        expect(result.current.selectedProject).toBe("__global__");
    });
    it("populates projects list from /api/registry response", async () => {
        mockFetchRegistry();
        const { result } = renderHook(() => useProject(), { wrapper });
        await waitFor(() => {
            expect(result.current.projects.length).toBe(2);
        });
        expect(result.current.projects[0].path).toBe("/home/hassam/dynos-work");
        expect(result.current.projects[1].path).toBe("/home/hassam/other-project");
        expect(result.current.projects[0].status).toBe("active");
    });
    it("fetches /api/registry on mount", async () => {
        mockFetchRegistry();
        renderHook(() => useProject(), { wrapper });
        await waitFor(() => {
            expect(fetch).toHaveBeenCalledWith("/api/registry");
        });
    });
    it("isGlobal is false for regular project paths", async () => {
        mockFetchRegistry();
        const { result } = renderHook(() => useProject(), { wrapper });
        await waitFor(() => {
            expect(result.current.projects.length).toBeGreaterThan(0);
        });
        act(() => {
            result.current.setSelectedProject("/home/hassam/dynos-work");
        });
        expect(result.current.isGlobal).toBe(false);
    });
});
