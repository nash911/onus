import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { PaperRow } from "../api/types";
import { OlderVersionBadge, useMeta } from "../components/Layout";
import { EmptyState, ErrorBox, Loading, PageTitle } from "../components/States";
import { VerdictCounts, VerdictLegend, VerdictMixBar } from "../components/Verdict";
import { humanize, pct } from "../lib/format";
import { VERDICTS, VERDICT_ORDER } from "../lib/verdicts";

const PAGE = 50;
type SortKey = "title" | "flagged_share" | "main_flagged_share" | "n_claims";

function SortHeader({
  k,
  label,
  sort,
  order,
  onSort,
  className = "",
}: {
  k: SortKey;
  label: string;
  sort: SortKey;
  order: "asc" | "desc";
  onSort: (k: SortKey) => void;
  className?: string;
}) {
  const active = sort === k;
  return (
    <th scope="col" aria-sort={active ? (order === "asc" ? "ascending" : "descending") : "none"} className={`py-2 pr-3 font-medium ${className}`}>
      <button type="button" onClick={() => onSort(k)} className="inline-flex items-center gap-1 hover:text-slate-900">
        {label}
        <span aria-hidden="true" className="text-xs">
          {active ? (order === "asc" ? "▲" : "▼") : "↕"}
        </span>
      </button>
    </th>
  );
}

function Row({ p }: { p: PaperRow }) {
  const s = p.summary;
  const d = p.display;
  return (
    <tr className="align-top">
      <td className="py-3 pr-3">
        <Link to={`/papers/${encodeURIComponent(p.ref)}`} className="text-blue-900 hover:underline break-anywhere">
          {p.title ?? p.ref}
        </Link>
        {p.fingerprint && (
          <div className="mt-1">
            <OlderVersionBadge fingerprint={p.fingerprint} version={p.onus_version} />
          </div>
        )}
        <div className="mt-1 flex flex-wrap gap-x-3 text-xs text-slate-500 md:hidden">
          {d?.decision && <span className="capitalize">{d.decision}</span>}
          {d?.tier && <span>code: {d.tier}</span>}
          <span>{s?.n_claims ?? "—"} claims</span>
        </div>
        <div className="mt-2 md:hidden">
          <VerdictCounts counts={s?.counts} />
        </div>
      </td>
      <td className="hidden py-3 pr-3 capitalize md:table-cell">{d?.decision ?? "—"}</td>
      <td className="hidden py-3 pr-3 md:table-cell">{d?.tier ?? "—"}</td>
      <td className="hidden py-3 pr-3 text-right tabular-nums md:table-cell">{s?.n_claims ?? "—"}</td>
      <td className="py-3 pr-3 text-right tabular-nums">{pct(s?.flagged_share)}</td>
      <td className="hidden py-3 pr-3 text-right tabular-nums lg:table-cell">{pct(s?.main_flagged_share)}</td>
      <td className="hidden w-48 py-3 md:table-cell">
        <VerdictCounts counts={s?.counts} />
        <div className="mt-1.5">
          <VerdictMixBar counts={s?.counts} />
        </div>
      </td>
    </tr>
  );
}

export default function Corpus() {
  const meta = useMeta();
  const [params, setParams] = useSearchParams();
  const sort = (params.get("sort") as SortKey) || "flagged_share";
  const order = (params.get("order") as "asc" | "desc") || (sort === "title" ? "asc" : "desc");
  const offset = Number(params.get("offset") ?? 0) || 0;

  const apiParams = new URLSearchParams();
  for (const k of ["decision", "tier", "has_code", "verdict", "q"]) {
    const v = params.get(k);
    if (v) apiParams.set(k, v);
  }
  apiParams.set("sort", sort);
  apiParams.set("order", order);
  apiParams.set("offset", String(offset));
  apiParams.set("limit", String(PAGE));

  const q = useQuery({
    queryKey: ["papers", apiParams.toString()],
    queryFn: () => api.papers(apiParams),
    placeholderData: keepPreviousData,
  });

  const set = (k: string, v: string | null) =>
    setParams((p) => {
      const n = new URLSearchParams(p);
      if (v) n.set(k, v);
      else n.delete(k);
      if (k !== "offset") n.delete("offset");
      return n;
    });
  const onSort = (k: SortKey) => {
    setParams((p) => {
      const n = new URLSearchParams(p);
      const nextOrder = sort === k ? (order === "asc" ? "desc" : "asc") : k === "title" ? "asc" : "desc";
      n.set("sort", k);
      n.set("order", nextOrder);
      n.delete("offset");
      return n;
    });
  };

  const facets = q.data?.facets;
  const filtered = ["decision", "tier", "has_code", "verdict", "q"].some((k) => params.get(k));
  const select = "rounded border border-slate-300 px-2 py-1 text-sm font-normal";

  return (
    <div>
      <PageTitle sub="Every analysed submission. Flagged share is the share of a paper's claims that are contradicted, overstated or untraced. It counts evidence gaps, not errors or misconduct.">
        Corpus
      </PageTitle>

      <form className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end" role="search" onSubmit={(e) => e.preventDefault()}>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Title
          <input type="search" defaultValue={params.get("q") ?? ""} onChange={(e) => set("q", e.target.value)} className={select} />
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Decision
          <select value={params.get("decision") ?? ""} onChange={(e) => set("decision", e.target.value)} className={select}>
            <option value="">All</option>
            {facets?.decision.map((d) => (
              <option key={d} value={d}>
                {humanize(d)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Code tier
          <select value={params.get("tier") ?? ""} onChange={(e) => set("tier", e.target.value)} className={select}>
            <option value="">All</option>
            {facets?.tier.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Code submitted
          <select value={params.get("has_code") ?? ""} onChange={(e) => set("has_code", e.target.value)} className={select}>
            <option value="">Either</option>
            <option value="true">With code</option>
            <option value="false">Without code</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Has a claim that is
          <select value={params.get("verdict") ?? ""} onChange={(e) => set("verdict", e.target.value)} className={select}>
            <option value="">Any verdict</option>
            {VERDICT_ORDER.map((v) => (
              <option key={v} value={v}>
                {VERDICTS[v].label}
              </option>
            ))}
          </select>
        </label>
      </form>

      {meta.data && !meta.data.has_findings && (
        <p className="mt-4 max-w-prose text-sm text-slate-600">
          {meta.data.corpus_papers > 0
            ? `The corpus lists ${meta.data.corpus_papers} analysed paper${meta.data.corpus_papers === 1 ? "" : "s"} for now. The full set is added with the final results.`
            : "No papers have been analysed yet. The corpus is added with the final results."}
        </p>
      )}

      <div className="mt-4">
        <VerdictLegend />
      </div>

      {q.error && <ErrorBox error={q.error} />}
      {q.isPending ? (
        <Loading />
      ) : q.data && q.data.total === 0 ? (
        <EmptyState title={filtered ? "No papers match these filters." : "No papers have been analysed yet."}>
          {filtered ? "Clear a filter to see more papers." : "Analysed papers appear here once their reports are loaded."}
        </EmptyState>
      ) : q.data ? (
        <>
          <p className="mt-4 text-sm text-slate-600" role="status">
            {q.data.total} paper{q.data.total === 1 ? "" : "s"}
          </p>
          <table className="mt-2 w-full table-fixed text-left text-sm md:table-auto">
            <thead className="border-b border-slate-300 text-slate-600">
              <tr>
                <SortHeader k="title" label="Paper" sort={sort} order={order} onSort={onSort} />
                <th scope="col" className="hidden py-2 pr-3 font-medium md:table-cell">Decision</th>
                <th scope="col" className="hidden py-2 pr-3 font-medium md:table-cell">Code tier</th>
                <SortHeader k="n_claims" label="Claims" sort={sort} order={order} onSort={onSort} className="hidden text-right md:table-cell" />
                <SortHeader k="flagged_share" label="Flagged" sort={sort} order={order} onSort={onSort} className="w-24 text-right md:w-auto" />
                <SortHeader k="main_flagged_share" label="Main flagged" sort={sort} order={order} onSort={onSort} className="hidden text-right lg:table-cell" />
                <th scope="col" className="hidden py-2 font-medium md:table-cell">Verdict mix</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {q.data.items.map((p) => (
                <Row key={p.id} p={p} />
              ))}
            </tbody>
          </table>
          {q.data.total > PAGE && (
            <nav className="mt-4 flex items-center gap-4 text-sm" aria-label="Pages">
              <button
                type="button"
                disabled={offset === 0}
                onClick={() => set("offset", String(Math.max(0, offset - PAGE)))}
                className="rounded border border-slate-300 px-3 py-1 disabled:opacity-40"
              >
                Previous
              </button>
              <span>
                {offset + 1}–{Math.min(offset + PAGE, q.data.total)} of {q.data.total}
              </span>
              <button
                type="button"
                disabled={offset + PAGE >= q.data.total}
                onClick={() => set("offset", String(offset + PAGE))}
                className="rounded border border-slate-300 px-3 py-1 disabled:opacity-40"
              >
                Next
              </button>
            </nav>
          )}
        </>
      ) : null}
    </div>
  );
}
