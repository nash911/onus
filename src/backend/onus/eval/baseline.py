"""Single-prompt baseline. (Phase 7)

The same extracted claims (from a RunResult), judged by one prompt (`baseline_support`, judge role, the same
reasoning setting as the judge) over the paper text truncated to about 60k characters, plus table renderings
and the checklist section. Labels: `supported` | `not_supported`, with a reason. Claims are judged in batches
of up to 30 per call (one call per paper for most papers) rather than one call per claim, to keep the paper
text from being sent dozens of times; ids are validated, and a claim the model skips is `not_judged`.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from onus.llm.client import CallMeta, LLMError
from onus.schemas.document import Document
from onus.schemas.result import RunResult

from .mutate import table_renderings

PAPER_CHARS = 60_000
CHECKLIST_CHARS = 12_000
BATCH = 30
SKIP_ROLES = {"references"}


class BaselineItem(BaseModel):
    claim_id: str
    label: str = "supported"
    reason: str = ""


class BaselineOut(BaseModel):
    items: list[BaselineItem] = Field(default_factory=list)


class BaselineClaim(BaseModel):
    claim_id: str  # the RunResult claim id
    quote: str
    type: str
    checklist_key: str | None = None
    start: int
    end: int
    label: str  # supported | not_supported | not_judged
    reason: str = ""


class BaselineResult(BaseModel):
    paper_id: str
    fingerprint: str
    claims: list[BaselineClaim] = Field(default_factory=list)
    eur: float = 0.0
    calls: int = 0
    errors: list[str] = Field(default_factory=list)

    def label_of(self, claim_id: str) -> str | None:
        return next((c.label for c in self.claims if c.claim_id == claim_id), None)


def paper_text(doc: Document, limit: int = PAPER_CHARS) -> str:
    """Body text without references, truncated; the checklist is appended separately so truncation keeps it."""
    keep: list[tuple[int, int]] = []
    checklist: list[tuple[int, int]] = []
    tops = sorted((s for s in doc.sections if s.level <= 1), key=lambda s: s.start) or []
    if not tops:
        return doc.text[:limit]
    for s in tops:
        if s.role in SKIP_ROLES:
            continue
        (checklist if s.role in ("checklist", "ai_checklist") else keep).append((s.start, s.end))
    body = "\n".join(doc.text[a:b] for a, b in keep)
    if tops[0].start > 0:
        body = doc.text[: tops[0].start] + "\n" + body
    out = body[:limit] + ("\n[... truncated ...]" if len(body) > limit else "")
    ck = "\n".join(doc.text[a:b] for a, b in checklist)[:CHECKLIST_CHARS]
    return out + ("\n\n" + ck if ck else "")


async def run_baseline(doc: Document, result: RunResult, llm, *, fingerprint: str,
                       run_index: int = 0) -> BaselineResult:
    # paper order, never Onus's (verdict-sorted) order: harness v2 (docs/public/eval_protocol.md §4)
    claims = sorted(result.claims, key=lambda c: (c.location.start, c.location.end, c.id))
    br = BaselineResult(paper_id=result.paper["id"], fingerprint=fingerprint)
    text, tables = paper_text(doc), table_renderings(doc) or "(none)"
    labels: dict[str, tuple[str, str]] = {}
    for b in range(0, len(claims), BATCH):
        batch = claims[b : b + BATCH]
        ids = {f"C{i + 1}": c.id for i, c in enumerate(batch)}
        listing = "\n".join(f"C{i + 1}: \"{c.quote}\"" + (f" (checklist item: {c.checklist_key})"
                                                          if c.type == "checklist" else "")
                            for i, c in enumerate(batch))
        try:
            out, meta = await llm.complete_json("baseline_support", {"paper": text, "tables": tables,
                                                                     "claims": listing},
                                                BaselineOut, run_index=run_index, paper_id=br.paper_id)
            _account(br, meta)
        except LLMError as e:
            br.errors.append(str(e)[:200])
            continue
        for it in out.items:
            k = it.claim_id.strip().strip("[]").upper()
            if k in ids and ids[k] not in labels:
                lab = it.label.strip().lower().replace(" ", "_").replace("-", "_")
                labels[ids[k]] = ("not_supported" if lab.startswith("not") or lab == "unsupported" else "supported",
                                  " ".join(it.reason.split())[:500])
    for c in claims:
        lab, why = labels.get(c.id, ("not_judged", ""))
        br.claims.append(BaselineClaim(claim_id=c.id, quote=c.quote, type=c.type, checklist_key=c.checklist_key,
                                       start=c.location.start, end=c.location.end, label=lab, reason=why))
    return br


def _account(br: BaselineResult, meta: CallMeta) -> None:
    br.eur += meta.eur
    br.calls += meta.calls


def baseline_path(out_dir: Path, paper_id: str) -> Path:
    return out_dir / f"{paper_id.replace('/', '_').replace(':', '_')}.json"


def load_baselines(d: Path) -> dict[str, BaselineResult]:
    out = {}
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            b = BaselineResult.model_validate_json(f.read_text())
        except ValueError:
            continue
        out[b.paper_id] = b
    return out
