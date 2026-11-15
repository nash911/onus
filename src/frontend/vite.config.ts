import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

/**
 * Dev-only fixtures (`npm run dev:fixtures`, i.e. VITE_DEV_FIXTURES=1): serves the dev-split findings and
 * metrics that Session A's harness wrote under var/eval/, so the Findings and Trust charts can be developed
 * before the held-out runs. `apply: "serve"` keeps this out of every build, and the app only requests
 * /__dev/* when `import.meta.env.DEV` is true, which is statically false in a build.
 */
function devFixtures(): Plugin {
  const repo = resolve(fileURLToPath(new URL(".", import.meta.url)), "../..");
  const read = (p: string) => JSON.parse(readFileSync(resolve(repo, p), "utf-8"));
  return {
    name: "onus-dev-fixtures",
    apply: "serve",
    configureServer(server) {
      if (process.env.VITE_DEV_FIXTURES !== "1") return;
      server.middlewares.use("/__dev/findings.json", (_req, res) => {
        res.setHeader("content-type", "application/json");
        res.end(JSON.stringify({ ...read("var/eval/dev_findings.json"), _fixture: "dev split" }));
      });
      server.middlewares.use("/__dev/metrics.json", (_req, res) => {
        // Same shape as `onus eval merge-metrics`: {splits, pooled, labels}.
        const dev = read("var/eval/dev/metrics.json");
        let stability = dev.stability;
        try {
          stability = read("var/eval/dev_3runs/metrics.json").stability;
        } catch {
          /* keep the 1-run note */
        }
        const merged = {
          splits: { dev: { ...dev, stability } },
          pooled: { recall: dev.mutations?.recall ?? {}, collateral: dev.mutations?.collateral?.onus ?? null },
          labels: null,
          _fixture: "dev split",
        };
        res.setHeader("content-type", "application/json");
        res.end(JSON.stringify(merged));
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), devFixtures()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 700 },
});
