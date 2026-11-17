import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

/**
 * Dev-only fixtures (`npm run dev:fixtures`, i.e. VITE_DEV_FIXTURES=1): a report for dev paper EJ8cQFi5cU, with
 * its PDF from data/dev and anchors from tests/fixtures/anchors_sample.json (the /anchors contract), so the PDF
 * report view can be built before the real endpoints exist. Open /analyses/dev-fixture (or
 * /analyses/dev-fixture-nopdf for the missing-PDF view). `apply: "serve"` keeps this plugin out of every build.
 */
const FIXTURE_PAPER = "EJ8cQFi5cU";

function devFixtures(): Plugin {
  const repo = resolve(fileURLToPath(new URL(".", import.meta.url)), "../..");
  const read = (p: string) => JSON.parse(readFileSync(resolve(repo, p), "utf-8"));
  const json = (res: import("node:http").ServerResponse, body: unknown, status = 200) => {
    res.statusCode = status;
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify(body));
  };
  return {
    name: "onus-dev-fixtures",
    apply: "serve",
    configureServer(server) {
      if (process.env.VITE_DEV_FIXTURES !== "1") return;
      server.middlewares.use((req, res, next) => {
        const m = /^\/api\/analyses\/(dev-fixture(?:-nopdf)?)(\/[a-z]+)?(?:\?.*)?$/.exec(req.url ?? "");
        if (!m || req.method !== "GET") return next();
        const [, id, sub] = m;
        const noPdf = id === "dev-fixture-nopdf";
        if (sub === "/status") {
          return json(res, { id, paper_id: FIXTURE_PAPER, status: "done", mode: "batch", progress: null, error: null,
                             created_at: null, finished_at: null });
        }
        if (sub === "/pdf") {
          if (noPdf) return json(res, { detail: "no_pdf" }, 404);
          res.setHeader("content-type", "application/pdf");
          return res.end(readFileSync(resolve(repo, `data/dev/papers/${FIXTURE_PAPER}/paper.pdf`)));
        }
        if (sub === "/anchors") {
          const a = read("tests/fixtures/anchors_sample.json");
          return json(res, noPdf ? { pdf: false, page_sizes: [], items: {}, missing: [] } : a);
        }
        if (sub) return next();
        const r = read("tests/fixtures/report_sample.json");
        return json(res, {
          ...r,
          display: null,
          code_stage: null,
          analysis: { id, paper_id: FIXTURE_PAPER, paper_ref: FIXTURE_PAPER, status: "done", source_url: null,
                      title: r.paper?.title ?? null },
        });
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
