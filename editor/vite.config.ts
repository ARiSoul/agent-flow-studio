import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` serve o editor em :5173 e encaminha /api para o servidor Python (:8765).
// `npm run build` gera o editor em src/investigator/editor/static (servido pelo agent-flow-studio-editor).
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "../src/agent_flow_studio/editor/static", emptyOutDir: true },
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8765" } },
});
