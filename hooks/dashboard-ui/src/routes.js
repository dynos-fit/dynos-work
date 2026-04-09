import { jsx as _jsx } from "react/jsx-runtime";
import { createBrowserRouter } from "react-router";
import { lazy, Suspense } from "react";
import Root from "./components/Root";
/**
 * Lazy-load wrapper with Suspense fallback.
 * Pages are created by seg-6 through seg-11; lazy imports resolve
 * once those files exist on disk.
 */
function lazyPage(loader) {
    const Component = lazy(loader);
    return (_jsx(Suspense, { fallback: _jsx("div", { className: "flex items-center justify-center h-full min-h-[200px]", children: _jsx("div", { className: "w-6 h-6 border-2 border-[#BDF000]/30 border-t-[#BDF000] rounded-full animate-spin" }) }), children: _jsx(Component, {}) }));
}
export const router = createBrowserRouter([
    {
        path: "/",
        element: _jsx(Root, {}),
        children: [
            { index: true, element: lazyPage(() => import("./pages/Dashboard")) },
            { path: "tasks", element: lazyPage(() => import("./pages/TaskPipeline")) },
            { path: "tasks/:taskId", element: lazyPage(() => import("./pages/TaskDetail")) },
            { path: "agents", element: lazyPage(() => import("./pages/Agents")) },
            { path: "learning-ops", element: lazyPage(() => import("./pages/LearningOps")) },
            { path: "analytics", element: lazyPage(() => import("./pages/Analytics")) },
            { path: "settings", element: lazyPage(() => import("./pages/Settings")) },
            { path: "terminal", element: lazyPage(() => import("./pages/Terminal")) },
            { path: "graph", element: lazyPage(() => import("./pages/DependencyGraph")) },
        ],
    },
]);
