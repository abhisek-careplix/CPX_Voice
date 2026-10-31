import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const api = process.env.CAREPLIX_API_URL ?? "http://localhost:8080";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Same-origin proxy keeps the session cookie HttpOnly and SameSite=Strict.
    proxy: { "/api": { target: api, changeOrigin: false }, "/healthz": api },
  },
});
