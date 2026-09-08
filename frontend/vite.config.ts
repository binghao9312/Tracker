import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.VITE_API_PROXY ?? "http://127.0.0.1:8000";
const wsTarget = process.env.VITE_WS_PROXY ?? "ws://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": apiTarget, "/ws": { target: wsTarget, ws: true } } },
});
