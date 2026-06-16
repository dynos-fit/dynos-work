/**
 * ProjectContext provides project selection state for the dashboard.
 *
 * On mount, fetches GET /api/registry to populate the project list.
 * Reads localStorage key 'dynos-dashboard-project' for persisted selection.
 * Falls back to repo root match, then first project from registry.
 * Stores selection in localStorage on change.
 */

import {
  createContext,
  useContext,
  useState,
  useCallback,
  useEffect,
  type ReactNode,
} from "react";
import type { ProjectRegistryEntry } from "./types";

const STORAGE_KEY = "dynos-dashboard-project";

export interface ProjectContextValue {
  selectedProject: string;
  setSelectedProject: (project: string) => void;
  isGlobal: boolean;
  projects: ProjectRegistryEntry[];
}

export const ProjectContext = createContext<ProjectContextValue>({
  selectedProject: "",
  setSelectedProject: () => {},
  isGlobal: false,
  projects: [],
});

export function ProjectProvider({ children }: { children: ReactNode }) {
  const [projects, setProjects] = useState<ProjectRegistryEntry[]>([]);
  const [selectedProject, setSelectedProjectState] = useState<string>(() => {
    return localStorage.getItem(STORAGE_KEY) ?? "";
  });

  const setSelectedProject = useCallback((project: string) => {
    setSelectedProjectState(project);
    localStorage.setItem(STORAGE_KEY, project);
  }, []);

  useEffect(() => {
    fetch("/api/registry")
      .then((r) => {
        if (!r.ok) throw new Error("registry fetch failed");
        return r.json();
      })
      .then((data: { projects?: unknown }) => {
        const list = Array.isArray((data as { projects?: unknown }).projects)
          ? (data as { projects: ProjectRegistryEntry[] }).projects
          : [];
        setProjects(list);
        if (!selectedProject && list.length > 0) {
          setSelectedProject(list[0].path);
        }
      })
      .catch(() => {});
  }, []);

  const isGlobal = selectedProject === "__global__";

  return (
    <ProjectContext.Provider
      value={{ selectedProject, setSelectedProject, isGlobal, projects }}
    >
      {children}
    </ProjectContext.Provider>
  );
}

/**
 * Convenience hook for consuming ProjectContext.
 */
export function useProject(): ProjectContextValue {
  return useContext(ProjectContext);
}
