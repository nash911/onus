"""Markdown report of the checks on dev (`onus checks ... --report docs/checks_dev.md`)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from onus.checks.models import CLAIM_CHECKS, CheckSet
from onus.claims.models import ClaimSet
from onus.evidence.models import EvidenceSet
from onus.schemas.document import Document

OUTCOMES = ("pass", "fail", "inconclusive", "not_run")
STATUSES = ("backed", "unbacked", "contradicted", "not_checkable", "not_applicable")


def _cell(s: str, n: int | None = None) -> str:
    s = " ".join(s.split()).replace("|", "\\|")
    return s if n is None or len(s) <= n else s[: n - 1] + "…"


def render_checks_report(results: list[tuple[Document, ClaimSet, EvidenceSet, CheckSet]]) -> str:
    fp = results[0][3].fingerprint if results else "-"
    out = [
        "# Checks on dev (Phase 5)", "",
        f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by `onus checks`. Fingerprint `{fp}`.", "",
        "Per paper, over non-checklist claims: pass / fail / inconclusive / not run.", "",
        "| Paper | Claims | number | dominance | strength | scope | Limitations | Checklist (B/U/C/NC/NA) | € |",
        "| --- | ---: | --- | --- | --- | --- | ---: | --- | ---: |",
    ]
    tot: dict[str, Counter] = {c: Counter() for c in CLAIM_CHECKS}
    sev: dict[str, Counter] = {c: Counter() for c in CLAIM_CHECKS}
    cl_tot: Counter = Counter()
    cl_key: dict[str, Counter] = {}
    reasons: dict[str, Counter] = {c: Counter() for c in CLAIM_CHECKS}
    eur = 0.0
    for doc, _, _, ks in results:
        row = [doc.paper_id, str(len(ks.claims))]
        for name in CLAIM_CHECKS:
            oc = Counter(c.get(name).outcome for c in ks.claims)  # type: ignore[union-attr]
            tot[name] += oc
            for c in ks.claims:
                r = c.get(name)
                if r is not None and r.outcome == "fail":
                    sev[name][r.severity] += 1
                if r is not None and r.outcome == "not_run":
                    reasons[name][r.data.get("reason", r.detail)] += 1
            row.append(" / ".join(str(oc.get(o, 0)) for o in OUTCOMES))
        st = Counter(a.status for a in ks.checklist)
        cl_tot += st
        for a in ks.checklist:
            cl_key.setdefault(a.key, Counter())[a.status] += 1
        row += [str(len(ks.limitations)), " / ".join(str(st.get(s, 0)) for s in STATUSES), f"{ks.cost.get('eur', 0):.4f}"]
        eur += ks.cost.get("eur", 0)
        out.append("| " + " | ".join(row) + " |")
    out += ["", "## Totals", "", "| Check | pass | fail | inconclusive | not run | fail: contradicts / overstates |",
            "| --- | ---: | ---: | ---: | ---: | --- |"]
    for name in CLAIM_CHECKS:
        t = tot[name]
        out.append(f"| {name} | {t['pass']} | {t['fail']} | {t['inconclusive']} | {t['not_run']} "
                   f"| {sev[name]['contradicts']} / {sev[name]['overstates']} |")
    out += ["", f"Chat spend for the checks: €{eur:.4f}.", "", "Not-run reasons:", ""]
    for name in CLAIM_CHECKS:
        rs = ", ".join(f"{k} ({v})" for k, v in reasons[name].most_common())
        out.append(f"- **{name}:** {rs or '—'}")
    out += ["", "## Checklist audit", "", "| Item | " + " | ".join(STATUSES) + " |",
            "| --- | " + " | ".join("---:" for _ in STATUSES) + " |"]
    for key in sorted(cl_key):
        out.append(f"| {key} | " + " | ".join(str(cl_key[key].get(s, 0)) for s in STATUSES) + " |")
    out.append("| **total** | " + " | ".join(str(cl_tot.get(s, 0)) for s in STATUSES) + " |")

    out += ["", "## Every fail", ""]
    n = 0
    for doc, cs, _, ks in results:
        claims = {c.id: c for c in cs.claims}
        rows = []
        for cc in ks.claims:
            c = claims.get(cc.claim_id)
            for r in cc.checks:
                if r.outcome == "fail" or (r.outcome == "pass" and r.severity != "none"):
                    n += 1
                    tag = r.outcome if r.outcome == "fail" else "pass (caveat)"
                    rows.append(f"| {r.check} | {tag}, {r.severity} | {_cell(c.quote if c else cc.claim_id, 180)} "
                                f"| {c.location.cite() if c else ''} | {_cell(r.detail, 420)} |")
        if rows:
            out += [f"### {doc.paper_id}", "", "| Check | Outcome | Claim | Location | Detail |",
                    "| --- | --- | --- | --- | --- |", *rows, ""]
    if n == 0:
        out.append("(none)")
    out += ["", "## Checklist items not backed", ""]
    rows = [f"| {doc.paper_id} | {a.key} | {a.status} | {_cell(a.justification, 160)} | {_cell(a.reason, 240)} |"
            for doc, _, _, ks in results for a in ks.checklist if a.status in ("unbacked", "contradicted", "not_checkable")]
    out += (["| Paper | Item | Status | Justification | Reason |", "| --- | --- | --- | --- | --- |", *rows]
            if rows else ["(none)"])
    return "\n".join(out) + "\n"
