import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";

const apiProxyTarget = process.env.VITE_API_PROXY_TARGET || "http://localhost:8000";

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: "jsdom",
    coverage: {
      provider: "v8",
      all: true,
      // Production runtime source under src/ must stay fully covered. Type-only
      // modules (no runtime statements) and the app bootstrap entry are excluded.
      include: ["src/**/*.{ts,vue}"],
      exclude: [
        "src/main.ts",
        "src/types.ts",
        "src/**/*Types.ts",
        "src/**/*.d.ts",
        "src/**/__tests__/**",
        "src/**/*.spec.ts",
      ],
      lines: 100,
      branches: 100,
      functions: 100,
      statements: 100,
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
});
