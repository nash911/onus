"""Strength rubric (F4). (Phase 5)

Code extracts the rubric fields for every claim: seeds or runs, sample size, variance reported (cell spreads;
std / CI / error-bar language), statistical tests, and ablations. Sources, in order: the claim and its ±1
sentence, its linked evidence, then the paper's method / experiments / results / discussion / appendix text
(setup facts such as seeds and tests are stated there and are never linked as evidence for a result).
Datasets, baselines and settings are counted from the linked evidence by prompt `rubric_counts`, which must copy
labels; code keeps only labels that exist in a linked table or linked text. The prompt runs only when a fired
rule needs those counts.

Rules come from `rubrics/strength_rules.yaml` (Appendix B.2). Hedged claims are never flagged. Each fired rule
whose requirement is not met makes the check `fail` with severity "overstates" ("contradicts" when the
dominance check failed).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from onus.claims.quotes import match_key
from onus.llm.client import CallMeta, LLMError
from onus.schemas.result import CheckResult

from .context import PAPER_FEATURE_ROLES, ClaimContext, PaperContext
from .models import not_run
from .rubrics import StrengthRule, StrengthRules, default_strength_rules

MAX_LABELS = 20
EVIDENCE_TEXT_CHARS = 1500

_SEEDS = [
    re.compile(r"\b(\d{1,4})\s+(?:(?:different|independent|random|separate|distinct)\s+)*(?:random\s+)?"
               r"(?:seeds?|runs|trials|repetitions|repeats|executions|replicates|restarts|replications)\b", re.IGNORECASE),
    re.compile(r"\b(?:seeds?|runs|trials|repetitions|executions)\s*(?:=|:)\s*(\d{1,4})\b", re.IGNORECASE),
    re.compile(r"\b(?:averaged|repeated|run|mean)\s+(?:over|across)\s+(\d{1,4})\b", re.IGNORECASE),
]
_SAMPLE = [
    re.compile(r"\b[nN]\s*=\s*(\d[\d,]*)"),
    re.compile(r"\b(\d[\d,]*)\s+(?:\w+\s+)?(?:samples|examples|questions|participants|subjects|instances|problems|"
               r"items|prompts|papers|images|documents|episodes|sentences|queries|patients|cases|users)\b", re.IGNORECASE),
]
_VARIANCE = re.compile(r"\b(?:std|s\.d\.|stdev|standard deviations?|standard errors?|s\.e\.m?\.|confidence "
                       r"intervals?|\d{2}\s?% ci|error bars?|interquartile|iqr|variance|credible intervals?)\b|±|"
                       r"\+/-", re.IGNORECASE)
_TEST_STRONG = re.compile(r"\bp\s*[<=≤]\s*0?\.\d|\bp-values?\b|\bt-tests?\b|wilcoxon|mann[- ]whitney|bootstrap\w*|"
                          r"\banova\b|chi-?squared?|χ2|permutation tests?|fisher'?s exact|mcnemar|kruskal|bonferroni|"
                          r"benjamini|\bfdr\b|cohen'?s d|binomial tests?|z-tests?|significance tests?", re.IGNORECASE)
_TEST_WEAK = re.compile(r"statistically significant|significant at (?:the )?(?:\d|p|α|alpha)", re.IGNORECASE)
_ABLATION = re.compile(r"\bablat\w*|\bw/o\b|\bwithout (?:the )?[\w-]+ (?:module|component|term|loss|step|stage)",
                       re.IGNORECASE)


def _max_int(pats: list[re.Pattern[str]], text: str, cap: int) -> int | None:
    vals = [int(m.group(1).replace(",", "")) for p in pats for m in p.finditer(text)]
    vals = [v for v in vals if 0 < v <= cap]
    return max(vals) if vals else None


def text_features(text: str, *, claim: bool = False) -> dict:
    return {
        "seeds": _max_int(_SEEDS, text, 10000),
        "sample_size": _max_int(_SAMPLE, text, 10**8),
        "variance": bool(_VARIANCE.search(text)),
        "stat_test": bool(_TEST_STRONG.search(text) or (not claim and _TEST_WEAK.search(text))),
        "ablation": bool(_ABLATION.search(text)),
    }


class PaperFeatures:
    """Paper-level fallback, computed once per paper."""

    def __init__(self, pc: PaperContext):
        parts = [pc.text(p.start, p.end) for p in pc.body_paragraphs(PAPER_FEATURE_ROLES)]
        parts += [v.render(max_rows=200, max_chars=20000) for v in pc.views.values()]
        parts += [f.caption for f in pc.doc.figures]
        self.text = "\n".join(parts)
        self.features = text_features(self.text)
        self.spread = any(v.has_spread() for v in pc.views.values())


_WORDNUM = {w: i for i, w in enumerate(["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"])}
_NUM = r"(\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_PARTIAL = re.compile(
    r"\bmost\s+(?:of\s+(?:the\s+|our\s+|these\s+)?)?[a-z]+s\b|\b(?:the\s+)?majority\s+of\b|\bmostly\b|"
    r"\bexcept(?:\s+for)?\b|\ball\s+but\b|\bwith\s+the\s+exceptions?\b|\b(?:almost|nearly)\s+(?:all|every|always)\b|"
    r"\bnot\s+(?:all|always)\b|\bin\s+(?:many|several|some)\s+(?:cases|settings|instances|runs|tasks)\b", re.IGNORECASE)
_N_OF_M = re.compile(_NUM + r"\s+(?:out\s+)?of\s+(?:the\s+|our\s+)?" + _NUM + r"\b", re.IGNORECASE)


def _n(x: str) -> int:
    return int(x) if x.isdigit() else _WORDNUM[x.lower()]


UNIVERSAL_MARKERS = {"always", "never", "every", "universally", "without exception", "in all cases", "in every case"}


def partial_quantifier(texts: list[str]) -> str | None:
    """The first partial quantifier ("most ...", "the majority of", "7 of 10", "except", ...) in the texts, with
    a little context; None if there is none. Used against absolute or universal claims."""
    for t in texts:
        m = _PARTIAL.search(t)
        hit = (m.start(), m.end()) if m else None
        for nm in _N_OF_M.finditer(t):
            a, b = _n(nm.group(1)), _n(nm.group(2))
            if 0 < a < b and (hit is None or nm.start() < hit[0]):
                hit = (nm.start(), nm.end())
                break
        if hit:
            s, e = max(0, hit[0] - 40), min(len(t), hit[1] + 40)
            return " ".join(t[s:e].split())
    return None


def rubric_fields(ctx: ClaimContext, pf: PaperFeatures) -> dict:
    """Code-only rubric fields with their source ("claim", "evidence", "paper" or None)."""
    claim_f = text_features(ctx.claim.quote, claim=True)
    ctx_text = ctx.context.replace(ctx.claim.quote, " ")
    ev_f = text_features(ctx_text + "\n" + "\n".join(t for _, t in ctx.linked))
    ev_spread = any(v.has_spread() for v in ctx.tables)
    out: dict = {}
    for k in ("seeds", "sample_size"):
        for src, f in (("claim", claim_f), ("evidence", ev_f), ("paper", pf.features)):
            if f[k] is not None:
                out[k], out[f"{k}_source"] = f[k], src
                break
        else:
            out[k], out[f"{k}_source"] = None, None
    for k in ("variance", "stat_test", "ablation"):
        srcs = [("claim", claim_f[k]), ("evidence", ev_f[k] or (k == "variance" and ev_spread)),
                ("paper", pf.features[k] or (k == "variance" and pf.spread))]
        src = next((s for s, v in srcs if v), None)
        out[k], out[f"{k}_source"] = src is not None, src
    w = ctx.claim.wording
    out["universal_wording"] = w.scope in ("universal", "general") or any(
        m.lower() in UNIVERSAL_MARKERS for m in w.markers)
    out["partial_quantifier"] = partial_quantifier(
        [t for e, t in ctx.linked if e.relation in ("supports", "partial") and e.kind not in ("table", "table_cell")])
    return out


# ---------------------------------------------------------------- rubric_counts


class CountsOut(BaseModel):
    datasets: list[str] = Field(default_factory=list)
    baselines: list[str] = Field(default_factory=list)
    settings: list[str] = Field(default_factory=list)


def render_evidence(ctx: ClaimContext) -> str:
    blocks = []
    seen_tables = set()
    for e, text in ctx.linked:
        if e.kind == "table":
            view = next((v for v in ctx.tables if v.id == e.label), None)
            if view is not None:
                if view.id not in seen_tables:
                    blocks.append(view.render())
                    seen_tables.add(view.id)
                continue
        t = text if len(text) <= EVIDENCE_TEXT_CHARS else text[: EVIDENCE_TEXT_CHARS - 1] + "…"
        blocks.append(f"[{e.label}]\n{t}")
    return "\n\n".join(blocks)


def verify_labels(labels: list[str], ctx: ClaimContext) -> tuple[list[str], int]:
    texts = [match_key(t) for _, t in ctx.linked]
    kept: list[str] = []
    dropped = 0
    for lab in labels:
        lab = lab.strip()
        if not lab:
            continue
        key = match_key(lab)
        ok = bool(key) and (any(v.label_exists(lab) for v in ctx.tables) or any(key in t for t in texts))
        if ok and all(match_key(k) != key for k in kept):
            kept.append(lab)
        elif not ok:
            dropped += 1
    return kept[:MAX_LABELS], dropped


async def rubric_counts(ctx: ClaimContext, llm, *, run_index: int, paper_id: str,
                        metas: list[CallMeta]) -> dict | None:
    if not ctx.linked:
        return {"datasets": [], "baselines": [], "settings": [], "unverified_labels": 0}
    try:
        out, meta = await llm.complete_json("rubric_counts", {"claim": ctx.claim.quote, "evidence": render_evidence(ctx)},
                                            CountsOut, run_index=run_index, paper_id=paper_id)
        metas.append(meta)
    except LLMError:
        return None
    res: dict = {"unverified_labels": 0}
    for k in ("datasets", "baselines", "settings"):
        res[k], d = verify_labels(getattr(out, k), ctx)
        res["unverified_labels"] += d
    return res


# ---------------------------------------------------------------- rules


def fired_rules(ctx: ClaimContext, rules: StrengthRules) -> list[tuple[StrengthRule, list[str]]]:
    w = ctx.claim.wording
    out = []
    for r in rules.rules:
        if r.when == "absolute_or_universal":
            if w.strength == "absolute" or w.scope == "universal":
                out.append((r, [m for m in w.markers] or [w.strength, w.scope]))
        else:
            hits = r.hits(ctx.claim.quote)
            if hits:
                out.append((r, hits))
    return out


def _requirement(name: str, rub: dict, counts: dict | None, dom: CheckResult) -> tuple[str, str]:
    """(status, message). status: ok | unmet | contradicts | inconclusive."""
    if name == "stat_test":
        return ("ok", "") if rub["stat_test"] else ("unmet", "no statistical test reported")
    if name in ("baselines>=2", "settings>=2"):
        if counts is None:
            return "inconclusive", "dataset and baseline counts unavailable"
        if name == "baselines>=2":
            n = len(counts["baselines"])
            return ("ok", "") if n >= 2 else ("unmet", f"{n} verified baseline(s) in the linked evidence (needs 2)")
        names = {match_key(x) for x in counts["datasets"] + counts["settings"]}
        n = len(names)
        return ("ok", "") if n >= 2 else ("unmet", (f"{n} verified dataset(s) or setting(s) in the linked evidence "
                                                    "(needs 2)"))
    if name in ("dominance", "dominance_if_comparison"):
        if name == "dominance_if_comparison" and dom.outcome == "not_run" and \
                dom.data.get("reason") == "not a comparison claim":
            return "ok", ""
        if dom.outcome == "pass":
            return "ok", ""
        if dom.outcome == "fail":
            return "contradicts", "the dominance check failed"
        if dom.outcome == "inconclusive":
            return "inconclusive", "the dominance check was inconclusive"
        return "unmet", f"no dominance check ({dom.data.get('reason', 'not run')})"
    if name == "variance_or_3_seeds":
        if rub["variance"] or (rub["seeds"] or 0) >= 3:
            return "ok", ""
        return "unmet", f"no variance reported and {rub['seeds'] or 'no'} seed(s) or run(s) (needs 3)"
    if name == "no_partial_quantifier":
        q = rub.get("partial_quantifier") if rub.get("universal_wording", True) else None  # not for "of all X"
        return ("ok", "") if not q else ("unmet", f"the supporting evidence is partial: \"…{q}…\"")
    raise ValueError(name)


def needs_counts(fired: list[tuple[StrengthRule, list[str]]]) -> bool:
    return any(q in ("baselines>=2", "settings>=2") for r, _ in fired for q in r.requires)


async def check_strength(ctx: ClaimContext, pf: PaperFeatures, dom: CheckResult, llm, *, run_index: int,
                         paper_id: str, metas: list[CallMeta],
                         rules: StrengthRules | None = None) -> tuple[CheckResult, dict]:
    rules = rules or default_strength_rules()
    rub = rubric_fields(ctx, pf)
    if ctx.claim.origin == "checklist_number":
        return not_run("strength", "checklist justification (checked by the checklist audit)"), rub
    if ctx.claim.type not in rules.claim_types:
        return not_run("strength", f"{ctx.claim.type} claim (the rubric covers results)"), rub
    if ctx.claim.wording.strength == "hedged":
        return not_run("strength", "hedged wording"), rub
    fired = fired_rules(ctx, rules)
    if not fired:
        return not_run("strength", "no strength markers"), rub
    counts = None
    if needs_counts(fired):
        counts = await rubric_counts(ctx, llm, run_index=run_index, paper_id=paper_id, metas=metas)
        rub["counts_source"] = "rubric_counts" if counts is not None else "failed"
    else:
        rub["counts_source"] = "not_needed"
    if counts is not None:
        rub |= {k: counts[k] for k in ("datasets", "baselines", "settings")}
        rub["unverified_labels"] = counts["unverified_labels"]
    results = []
    for r, markers in fired:
        reqs = {q: _requirement(q, rub, counts, dom) for q in r.requires}
        st = [s for s, _ in reqs.values()]
        outcome = ("contradicts" if "contradicts" in st else "overstates" if "unmet" in st
                   else "inconclusive" if "inconclusive" in st else "pass")
        results.append({"rule": r.id, "title": r.title, "markers": markers, "outcome": outcome,
                        "requirements": {q: s for q, (s, _) in reqs.items()},
                        "unmet": [m for s, m in reqs.values() if m and s != "ok"]})
    data = {"rules": results}
    ev = ctx.evidence_ids
    bad = [x for x in results if x["outcome"] in ("contradicts", "overstates")]
    if bad:
        sev = "contradicts" if any(x["outcome"] == "contradicts" for x in bad) else "overstates"
        detail = " ".join(f"{x['title']} ({', '.join(x['markers'][:3])}): {'; '.join(x['unmet'])}." for x in bad)
        return CheckResult(check="strength", outcome="fail", severity=sev, detail=detail, data=data,  # type: ignore[arg-type]
                           evidence_ids=ev), rub
    if any(x["outcome"] == "inconclusive" for x in results):
        detail = " ".join(f"{x['title']}: {'; '.join(x['unmet'])}." for x in results if x["outcome"] == "inconclusive")
        return CheckResult(check="strength", outcome="inconclusive", detail=detail, data=data, evidence_ids=ev), rub
    return CheckResult(check="strength", outcome="pass",
                       detail="Rubric requirements met for: " + ", ".join(x["title"] for x in results) + ".",
                       data=data, evidence_ids=ev), rub
