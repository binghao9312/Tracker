var _a, _b;
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
var apiTarget = (_a = process.env.VITE_API_PROXY) !== null && _a !== void 0 ? _a : "http://127.0.0.1:8000";
var wsTarget = (_b = process.env.VITE_WS_PROXY) !== null && _b !== void 0 ? _b : "ws://127.0.0.1:8000";
export default defineConfig({
    plugins: [react()],
    server: { proxy: { "/api": apiTarget, "/ws": { target: wsTarget, ws: true } } },
});
