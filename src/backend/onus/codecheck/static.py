"""Static paper-code check (F14, code-v1). (Phase 10)

1. `impl_facts` (extract role) lists checkable implementation facts from the method and experiment sections;
   each quote is verified against the paper (claims/quotes.py) or the fact is dropped.
2. Code search (code, no model): per fact, lines matching its value or its content words, scored, shown as
   windows of `path:line| text` (rubrics/code/static.yaml).
3. `impl_match` (judge role), one call per paper over all facts with their snippets: present | absent |
   contradicts with cited lines. Every citation must name a file in the code tree and its text must match the
   file within ±citation_slack lines (BUILD_PLAN §7.5); a present/contradicts verdict without a verified
   citation becomes "unverified". Fact ids outside the list are dropped.
4. Paper level (code): mismatch if any core fact contradicts, or at least `absent_share` of the verified core
   facts are absent (with at least `min_core` of them).

The claim-level `code_static` checks are attached in codecheck/apply.py.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from onus.claims.quotes import QuoteVerifier
from onus.codecheck.common import CodeFile, code_root, load_code_files, rubric
from onus.llm.client import CallMeta, LLMError
from onus.schemas.document import Document

FactKind = Literal["model", "algorithm", "hyperparameter", "seed_count", "dataset", "metric", "component"]
Status = Literal["present", "absent", "contradicts"]

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,}|\d+(?:\.\d+)?(?:e-?\d+)?")
_NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?:[eE]-?\d+)?(?![\w.]*[A-Za-z])")
STOP = {
    "the", "and", "for", "with", "from", "that", "this", "these", "those", "are", "was", "were", "been", "being",
    "its", "our", "their", "which", "into", "over", "under", "via", "per", "also", "can", "not", "such", "each",
    "other", "more", "most", "less", "very", "much", "many", "any", "all", "only", "but", "does", "did", "using",
    "use", "used", "based", "code", "should", "contain", "contains", "implement", "implements", "implemented",
    "implementation", "paper", "uses", "model", "method", "set", "sets", "value", "values", "number", "data",
    "run", "runs", "where", "when", "has", "have", "will", "would", "then", "than", "between", "within", "one", "two", "three", "true", "false", "none", "self", "return", "def", "import",
}


# ---------------------------------------------------------------- model outputs


class FactOut(BaseModel):
    kind: FactKind
    statement: str
    value: str = ""
    quote: str


class FactsOut(BaseModel):
    items: list[FactOut] = Field(default_factory=list)


class CitationOut(BaseModel):
    path: str
    line: int
    text: str = ""


class MatchOut(BaseModel):
    fact_id: str
    status: Status
    citations: list[CitationOut] = Field(default_factory=list)
    note: str = ""


class MatchesOut(BaseModel):
    items: list[MatchOut] = Field(default_factory=list)


# ---------------------------------------------------------------- results


@dataclass
class Fact:
    id: str
    kind: str
    statement: str
    value: str
    quote: str
    start: int
    end: int
    core: bool
    location: dict | None = None
    snippets: list[dict] = field(default_factory=list)  # {path, start, end, text}
    status: str = "not_run"  # present | absent | contradicts | unverified | not_run
    citations: list[dict] = field(default_factory=list)  # verified {path, line, text}
    note: str = ""

    def as_dict(self) -> dict:
        return {"fact_id": self.id, "kind": self.kind, "core": self.core, "fact": self.statement, "value": self.value,
                "quote": self.quote, "location": self.location, "status": self.status,
                "citations": [f"{c['path']}:{c['line']}" for c in self.citations], "cited_lines": self.citations,
                "note": self.note, "snippets": [f"{s['path']}:{s['start']}-{s['end']}" for s in self.snippets]}


@dataclass
class StaticReport:
    status: str  # done | not_run
    reason: str = ""
    facts: list[Fact] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)
    paper_level: dict = field(default_factory=dict)
    metas: list[CallMeta] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- sections


def section_text(doc: Document, roles: list[str], cap: int) -> tuple[str, list[tuple[int, int]]]:
    spans: list[tuple[int, int]] = []
    for role in roles:
        for s in doc.sections:
            if s.role == role and not any(a <= s.start < b for a, b in spans):
                spans.append((s.start, s.end))
    parts, total, used = [], 0, []
    for s, e in spans:
        if total >= cap:
            break
        chunk = doc.text[s:e][: cap - total]
        parts.append(chunk)
        used.append((s, s + len(chunk)))
        total += len(chunk)
    return "\n\n".join(parts), used


# ---------------------------------------------------------------- code search


def _terms(fact_statement: str, value: str) -> list[str]:
    words = [w.lower() for w in _WORD.findall(f"{value} {fact_statement}")]
    out = [w for w in words if w not in STOP and not re.fullmatch(r"\d+(?:\.\d+)?", w)]
    pairs = list(itertools.pairwise(out))
    out += [f"{a}_{b}" for a, b in pairs] + [f"{a}{b}" for a, b in pairs]
    return list(dict.fromkeys(out))


def _value_numbers(value: str) -> list[float]:
    out = []
    for m in _NUM.finditer(value.replace(",", "")):
        try:
            out.append(float(m.group(0)))
        except ValueError:
            pass
    return out


def _line_numbers(line: str) -> set[float]:
    out = set()
    for m in _NUM.finditer(line):
        try:
            out.add(float(m.group(0)))
        except ValueError:
            pass
    return out


def search_code(files: list[CodeFile], statement: str, value: str, *, context: int, max_snippets: int,
                max_chars: int) -> list[dict]:
    """Best windows for one fact: a line scores 3 per value hit (number equal by value, or the value as a
    case-insensitive substring) plus 1 per distinct search term on it."""
    terms = _terms(statement, value)
    nums = _value_numbers(value)
    vtext = value.strip().lower() if value and not _NUM.fullmatch(value.strip()) and len(value.strip()) >= 3 else ""
    scored: list[tuple[float, int, int]] = []  # (score, file index, line index)
    for fi, f in enumerate(files):
        for li, line in enumerate(f.lines):
            low = line.lower()
            s = 0.0
            if nums and any(n in _line_numbers(line) for n in nums):
                s += 3
            if vtext and vtext in low:
                s += 3
            hits = sum(1 for t in terms if t in low)
            s += hits
            if s >= 2 or (s >= 1 and hits and len(terms) <= 2):
                scored.append((s, fi, li))
    scored.sort(key=lambda x: (-x[0], x[1], x[2]))
    windows: list[dict] = []
    total = 0
    for _, fi, li in scored:
        f = files[fi]
        a, b = max(0, li - context), min(len(f.lines), li + context + 1)
        merged = False
        for w in windows:
            if w["fi"] == fi and a <= w["b"] and b >= w["a"]:
                w["a"], w["b"] = min(a, w["a"]), max(b, w["b"])
                merged = True
                break
        if not merged:
            if len(windows) >= max_snippets:
                continue
            windows.append({"fi": fi, "a": a, "b": b})
    out = []
    for w in windows:
        f = files[w["fi"]]
        text = "\n".join(f"{f.path}:{i + 1}| {f.lines[i][:300]}" for i in range(w["a"], w["b"]))
        if total + len(text) > max_chars:
            continue
        total += len(text)
        out.append({"path": f.path, "start": w["a"] + 1, "end": w["b"], "text": text})
    return out


# ---------------------------------------------------------------- citation verification (§7.5)


def _norm(s: str) -> str:
    return " ".join(s.split()).strip()


def verify_citation(c: CitationOut, by_path: dict[str, CodeFile], slack: int) -> dict | None:
    f = by_path.get(c.path.strip().lstrip("./"))
    if f is None or not (1 <= c.line <= len(f.lines)):
        return None
    want = _norm(c.text)
    lo, hi = max(1, c.line - slack), min(len(f.lines), c.line + slack)
    if not want:
        return None
    for n in range(lo, hi + 1):
        got = _norm(f.lines[n - 1])
        if got and (want == got or (len(want) >= 8 and want in got) or (len(got) >= 8 and got in want)):
            return {"path": f.path, "line": n, "text": f.lines[n - 1].strip()[:300]}
    return None


# ---------------------------------------------------------------- paper level


def paper_level(facts: list[Fact], absent_share: float, min_core: int) -> dict:
    core = [f for f in facts if f.core and f.status in ("present", "absent", "contradicts")]
    counts = {s: sum(1 for f in core if f.status == s) for s in ("present", "absent", "contradicts")}
    out = {"core_facts": len(core), **counts, "facts": len(facts),
           "unverified": sum(1 for f in facts if f.status == "unverified")}
    if counts["contradicts"]:
        ids = [f.id for f in core if f.status == "contradicts"]
        return out | {"mismatch": True, "reason": f"core fact(s) {', '.join(ids)} contradicted by the code"}
    if len(core) >= min_core and counts["absent"] / len(core) >= absent_share:
        return out | {"mismatch": True, "reason": f"{counts['absent']} of {len(core)} core facts not found in the "
                                                  "code"}
    if len(core) < min_core:
        return out | {"mismatch": None, "reason": f"fewer than {min_core} core facts could be checked"}
    return out | {"mismatch": False, "reason": f"{counts['present']} of {len(core)} core facts found in the code"}


def render_facts(facts: list[Fact]) -> str:
    blocks = []
    for f in facts:
        snips = "\n...\n".join(s["text"] for s in f.snippets) or "(the search found no matching lines)"
        val = f" value: {f.value}" if f.value else ""
        blocks.append(f"## Fact {f.id} ({f.kind}{val})\nStatement: {f.statement}\nPaper: \"{f.quote}\"\n"
                      f"Snippets:\n{snips}")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- main


async def static_check(doc: Document, paper_dir: Path | None, llm, *, paper_id: str | None = None) -> StaticReport:
    cfg = rubric("static.yaml")
    pid = paper_id or f"code:{doc.paper_id}"
    if not doc.code.present:
        return StaticReport("not_run", "no source code submitted")
    root = code_root(doc, paper_dir)
    if root is None:
        return StaticReport("not_run", "code directory not found")
    files = load_code_files(root, set(cfg["source_exts"]), int(cfg["max_file_bytes"]))
    if not files:
        return StaticReport("not_run", "no readable source files")
    text, spans = section_text(doc, list(cfg["section_roles"]), int(cfg["max_section_chars"]))
    if len(text) < 200:
        return StaticReport("not_run", "no method or experiment section text")
    rep = StaticReport("done")
    out = None
    for attempt in range(2):  # a second sample (its own cache key) when the first output is unusable
        try:
            out, meta = await llm.complete_json("impl_facts", {"sections": text, "max_facts": cfg["max_facts"]},
                                                FactsOut, run_index=attempt, paper_id=pid)
            rep.metas.append(meta)
            break
        except LLMError as e:
            rep.warnings.append(f"impl_facts attempt {attempt + 1} failed: {str(e)[:160]}")
    if out is None:
        return StaticReport("not_run", "impl_facts failed twice", warnings=rep.warnings)
    qv = QuoteVerifier(doc)
    seen: set[tuple[str, str]] = set()
    core_kinds = set(cfg["core_kinds"])
    for item in out.items:
        if len(rep.facts) >= int(cfg["max_facts"]):
            rep.dropped["over_cap"] = rep.dropped.get("over_cap", 0) + 1
            continue
        key = (item.kind, (item.value or item.statement).strip().lower())
        if key in seen:
            rep.dropped["duplicate"] = rep.dropped.get("duplicate", 0) + 1
            continue
        m = qv.find(item.quote, spans)
        if m is None:
            rep.dropped["unverified_quote"] = rep.dropped.get("unverified_quote", 0) + 1
            continue
        seen.add(key)
        loc = qv.locate(m.start, m.end)
        rep.facts.append(Fact(id=f"F{len(rep.facts) + 1}", kind=item.kind, statement=item.statement.strip(),
                              value=item.value.strip(), quote=doc.text[m.start:m.end], start=m.start, end=m.end,
                              core=item.kind in core_kinds, location=loc.model_dump() if loc else None))
    if not rep.facts:
        rep.status, rep.reason = "not_run", "no verifiable implementation facts"
        return rep
    for f in rep.facts:
        f.snippets = search_code(files, f.statement, f.value, context=int(cfg["context"]),
                                 max_snippets=int(cfg["max_snippets"]), max_chars=int(cfg["max_snippet_chars"]))
    searchable = [f for f in rep.facts if f.snippets]
    for f in rep.facts:
        if not f.snippets:
            f.status, f.note = "absent", "the code search found no matching lines"
    if searchable:
        try:
            mo, meta = await llm.complete_json("impl_match", {"facts": render_facts(searchable)}, MatchesOut,
                                               paper_id=pid)
            rep.metas.append(meta)
        except LLMError as e:
            rep.warnings.append(f"impl_match failed: {str(e)[:200]}")
            mo = MatchesOut()
        by_id = {f.id: f for f in searchable}
        by_path = {f.path: f for f in files}
        slack = int(cfg["citation_slack"])
        for r in mo.items:
            f = by_id.get(r.fact_id.strip())
            if f is None or f.status != "not_run":
                rep.dropped["unknown_fact_id"] = rep.dropped.get("unknown_fact_id", 0) + 1
                continue
            cites = [v for c in r.citations[:3] if (v := verify_citation(c, by_path, slack)) is not None]
            bad = len(r.citations[:3]) - len(cites)
            if bad:
                rep.dropped["unverified_citation"] = rep.dropped.get("unverified_citation", 0) + bad
            f.note = r.note.strip()[:300]
            if r.status == "absent":
                f.status = "absent"
            elif cites:
                f.status, f.citations = r.status, cites
            else:
                f.status = "unverified"
        for f in searchable:
            if f.status == "not_run":
                f.status, f.note = "unverified", "no answer for this fact"
    rep.paper_level = paper_level(rep.facts, float(cfg["absent_share"]), int(cfg["min_core"]))
    return rep
