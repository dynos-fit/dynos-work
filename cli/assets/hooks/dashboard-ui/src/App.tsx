import { RouterProvider } from "react-router";
import { Toaster } from "sonner";
import { ProjectProvider } from "./data/ProjectContext";
import { router } from "./routes";

export default function App() {
  return (
    <ProjectProvider>
      <RouterProvider router={router} />
      <Toaster
        theme="dark"
        toastOptions={{
          style: {
            background: "#0D1321",
            border: "1px solid rgba(0, 229, 255, 0.15)",
            color: "#E2E8F0",
            fontFamily: "'JetBrains Mono', monospace",
            fontSize: "0.75rem",
          },
        }}
      />
    </ProjectProvider>
  );
}
