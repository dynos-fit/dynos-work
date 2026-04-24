import { createBrowserRouter } from "react-router";
import { lazy, Suspense, type ComponentType } from "react";
import Root from "./components/Root";

function lazyPage(loader: () => Promise<{ default: ComponentType }>) {
  const Component = lazy(loader);
  return (
    <Suspense
      fallback={
        <div className="flex items-center justify-center h-full min-h-[200px]">
          <div className="w-6 h-6 border-2 border-[#6ee7b7]/30 border-t-[#6ee7b7] rounded-full animate-spin" />
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
      { index: true, element: lazyPage(() => import("./pages/Home")) },
      { path: "repo/:slug", element: lazyPage(() => import("./pages/RepoPage")) },
      { path: "repo/:slug/task/:taskId", element: lazyPage(() => import("./pages/TaskDetail")) },
    ],
  },
]);
