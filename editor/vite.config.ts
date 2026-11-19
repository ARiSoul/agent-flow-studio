import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` serves the editor on :5173 and forwards /api to the Python server (:8765).
// `npm run build` writes the editor to src/agent_flow_studio/editor/static (served by agent-flow-studio-editor).
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "../src/agent_flow_studio/editor/static", emptyOutDir: true },
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8765" } },
});
