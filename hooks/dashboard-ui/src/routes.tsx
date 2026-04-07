import { createBrowserRouter } from "react-router";
import { lazy, Suspense, type ComponentType } from "react";
import Root from "./components/Root";

/**
 * Lazy-load wrapper with Suspense fallback.
 * Pages are created by seg-6 through seg-11; lazy imports resolve
 * once those files exist on disk.
 */
function lazyPage(loader: () => Promise<{ default: ComponentType }>) {
  const Component = lazy(loader);
  return (
    <Suspense
      fallback={
        <div className="flex items-center justify-center h-full min-h-[200px]">
          <div className="w-6 h-6 border-2 border-[#BDF000]/30 border-t-[#BDF000] rounded-full animate-spin" />
        </div>
      }
    >
      <Component />
    </Suspense>
  );
}

export const router = createBrowserRouter([
  {
    path: "/",
    element: <Root />,
    children: [
      { index: true, element: lazyPage(() => import("./pages/Dashboard")) },
      { path: "tasks", element: lazyPage(() => import("./pages/TaskPipeline")) },
      { path: "tasks/:taskId", element: lazyPage(() => import("./pages/TaskDetail")) },
      { path: "agents", element: lazyPage(() => import("./pages/Agents")) },
      { path: "autofix", element: lazyPage(() => import("./pages/Autofix")) },
      { path: "learning-ops", element: lazyPage(() => import("./pages/LearningOps")) },
      { path: "analytics", element: lazyPage(() => import("./pages/Analytics")) },
      { path: "settings", element: lazyPage(() => import("./pages/Settings")) },
      { path: "terminal", element: lazyPage(() => import("./pages/Terminal")) },
      { path: "graph", element: lazyPage(() => import("./pages/DependencyGraph")) },
    ],
  },
]);
