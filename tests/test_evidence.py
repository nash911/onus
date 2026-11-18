"""Phase 4: explicit references, retrieval pool and ranking, evidence selection, the link check and the CLI."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from onus.claims.extract import extract_claims
from onus.claims.quotes import QuoteVerifier
from onus.config import get_settings
from onus.evidence.explicit import ObjectIndex, explicit_refs
from onus.evidence.retrieve import CandidatePool, tokens
from onus.evidence.select import link_evidence, load_evidence, render_candidates
from onus.ingest.document_builder import build_document
from onus.llm.client import LLMCallError, LLMOutputError
from onus.llm.fake import FakeLLM
from onus.llm.prompts import PromptRegistry
from onus.schemas.document import Document
from tests.pdfgen import make_paper, sample_pages

FP = "test"
CLAIM_ABS = "We study widgets and show they improve accuracy by 4.2 points on two benchmarks"
CLAIM_EFF = "Our method is efficient and simple."
CLAIM_METHOD = "We describe the method in detail; Tables 1 and 2 summarise it, and Eq. 3 too."


@pytest.fixture(scope="module")
def doc(tmp_path_factory: pytest.TempPathFactory) -> Document:
    d = tmp_path_factory.mktemp("evidence_pdf")
    pdf = make_paper(d / "split/papers/p1/paper.pdf", sample_pages(), line_numbers=True)
    return build_document(pdf, paper_id="p1", source="agents4science", var_dir=d / "var")


@pytest.fixture
def index(doc) -> ObjectIndex:
    return ObjectIndex(doc, QuoteVerifier(doc))


def _span(doc: Document, quote: str) -> tuple[int, int]:
    m = QuoteVerifier(doc).find(quote)
    assert m is not None, quote
    return m.start, m.end


# ---------------------------------------------------------------- explicit references


def test_explicit_refs_resolve_and_flag_dangling(doc, index):
    # the claim's next sentence cites Table 1 (parsed) and Fig. 2 (no such figure)
    found, dangling = explicit_refs(index, *_span(doc, CLAIM_ABS))
    assert [c.label for c in found] == ["Table 1"] and found[0].kind == "table"
    assert dangling == ["Figure 2"]
    t = found[0]
    assert "Ours | 85.2 ± 0.4 | 0.74" in t.text and t.location is not None and t.location.page == 2


def test_explicit_refs_section_and_equation_and_missing_table(doc, index):
    found, dangling = explicit_refs(index, *_span(doc, CLAIM_METHOD))
    assert "Table 1" in [c.label for c in found]
    assert "Table 2" in dangling  # "Tables 1 and 2": only Table 1 exists


def test_context_is_one_sentence_each_side(doc, index):
    s, e = _span(doc, CLAIM_EFF)
    lo, hi = index.context_span(s, e)
    ctx = doc.text[lo:hi]
    assert "It needs no tuning at all" in ctx and "Table 1 and Fig. 2" in ctx
    assert "We study widgets" not in ctx  # two sentences back


# ---------------------------------------------------------------- retrieval


def test_pool_contents(doc, index):
    pool = CandidatePool(doc, index)
    texts = [c.text for c in pool.candidates]
    labels = [c.label for c in pool.candidates]
    assert "Table 1" in labels
    assert any("We describe the method in detail" in t for t in texts)  # method section
    assert not any("Our main limitation" in t for t in texts)  # limitations excluded
    assert not any("We study widgets" in t for t in texts)  # abstract excluded
    assert not any(t.startswith("Table 1: Main results") for t, c in zip(texts, pool.candidates, strict=True)
                   if c.kind == "text")  # the caption paragraph is covered by the table candidate


def test_bm25_ranks_and_excludes_the_claims_own_paragraph(doc, index):
    pool = CandidatePool(doc, index)
    keys = [pool.candidates[i].key for i in pool.bm25_rank("results on two benchmarks Ours Baseline accuracy",
                                                           -1, -1)]
    assert keys and keys[0] == "Table 1"
    s, e = _span(doc, CLAIM_METHOD)
    ranked = [pool.candidates[i] for i in pool.bm25_rank(CLAIM_METHOD, s, e)]
    assert all(not (c.start < e and c.end > s) for c in ranked)


def test_union_is_interleaved_unique_and_capped(doc, index):
    pool = CandidatePool(doc, index)
    cands, _ = pool.retrieve("widgets method", [0.0] * 4, -1, -1, cap=2)
    assert len(cands) <= 2 and len({c.key for c in cands}) == len(cands)
    assert tokens("The Accuracy is 85.2 on the benchmark") == ["accuracy", "85.2", "benchmark"]


# ---------------------------------------------------------------- selection and the link check


async def _claims(doc: Document):
    fake = FakeLLM({"extract_claims": {"items": [
        {"quote": CLAIM_ABS, "type": "result", "is_main": True},
        {"quote": CLAIM_EFF, "type": "method"}]},
        "classify_scope": {"scope": "specific"}}, prompts=PromptRegistry())
    return await extract_claims(doc, fake, fingerprint=FP)


def _selector(variables: dict, run_index: int) -> dict:
    """Mark the first [cited] candidate as supporting, the first other candidate partial, the rest unrelated,
    plus one invented id."""
    ids = re.findall(r"^\[(E\d+)\](\s\[cited\])?", variables["candidates"], flags=re.MULTILINE)
    items, partial_done = [], False
    for eid, cited in ids:
        if cited:
            items.append({"evidence_id": eid, "relation": "supports", "note": "Row 'Ours' gives 85.2."})
        elif not partial_done:
            items.append({"evidence_id": eid, "relation": "partial", "note": "Describes the method."})
            partial_done = True
        else:
            items.append({"evidence_id": eid, "relation": "unrelated"})
    items.append({"evidence_id": "E99", "relation": "supports"})
    return {"items": items}


async def test_link_evidence_end_to_end(doc):
    cs = await _claims(doc)
    fake = FakeLLM({"select_evidence": _selector}, prompts=PromptRegistry())
    es = await link_evidence(doc, cs, fake, fingerprint=FP)
    assert len(es.claims) == 2 and es.ledger.check == "link" and es.ledger.ran_on == 2
    assert es.ledger.not_run_on == len(cs.claims) - 2  # checklist claims are audited in Phase 5

    first = es.for_claim(cs.claims[0].id)
    assert first.explicit == ["Table 1"] and first.dangling == ["Figure 2"]
    ev = first.evidence
    assert ev[0].label == "Table 1" and ev[0].link_method == "explicit_ref" and ev[0].relation == "supports"
    assert [e.relation for e in ev] == ["supports", "partial"]  # unrelated retrieved items are dropped
    assert ev[1].link_method == "llm_selected"
    assert first.link.outcome == "pass" and first.link.evidence_ids == [e.id for e in ev]
    assert first.notes[ev[0].id] == "Row 'Ours' gives 85.2."
    assert es.stats["invalid_ids"] == 2  # E99 once per claim
    all_ids = [e.id for c in es.claims for e in c.evidence]
    assert all_ids == [f"ev{i}" for i in range(1, len(all_ids) + 1)]
    # the prompt saw the cited object marked and the claim's text, nothing outside the candidates
    call = fake.calls[0]["variables"]
    assert "[E1] [cited] Table 1" in call["candidates"] and call["claim"] == cs.claims[0].quote


async def test_model_omits_a_cited_object_and_everything_unrelated(doc):
    cs = await _claims(doc)
    fake = FakeLLM({"select_evidence": {"items": []}}, prompts=PromptRegistry())
    es = await link_evidence(doc, cs, fake, fingerprint=FP)
    first = es.for_claim(cs.claims[0].id)
    assert [(e.label, e.relation) for e in first.evidence] == [("Table 1", None)]
    assert first.link.outcome == "fail" and "Figure 2" in first.link.detail


async def test_selection_failure_keeps_unjudged_candidates(doc):
    cs = await _claims(doc)
    fake = FakeLLM({"select_evidence": LLMOutputError("bad")}, prompts=PromptRegistry())
    es = await link_evidence(doc, cs, fake, fingerprint=FP)
    first = es.for_claim(cs.claims[0].id)
    assert first.select_failed and first.link.outcome == "inconclusive"
    assert first.evidence[0].link_method == "explicit_ref"
    assert {e.link_method for e in first.evidence[1:]} <= {"retrieval"} and len(first.evidence) <= 4
    assert all(e.relation is None for e in first.evidence)
    assert any("select_evidence failed" in w for w in es.warnings)


async def test_embedding_failure_falls_back_to_bm25(doc):
    cs = await _claims(doc)
    fake = FakeLLM({"select_evidence": _selector}, prompts=PromptRegistry())

    async def broken(texts, *, paper_id=None, task="document"):
        raise LLMCallError("ServiceUnavailableError")

    fake.embed = broken  # type: ignore[method-assign]
    es = await link_evidence(doc, cs, fake, fingerprint=FP)
    assert any("BM25 only" in w for w in es.warnings)
    assert all(c.candidates for c in es.claims)


async def test_claim_quotes_are_embedded_as_queries_and_candidates_as_documents(doc):
    cs = await _claims(doc)
    fake = FakeLLM({"select_evidence": {"items": []}}, prompts=PromptRegistry())
    seen: list[tuple[str, str]] = []
    orig = fake.embed

    async def spy(texts, *, paper_id=None, task="document"):
        seen.extend((task, t) for t in texts)
        return await orig(texts, paper_id=paper_id, task=task)

    fake.embed = spy  # type: ignore[method-assign]
    await link_evidence(doc, cs, fake, fingerprint=FP)
    queries = [t for task, t in seen if task == "query"]
    assert len(queries) == 2 and not any(t.startswith("Instruct") for _, t in seen)


def test_render_candidates_marks_cited(doc, index):
    t = index.get("Table 1")
    out = render_candidates([t], {"Table 1"})
    assert out.startswith("[E1] [cited] Table 1\nTable 1: Main results")


# ---------------------------------------------------------------- CLI


def test_cli_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from onus.cli import app
    from onus.llm.client import set_client

    paper = tmp_path / "split/papers/p1"
    make_paper(paper / "paper.pdf", sample_pages(), line_numbers=False)
    monkeypatch.setenv("ONUS_VAR_DIR", str(tmp_path / "var"))
    get_settings.cache_clear()
    set_client(FakeLLM({"extract_claims": {"items": [{"quote": CLAIM_ABS, "type": "result"}]},
                        "classify_scope": {"scope": "specific"}, "select_evidence": _selector},
                       prompts=PromptRegistry()))
    out = tmp_path / "evidence.md"
    try:
        res = CliRunner().invoke(app, ["evidence", str(tmp_path / "split"), "--report", str(out)])
    finally:
        get_settings.cache_clear()
    assert res.exit_code == 0, res.output
    es = load_evidence(tmp_path / "var/evidence/p1.r0.json")
    assert es.claims[0].link.outcome == "pass"
    assert (tmp_path / "var/claims/p1.r0.json").is_file()
    text = out.read_text()
    assert "Table 1" in text and "sampled claims" in text


async def test_evidence_is_capped_with_cited_objects_kept(doc, monkeypatch):
    import onus.evidence.select as sel

    monkeypatch.setattr(sel, "MAX_EVIDENCE", 1)
    cs = await _claims(doc)

    def everything_partial(variables: dict, run_index: int) -> dict:
        ids = re.findall(r"^\[(E\d+)\]", variables["candidates"], flags=re.MULTILINE)
        return {"items": [{"evidence_id": i, "relation": "partial"} for i in ids]}

    fake = FakeLLM({"select_evidence": everything_partial}, prompts=PromptRegistry())
    es = await link_evidence(doc, cs, fake, fingerprint=FP)
    first = es.for_claim(cs.claims[0].id)
    assert [e.label for e in first.evidence] == ["Table 1"]  # the cited table survives the cap
    for c in es.claims:  # retrieved items fill only the room the cited objects leave
        others = [e for e in c.evidence if e.link_method != "explicit_ref"]
        assert len(others) <= max(1 - len(c.explicit), 0)
    assert es.stats["over_cap"] > 0


async def test_embedding_retries_shorter_on_context_errors(doc):
    import onus.evidence.retrieve as rt

    cs = await _claims(doc)
    fake = FakeLLM({"select_evidence": {"items": []}}, prompts=PromptRegistry())
    orig = fake.embed
    lengths: list[int] = []

    async def picky(texts, *, paper_id=None, task="document"):
        lengths.append(max(len(t) for t in texts))
        if max(len(t) for t in texts) > rt.EMBED_CHARS_RETRY and task == "document":
            raise LLMCallError("ContextWindowExceededError")
        return await orig(texts, paper_id=paper_id, task=task)

    fake.embed = picky  # type: ignore[method-assign]
    es = await link_evidence(doc, cs, fake, fingerprint=FP)
    assert not any("BM25 only" in w for w in es.warnings) and lengths[1] <= rt.EMBED_CHARS_RETRY


async def test_availability_claims_are_backed_by_the_submission_not_the_text(doc, index):
    from onus.evidence.select import availability_evidence
    from onus.schemas.document import CodeIndex

    c = (await _claims(doc)).by_origin("llm")[0]
    code_claim = c.model_copy(update={"type": "availability", "quote": "Our code is included in the supplementary material."})
    data_claim = c.model_copy(update={"type": "availability", "quote": "All outputs are in the supplement."})
    none = doc.model_copy(update={"code": CodeIndex()})
    assert availability_evidence(code_claim, none, index) is None and availability_evidence(c, none, index) is None
    src = doc.model_copy(update={"code": CodeIndex(present=True, files=[
        {"path": "run.py", "ext": ".py", "lines": 3, "bytes": 30, "is_source": True},
        {"path": "out.csv", "ext": ".csv", "lines": 3, "bytes": 30, "is_source": False}])})
    ev = availability_evidence(code_claim, src, index)
    assert ev.kind == "code" and ev.relation == "supports" and ev.link_method == "code_search" and "run.py" in ev.snippet
    assert availability_evidence(data_claim, src, index).relation == "partial"
    content = c.model_copy(update={"type": "availability", "quote": "The repository specifies every random seed."})
    assert availability_evidence(content, src, index).relation == "partial"  # what the code holds: not checked
    assert availability_evidence(c, src, index) is None  # only availability claims
    url = doc.model_copy(update={"code": CodeIndex(repo_urls_in_paper=["https://github.com/x/y"])})
    ev = availability_evidence(code_claim, url, index)
    assert ev.kind == "text" and ev.relation == "supports"
    assert availability_evidence(data_claim, url, index) is None
