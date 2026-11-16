"""JSON shapes the API returns for papers and analyses (the RunResult itself is the schema contract)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from onus.db.models import Analysis, Paper

CODE_KEY = "+code-"  # as in code_stage.CODE_KEY (kept here so serialize stays import-light)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def source_url(source: str, external_id: str | None) -> str | None:
    """A link to the paper's public page (shown, never fetched by the API)."""
    if external_id and CODE_KEY in external_id:  # an analysis with uploaded code: same paper page
        external_id = external_id.split(CODE_KEY, 1)[0]
    if not external_id:
        return None
    if source in ("agents4science", "openreview"):
        return f"https://openreview.net/forum?id={external_id}"
    if source == "arxiv":
        return f"https://arxiv.org/abs/{external_id}"
    return None


def paper_ref(p: Paper) -> str:
    """The id the frontend uses in URLs: the external id for corpus papers, `source:id` otherwise."""
    if p.source == "agents4science":
        return p.external_id or p.id
    return f"{p.source}:{p.external_id}" if p.external_id else p.id


def paper_base(p: Paper) -> dict[str, Any]:
    display = p.display or {}
    return {
        "id": p.id,
        "ref": paper_ref(p),
        "source": p.source,
        "external_id": p.external_id,
        "title": display.get("title") or p.title,
        "pages": p.pages,
        "pdf_sha256": p.pdf_sha256,
        "source_url": source_url(p.source, p.external_id),
        "display": p.display,
    }


def analysis_header(a: Analysis | Any) -> dict[str, Any]:
    """Works for an Analysis row or a row from `repo.analysis_headers` (no result payload)."""
    out = {
        "id": a.id,
        "paper_id": a.paper_id,
        "mode": a.mode,
        "runs": a.runs,
        "fingerprint": a.fingerprint,
        "onus_version": a.onus_version,
        "summary": a.summary,
        "created_at": iso(a.created_at),
    }
    if isinstance(a, Analysis):
        out |= {"status": a.status, "cost_eur": a.cost_eur, "finished_at": iso(a.finished_at), "error": a.error}
    return out


def status_out(a: Analysis) -> dict[str, Any]:
    return {
        "id": a.id,
        "paper_id": a.paper_id,
        "status": a.status,
        "mode": a.mode,
        "progress": a.progress,
        "error": a.error,
        "created_at": iso(a.created_at),
        "finished_at": iso(a.finished_at),
    }


# ---------------------------------------------------------------- display repair: repository links cut by a line break

# An owner-only link ("https://github.com/owner/") that the frozen pipeline kept when the paper's PDF broke the
# link after the owner. Display only: the stored result is never changed.
_TRUNCATED_REPO = re.compile(
    r"https?://(?:www\.)?(?:github\.com|gitlab\.com|huggingface\.co)/[A-Za-z0-9][A-Za-z0-9_.\-]*/(?![A-Za-z0-9_\-])",
    re.IGNORECASE,
)
# Fields whose text is Onus's own wording or a link label; the paper's quotes and justifications stay verbatim.
_REPAIR_KEYS = ("reason", "caveat", "detail", "label", "snippet")


def _paper_texts(result: dict[str, Any]) -> list[str]:
    out = [c.get("quote") for c in result.get("claims") or [] if isinstance(c, dict)]
    out += [c.get("justification") for c in result.get("checklist") or [] if isinstance(c, dict)]
    return [t for t in out if isinstance(t, str) and t]


def _full_link(prefix: str, code_stage: dict[str, Any] | None, texts: list[str]) -> str | None:
    """The full repository URL a truncated link stands for: the code stage's source when the truncated link is a
    prefix of it, else the one link the paper's own (rejoined) quote text gives with that prefix."""
    from onus.api.code_stage import parse_repo, rejoin_split_links
    from onus.ingest.code_index import repo_urls

    p = prefix.lower()
    src = (code_stage or {}).get("source") or {}
    url = src.get("url") if isinstance(src, dict) else None
    if isinstance(url, str) and url.lower().startswith(p) and len(url) > len(prefix):
        return url
    found: list[str] = []
    for t in texts:
        for u in repo_urls(rejoin_split_links(t)):
            r = parse_repo(u)
            if r is not None and r.url.lower().startswith(p) and r.url not in found:
                found.append(r.url)
    return found[0] if len(found) == 1 else None  # ambiguous or none: leave the text as stored


def repair_repo_links(result: dict[str, Any], code_stage: dict[str, Any] | None = None) -> dict[str, Any]:
    """A copy of `result` where owner-only repository links in reasons, check details and evidence labels show the
    full repository URL. Returns `result` itself when nothing needs repair."""
    prefixes: set[str] = set()

    def scan(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k in _REPAIR_KEYS and isinstance(v, str):
                    prefixes.update(m.group(0) for m in _TRUNCATED_REPO.finditer(v))
                else:
                    scan(v)
        elif isinstance(o, list):
            for v in o:
                scan(v)

    scan(result)
    if not prefixes:
        return result
    texts = _paper_texts(result)
    fixes = {t: f for t in prefixes if (f := _full_link(t, code_stage, texts))}
    if not fixes:
        return result

    def sub(text: str) -> str:
        return _TRUNCATED_REPO.sub(lambda m: fixes.get(m.group(0), m.group(0)), text)

    def fix(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: sub(v) if k in _REPAIR_KEYS and isinstance(v, str) else fix(v) for k, v in o.items()}
        if isinstance(o, list):
            return [fix(v) for v in o]
        return o

    return fix(result)  # rebuilds every dict and list, so the stored result is never mutated
