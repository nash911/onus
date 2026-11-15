"""Markdown sample of extracted claims (`onus claims ... --report docs/claims_dev.md`)."""

from __future__ import annotations

from datetime import UTC, datetime

from onus.claims.models import ClaimSet
from onus.schemas.document import Document


def _cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def render_claims_report(results: list[tuple[Document, ClaimSet]], sample: int = 3) -> str:
    out = [
        "# Claims on dev (Phase 3)",
        "",
        (f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by `onus claims`. Fingerprint "
        f"`{results[0][1].fingerprint if results else '-'}`."),
        "",
        ("Model items are the quotes `extract_claims` returned; kept = verified, located and deduplicated. "
        "Checklist claims come from the parse (no LLM); numbers are justification sentences holding a number. "
         "€ new is this command's uncached spend (0 when every call was a cache hit)."),
        "",
        ("| Paper | Input chars | Model items | Kept | Main | Exact / no-num / fuzzy | Unverified | Duplicate "
        "| Other drops | Checklist | Numbers | Total | € new |"),
        "| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    tot = {"items": 0, "kept": 0, "unverified": 0, "eur": 0.0}
    for doc, cs in results:
        st, dr, bm = cs.stats, cs.stats["dropped"], cs.stats["by_method"]
        other = dr["too_short"] + dr["too_long"] + dr["unlocatable"] + dr["over_cap"]
        out.append(
            f"| {doc.paper_id} | {st['input_chars']} | {st['llm_items']} | {st['llm_claims']} | {st['main_claims']} "
            f"| {bm['exact']} / {bm['exact_nonum']} / {bm['fuzzy']} | {dr['unverified']} | {dr['duplicate']} "
            f"| {other} | {st['checklist_claims']} | {st['checklist_number_claims']} | {len(cs.claims)} "
            f"| {cs.cost.get('eur', 0):.4f} |"
        )
        tot["items"] += st["llm_items"]
        tot["kept"] += st["llm_claims"]
        tot["unverified"] += dr["unverified"]
        tot["eur"] += cs.cost.get("eur", 0)
    out += [
        "",
        (f"Totals: {tot['items']} model items, {tot['kept']} kept, {tot['unverified']} unverified "
        f"(dropped), €{tot['eur']:.4f}."),
        "",
        "Wording: strength from `rubrics/lexicon.yaml`; scope from lexicon markers, else `classify_scope`.",
        "",
        ("| Paper | hedged | plain | strong | absolute | scope: markers / model / default | marker vs model "
        "disagreements |"),
        "| --- | ---: | ---: | ---: | ---: | --- | ---: |",
    ]
    for doc, cs in results:
        sg, ss = cs.stats["strength"], cs.stats["scope_source"]
        out.append(f"| {doc.paper_id} | {sg['hedged']} | {sg['plain']} | {sg['strong']} | {sg['absolute']} "
                   f"| {ss['markers']} / {ss['model']} / {ss['default']} | {cs.stats['scope_disagreements']} |")

    for doc, cs in results[:sample]:
        out += ["", f"## {doc.paper_id}: {_cell(doc.title)}", "",
                f"Input sections: {', '.join(cs.stats['input_sections'])}.", "",
                "| # | Type | Main | Strength | Scope | Markers | Location | Match | Quote |",
                "| ---: | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for i, c in enumerate(cs.claims, 1):
            scope = c.wording.scope + ("" if c.scope_source == "markers" else f" ({c.scope_source})")
            out.append(
                f"| {i} | {c.type}{'/' + c.checklist_key if c.checklist_key else ''} | {'yes' if c.is_main else ''} "
                f"| {c.wording.strength} | {scope} | {_cell(', '.join(c.wording.markers))} "
                f"| {c.location.cite()}{'; ' + _cell(c.location.section) if c.location.section else ''} "
                f"| {c.match}{'' if c.match_score >= 100 else f' {c.match_score:g}'} | {_cell(c.quote)} |"
            )
        if cs.dropped:
            out += ["", "Dropped:", ""]
            out += [f"- *{d.reason}*: {_cell(d.quote)}" for d in cs.dropped]
        if cs.warnings:
            out += ["", "Warnings: " + "; ".join(cs.warnings)]
    return "\n".join(out) + "\n"
