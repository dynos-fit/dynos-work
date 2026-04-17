import { jsx as _jsx } from "react/jsx-runtime";
/**
 * ProjectContext provides project selection state for the dashboard.
 *
 * On mount, fetches GET /api/registry to populate the project list.
 * Reads localStorage key 'dynos-dashboard-project' for persisted selection.
 * Falls back to repo root match, then first project from registry.
 * Stores selection in localStorage on change.
 */
import { createContext, useContext, useState, useCallback, useEffect, } from "react";
const STORAGE_KEY = "dynos-dashboard-project";
export const ProjectContext = createContext({
    selectedProject: "",
    setSelectedProject: () => { },
    isGlobal: false,
    projects: [],
});
export function ProjectProvider({ children }) {
    const [projects, setProjects] = useState([]);
    const [selectedProject, setSelectedProjectState] = useState(() => {
        return localStorage.getItem(STORAGE_KEY) ?? "";
    });
    const setSelectedProject = useCallback((project) => {
        setSelectedProjectState(project);
        localStorage.setItem(STORAGE_KEY, project);
    }, []);
    useEffect(() => {
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
    return (_jsx(ProjectContext.Provider, { value: { selectedProject, setSelectedProject, isGlobal, projects }, children: children }));
}
/**
 * Convenience hook for consuming ProjectContext.
 */
export function useProject() {
    return useContext(ProjectContext);
}
