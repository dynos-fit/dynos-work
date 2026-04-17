import path from "path";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig, type Plugin } from "vite";

// Conditional import: the custom plugin is created by seg-4.
// Until it exists, we use a no-op placeholder so the build works.
let dynosApi: () => Plugin;
try {
  const mod = await import("./src/vite-plugin/dynos-api");
  dynosApi = mod.default ?? mod.dynosApi;
} catch {
  dynosApi = () => ({ name: "dynos-api-placeholder" });
}

export default defineConfig({
  plugins: [react(), tailwindcss(), dynosApi()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
});
