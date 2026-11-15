"""Markdown sample of evidence links (`onus evidence ... --report docs/evidence_dev.md`)."""

from __future__ import annotations

import random
from datetime import UTC, datetime

from onus.claims.models import ClaimSet
from onus.evidence.models import EvidenceSet
from onus.schemas.document import Document


def _cell(s: str, n: int | None = None) -> str:
    s = " ".join(s.split()).replace("|", "\\|")
    return s if n is None or len(s) <= n else s[: n - 1] + "…"


def render_evidence_report(results: list[tuple[Document, ClaimSet, EvidenceSet]], sample: int = 10,
                           seed: int = 7) -> str:
    fp = results[0][2].fingerprint if results else "-"
    out = [
        "# Evidence links on dev (Phase 4)", "",
        f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by `onus evidence`. Fingerprint `{fp}`.", "",
        ("Per paper, over non-checklist claims. *Linked* = the `link` check passes (≥ 1 supporting or partial item). "
        "*Dangling* = the claim (±1 sentence) cites a table, figure or section that is not in the paper. "
        "Methods count kept evidence items."), "",
        ("| Paper | Claims | Pool | Cited objects | Dangling | Linked | Not linked | Has contradicting | Select failed "
        "| explicit / llm_selected / retrieval | Invalid ids | € |"),
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: |",
    ]
    tot = {"claims": 0, "linked": 0, "cand_or_dangling": 0}
    for doc, _, es in results:
        st, lm = es.stats, es.stats["link_methods"]
        out.append(
            f"| {doc.paper_id} | {st['claims']} | {st['pool']} | {st['with_explicit']} | {st['dangling_claims']} "
            f"| {st['with_support']} | {st['no_support']} | {st['with_contradicts']} | {st['select_failed']} "
            f"| {lm['explicit_ref']} / {lm['llm_selected']} / {lm['retrieval']} | {st['invalid_ids']} "
            f"| {es.cost.get('eur', 0):.4f} |")
        tot["claims"] += st["claims"]
        tot["linked"] += st["with_support"]
        tot["cand_or_dangling"] += sum(1 for c in es.claims if c.candidates or c.dangling)
    out += ["", (f"Totals: {tot['claims']} claims; {tot['cand_or_dangling']} have ≥ 1 candidate or a dangling flag; "
                f"{tot['linked']} linked to supporting or partial evidence."), ""]

    pool = [(doc, c, es.for_claim(c.id)) for doc, cs, es in results for c in cs.claims if c.type != "checklist"]
    picks = random.Random(seed).sample(pool, min(sample, len(pool)))
    out += [f"## {len(picks)} sampled claims (seed {seed})", ""]
    for k, (doc, c, ce) in enumerate(picks, 1):
        out += [f"### {k}. {doc.paper_id}, {c.location.cite()} ({c.type}{', main' if c.is_main else ''})", "",
                f"> {_cell(c.quote)}", ""]
        if ce is None:
            out += ["(not linked)", ""]
            continue
        out += [f"**Link check:** {ce.link.outcome}. {ce.link.detail}"
                + (f" Cited: {', '.join(ce.explicit)}." if ce.explicit else "")
                + (f" Dangling: {', '.join(ce.dangling)}." if ce.dangling else ""), ""]
        if ce.evidence:
            out += ["| Id | Relation | Method | Evidence | Location | Snippet | Note |",
                    "| --- | --- | --- | --- | --- | --- | --- |"]
            for e in ce.evidence:
                loc = e.location.cite() if e.location else "—"
                out.append(f"| {e.id} | {e.relation or '—'} | {e.link_method} | {_cell(e.label)} | {loc} "
                           f"| {_cell(e.snippet, 220)} | {_cell(ce.notes.get(e.id, ''), 160)} |")
            out.append("")
    return "\n".join(out) + "\n"
