"""Phase 3: quote verification, wording, input selection, checklist claims, extraction and the CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from onus.claims.extract import (
    claim_id,
    dedupe,
    extract_claims,
    has_quantity,
    load_claims,
    normalise_type,
    select_input,
)
from onus.claims.models import DraftClaim
from onus.claims.quotes import QuoteVerifier, keyify, match_key
from onus.claims.wording import load_lexicon, wording_for
from onus.config import get_settings
from onus.ingest.document_builder import build_document
from onus.ingest.lines import split_sentences
from onus.llm.client import LLMOutputError
from onus.llm.fake import FakeLLM
from onus.llm.prompts import PromptRegistry
from onus.schemas.document import ChecklistItem, Document, Line, Paragraph, Section
from onus.schemas.result import Location, Wording
from tests.pdfgen import make_paper, sample_pages

FP = "test"


# ---------------------------------------------------------------- a hand-built Document


def make_doc(sections: list[tuple[str, str, list[str]]], *, checklist: list[tuple[str, str, str]] = (),
             front: str = "Anonymous Author(s)", page_break_every: int = 4) -> Document:
    """sections: (title, role, paragraphs). checklist: (key, answer, justification). Paragraph i sits on
    page 1 + i // page_break_every; every paragraph is one Line with line_no = its index + 1."""
    parts: list[str] = []
    pos = 0
    paras: list[Paragraph] = []
    lines: list[Line] = []
    secs: list[Section] = []
    per_page: dict[int, int] = {}
    count = 0

    def emit(s: str) -> tuple[int, int]:
        nonlocal pos
        if parts:
            parts.append("\n\n")
            pos += 2
        start = pos
        parts.append(s)
        pos += len(s)
        return start, pos

    def para(t: str) -> None:
        nonlocal count
        s, e = emit(t)
        page = 1 + count // page_break_every
        per_page[page] = per_page.get(page, 0) + 1
        text = "".join(parts)
        paras.append(Paragraph(page=page, index=per_page[page], start=s, end=e,
                               sentences=split_sentences(text[s:e], s)))
        lines.append(Line(page=page, line_no=count + 1, text=t, bbox=(0, 0, 1, 1), start=s, end=e))
        count += 1

    emit("A Paper Title")
    para(front)
    all_secs = list(sections)
    if checklist:
        all_secs.append(("Agents4Science Paper Checklist", "checklist", []))
    items: list[ChecklistItem] = []
    for title, role, ps in all_secs:
        hs, _ = emit(title)
        for t in ps:
            para(t)
        if role == "checklist":
            for i, (key, ans, just) in enumerate(checklist, 1):
                ist, _ = emit(f"{i}. {key.title()}")
                para(f"Question: Does the paper do {key}?")
                para(f"Answer: [{ans}]")
                para(f"Justification: {just}")
                items.append(ChecklistItem(key=key, title=key.title(), question=f"Does the paper do {key}?",
                                           answer=ans, justification=just, start=ist, end=pos))
        secs.append(Section(id=f"s{len(secs)}", title=title, role=role, start=hs, end=pos, level=1))
    return Document(paper_id="p1", source="test", pdf_sha256="0" * 64, title="A Paper Title",
                    pages=max(p.page for p in paras), text="".join(parts), lines=lines, paragraphs=paras,
                    sections=secs, checklist=items)


ABSTRACT = ("We introduce a widget ranking method that is simple to deploy. On three benchmarks our method "
            "improves accuracy by 4.2 points over the strongest baseline. The state-of-the-art results hold "
            "across all datasets we tried.")
INTRO = ["Ranking widgets is an old problem that many papers have studied before us.",
         ("Our contributions are threefold. First, we propose an interaction-aware ranking objective. Second, "
         "we show that it outperforms prior methods on every benchmark.")]
CONCL = ["In conclusion, our method may generalize to other ranking tasks, which suggests broad usefulness."]
DISC = ["The gains come mainly from the interaction term, as the ablation shows.", "A later paragraph."]


@pytest.fixture
def doc() -> Document:
    return make_doc(
        [("Abstract", "abstract", [ABSTRACT]), ("1 Introduction", "introduction", INTRO),
         ("2 Related Work", "related_work", ["Prior rankers use pairwise losses and reach 80% accuracy."]),
         ("3 Results", "results", ["Table 1 reports 85.2% accuracy for our method."]),
         ("4 Discussion", "discussion", DISC), ("5 Conclusion", "conclusion", CONCL),
         ("References", "references", ["[1] A. Smith. Widgets. 2020."])],
        checklist=[("claims", "Yes", "The abstract states the main results."),
                   ("error_bars", "Yes", "We report standard deviations over 5 seeds in Table 2. See Section 4."),
                   ("theory", "NA", "The paper has no theorems."),
                   ("compute", "Yes", "Each run takes 2 hours on one CPU.")],
    )


# ---------------------------------------------------------------- quote verifier


def test_keyify_folds_case_quotes_dashes_and_space():
    key, idx = keyify("The  “State-of-the-Art”\n\nresult")
    assert key == 'the "stateoftheart" result'
    assert len(idx) == len(key) and idx[0] == 0 and idx == sorted(idx)
    assert match_key("state of the art") != match_key("state-of-the-art")  # spaces still count


def test_verifier_exact_returns_paper_text_and_location(doc):
    v = QuoteVerifier(doc)
    q = "On three benchmarks our method improves accuracy by 4.2 points over the strongest baseline."
    m = v.find(q)
    assert m is not None and m.method == "exact"
    assert doc.text[m.start : m.end] == q
    loc = v.locate(m.start, m.end)
    assert (loc.page, loc.paragraph, loc.sentence) == (1, 2, 2)
    assert loc.section == "Abstract" and loc.line_start == 2


def test_verifier_exact_ignores_case_quotes_and_whitespace(doc):
    v = QuoteVerifier(doc)
    m = v.find("on THREE benchmarks our method\nimproves accuracy by 4.2 points")
    assert m is not None and m.method == "exact"
    assert doc.text[m.start : m.end] == "On three benchmarks our method improves accuracy by 4.2 points"


def test_verifier_hyphenated(doc):
    v = QuoteVerifier(doc)
    for q in ("we propose an interac- tion-aware ranking objective",  # line-break hyphen kept by the model
              "we propose an interac-tion-aware ranking objective",
              "we propose an interactionaware ranking objective",  # ingest dropped a real hyphen
              "The state-of-the-art results hold across all datasets"):
        m = v.find(q)
        assert m is not None and m.method == "exact", q
    m = v.find("we propose an interac- tion-aware ranking objective")
    assert doc.text[m.start : m.end] == "we propose an interaction-aware ranking objective"


def test_verifier_line_numbers_inside_quote(doc):
    v = QuoteVerifier(doc)
    m = v.find("On three benchmarks our method improves 12 accuracy by 4.2 points over the 13 strongest baseline.")
    assert m is not None and m.method == "exact_nonum"
    assert doc.text[m.start : m.end].startswith("On three benchmarks") and doc.text[m.end - 9 : m.end] == "baseline."


def test_verifier_fuzzy(doc):
    v = QuoteVerifier(doc)
    m = v.find("On three benchmark our method improve accuracy by 4.2 points over the strongest baselines.")
    assert m is not None and m.method == "fuzzy" and 92 <= m.score < 100
    assert doc.text[m.start : m.end].startswith("On three benchmarks our method")


def test_verifier_miss_and_too_short(doc):
    v = QuoteVerifier(doc)
    assert v.find("Our approach reduces latency by a factor of ten on mobile devices.") is None
    assert v.find("4.2 points") is None  # under 20 characters of key


def test_verifier_prefers_regions(doc):
    v = QuoteVerifier(doc)
    text = doc.text
    q = "accuracy for our method"
    first = text.find(q)
    assert first > 0
    m = v.find(q, regions=[(first - 10, first + 40)])
    assert m is not None and m.start == first


def test_verifier_spans_paragraphs(doc):
    v = QuoteVerifier(doc)
    q = "The gains come mainly from the interaction term, as the ablation shows. A later paragraph."
    m = v.find(q)
    assert m is not None and v.sentences_spanned(m.start, m.end) == 2


# ---------------------------------------------------------------- synthetic PDFs: line numbers change nothing


@pytest.fixture(scope="module")
def pdf_pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Document, Document]:
    d = tmp_path_factory.mktemp("claims_pdf")
    a = make_paper(d / "a/papers/p1/paper.pdf", sample_pages(), line_numbers=True)
    b = make_paper(d / "b/papers/p1/paper.pdf", sample_pages(), line_numbers=False)
    return (build_document(a, paper_id="p1", source="agents4science", var_dir=d / "var"),
            build_document(b, paper_id="p1", source="agents4science", var_dir=d / "var"))


def test_pdf_quotes_with_raw_line_numbers_and_hyphenation(pdf_pair):
    numbered, _ = pdf_pair
    v = QuoteVerifier(numbered)
    # copied from the raw PDF: a review line number between lines, and the line-break hyphen
    m = v.find("show they improve accuracy by 4.2 points on two 4 benchmarks, e.g. Widget-A and Widget-B.")
    assert m is not None and m.method == "exact_nonum"
    loc = v.locate(m.start, m.end)
    assert loc.page == 1 and loc.line_start is not None and loc.line_end > loc.line_start
    m = v.find("Our method is effi- cient and simple.")
    assert m is not None and numbered.text[m.start : m.end] == "Our method is efficient and simple."


async def test_extraction_identical_with_and_without_line_numbers(pdf_pair):
    numbered, plain = pdf_pair
    assert select_input(numbered).text == select_input(plain).text
    items = {"items": [{"quote": "We study widgets and show they improve accuracy by 4.2 points on two benchmarks",
                        "type": "result", "is_main": True},
                       {"quote": "Our method is efficient and simple.", "type": "method"}]}
    out = []
    for d in pdf_pair:
        fake = FakeLLM({"extract_claims": items, "classify_scope": {"scope": "specific"}},
                       prompts=PromptRegistry())
        cs = await extract_claims(d, fake, fingerprint=FP)
        out.append([(c.id, c.quote, c.location.page, c.location.paragraph, c.location.sentence) for c in cs.claims])
        # the prompt never sees line numbers or the front matter
        sent = fake.calls[0]["variables"]["text"]
        assert "Synthetic Paper" not in sent.split("###", 1)[1]
    assert out[0] == out[1] and len(out[0]) == 2


# ---------------------------------------------------------------- input selection


def test_select_input_sections_and_exclusions(doc):
    inp = select_input(doc)
    assert inp.sections == ["Abstract", "1 Introduction", "4 Discussion", "5 Conclusion"]
    assert "Anonymous" not in inp.text and "Prior rankers" not in inp.text and "Table 1 reports" not in inp.text
    assert DISC[0] in inp.text and DISC[1] not in inp.text  # first Discussion paragraph only
    assert "Justification" not in inp.text and "Smith" not in inp.text
    assert all(any(s <= r0 < e for s, e in [(p.start, p.end) for p in doc.paragraphs]) for r0, _ in inp.regions)


def test_select_input_cap_keeps_abstract_and_conclusion(doc):
    inp = select_input(doc, max_chars=len(ABSTRACT) + len(CONCL[0]) + 10)
    assert inp.truncated and ABSTRACT in inp.text and CONCL[0] in inp.text and INTRO[0] not in inp.text


def test_select_input_without_conclusion_uses_closing_paragraphs():
    d = make_doc([("Abstract", "abstract", [ABSTRACT]), ("1 Introduction", "introduction", INTRO),
                  ("2 Discussion", "discussion", ["First discussion paragraph here.", "Middle paragraph here.",
                                                  "Closing paragraph one.", "Closing paragraph two."]),
                  ("References", "references", ["[1] Ref."])])
    t = select_input(d).text
    assert "First discussion" in t and "Closing paragraph one." in t and "Closing paragraph two." in t
    assert "Middle paragraph" not in t


def test_select_input_includes_contribution_sections():
    d = make_doc([("Abstract", "abstract", [ABSTRACT]), ("3 Results", "results", ["Numbers here."]),
                  ("4 Contributions", "other", ["We contribute a ranking objective and its analysis."]),
                  ("References", "references", ["[1] Ref."])])
    t = select_input(d).text
    assert "We contribute a ranking objective" in t and "Numbers here." not in t


# ---------------------------------------------------------------- wording


def test_strength_precedence():
    lex = load_lexicon()
    assert lex.strength_of("Our method significantly outperforms the baseline.")[0] == "strong"
    assert lex.strength_of("It always works on all inputs.")[0] == "absolute"
    assert lex.strength_of("This may significantly improve all results.")[0] == "hedged"
    assert lex.strength_of("We measure accuracy on a held-out set.") == ("plain", [])
    s, markers = lex.strength_of("A state of the art result that is robust.")
    assert s == "strong" and "state of the art" in markers and "robust" in markers
    assert lex.strength_of("We are the first to rank widgets.")[0] == "strong"
    assert lex.strength_of("First, we rank widgets.")[0] == "plain"
    assert lex.strength_of("Overall, we recall the results.")[0] == "plain"  # 'all' needs a word boundary


def test_scope_markers_and_precedence():
    lex = load_lexicon()
    assert lex.scope_of("It works in all cases, regardless of the input.")[0] == "universal"
    assert lex.scope_of("It applies to any domain.")[0] == "universal"
    assert lex.scope_of("We release all code and evaluate across 10 models.")[0] is None
    assert lex.scope_of("The method generalizes to new domains.")[0] == "general"
    assert lex.scope_of("In our experiments, it works in all cases.")[0] == "specific"
    assert lex.scope_of("The method transfers to unseen tasks.")[0] == "general"
    assert lex.scope_of("On the CIFAR-10 benchmark the error drops.")[0] == "specific"
    assert lex.scope_of("Accuracy improves by 3 points.")[0] is None


async def test_wording_markers_win_over_model_and_model_fills_gaps():
    fake = FakeLLM({"classify_scope": {"scope": "general"}})
    r = await wording_for("It works in every case.", llm=fake)
    assert r.wording.scope == "universal" and r.scope_source == "markers" and r.model_scope == "general"
    r = await wording_for("Accuracy improves by 3 points.", llm=fake)
    assert r.wording.scope == "general" and r.scope_source == "model"


async def test_wording_model_failure_defaults_to_specific():
    fake = FakeLLM({"classify_scope": LLMOutputError("bad")})
    r = await wording_for("Accuracy improves by 3 points.", llm=fake)
    assert r.wording.scope == "specific" and r.scope_source == "default" and r.error


# ---------------------------------------------------------------- helpers


def test_has_quantity():
    assert has_quantity("We use 5 random seeds.")
    assert has_quantity("Accuracy is 92.3% (n = 1,476).")
    assert has_quantity("It is 3x faster.")
    assert not has_quantity("See Section 4.2, Tables 2 and 3, and Fig. 5a.")
    assert not has_quantity("As shown in [12, 14] and Smith (2023).")
    assert not has_quantity("We ran GPT-4 on an A100 GPU.")
    assert not has_quantity("(1) we release code; (2) we release data.")


def test_normalise_type_and_id():
    assert normalise_type("Comparison ") == "comparison" and normalise_type("results") == "result"
    assert normalise_type("weird") == "result"
    assert claim_id("The  Result.") == claim_id("the result.") and len(claim_id("x")) == 12


def _draft(quote: str, start: int, end: int, main: bool = False) -> DraftClaim:
    loc = Location(page=1, paragraph=1, sentence=1, start=start, end=end)
    return DraftClaim(id=claim_id(quote), type="result", quote=quote, location=loc, is_main=main,
                      wording=Wording(strength="plain", scope="specific"), origin="llm")


def test_dedupe_ratio_and_containment():
    a = _draft("Our method improves accuracy by 4.2 points on three benchmarks.", 0, 60)
    b = _draft("Our method improves accuracy by 4.2 points on 3 benchmarks.", 500, 560, main=True)
    c = _draft("Our method improves accuracy", 0, 28)
    d = _draft("A completely different claim about latency on phones.", 900, 950)
    kept, removed = dedupe([a, b, c, d])
    assert [k.quote for k in kept] == [a.quote, d.quote] and len(removed) == 2
    assert kept[0].is_main  # main survives the merge


# ---------------------------------------------------------------- checklist claims and full extraction


def _fake(items: list[dict], scope: str = "specific") -> FakeLLM:
    return FakeLLM({"extract_claims": {"items": items}, "classify_scope": {"scope": scope}},
                   prompts=PromptRegistry())


async def test_extract_claims_end_to_end(doc):
    items = [
        {"quote": "On three benchmarks our method improves accuracy by 4.2 points over the strongest baseline.",
         "type": "result", "is_main": True},
        {"quote": "we show that it outperforms prior methods on every benchmark", "type": "Comparison"},
        {"quote": "Our approach halves latency on mobile phones compared to all rivals.", "type": "result"},
        {"quote": "on three benchmarks our method improves accuracy by 4.2 points over the strongest baseline",
         "type": "result"},
        {"quote": "short one", "type": "result"},
        {"quote": "In conclusion, our method may generalize to other ranking tasks, which suggests broad usefulness.",
         "type": "generality"},
    ]
    cs = await extract_claims(doc, _fake(items), fingerprint=FP)
    llm = cs.by_origin("llm")
    assert [c.type for c in llm] == ["result", "comparison", "generality"]
    assert llm[0].is_main and llm[0].location.section == "Abstract" and llm[0].section_role == "abstract"
    assert llm[1].wording.strength == "absolute" and llm[1].wording.scope == "specific"  # model; "every" is strength
    assert {"outperforms", "every"} <= set(llm[1].wording.markers)
    assert llm[2].wording.strength == "hedged"
    assert cs.stats["dropped"] == {"unverified": 1, "too_short": 1, "too_long": 0, "unlocatable": 0,
                                   "duplicate": 1, "over_cap": 0}
    assert {d.reason for d in cs.dropped} == {"unverified", "too_short", "duplicate"}
    for c in cs.claims:  # every kept quote is the paper's own text at its location
        assert match_key(doc.text[c.location.start : c.location.end]) == match_key(c.quote)

    cl = cs.by_origin("checklist")
    assert [c.checklist_key for c in cl] == ["claims", "error_bars", "theory", "compute"]
    assert all(c.type == "checklist" and c.location.section == "Agents4Science Paper Checklist" for c in cl)
    assert cl[1].quote == "We report standard deviations over 5 seeds in Table 2. See Section 4."
    nums = cs.by_origin("checklist_number")
    assert [(c.checklist_key, c.quote, c.type) for c in nums] == [
        ("error_bars", "We report standard deviations over 5 seeds in Table 2.", "result")]
    assert len({c.id for c in cs.claims}) == len(cs.claims)
    assert llm[2].wording.scope == "general" and cs.stats["scope_source"]["markers"] >= 1  # "generalize"
    assert cs.warnings == []


async def test_extract_failure_keeps_checklist_claims(doc):
    fake = FakeLLM({"extract_claims": LLMOutputError("garbage twice"), "classify_scope": {"scope": "specific"}})
    cs = await extract_claims(doc, fake, fingerprint=FP)
    assert cs.stats["extract_failed"] and not cs.by_origin("llm") and len(cs.by_origin("checklist")) == 4
    assert any("extract_claims failed" in w for w in cs.warnings)


async def test_cap_keeps_main_claims_first(doc, monkeypatch):
    import onus.claims.extract as ex

    monkeypatch.setattr(ex, "MAX_LLM_CLAIMS", 1)
    items = [{"quote": "We introduce a widget ranking method that is simple to deploy.", "type": "method"},
             {"quote": "In conclusion, our method may generalize to other ranking tasks", "type": "generality",
              "is_main": True}]
    cs = await extract_claims(doc, _fake(items), fingerprint=FP)
    assert [c.type for c in cs.by_origin("llm")] == ["generality"] and cs.stats["dropped"]["over_cap"] == 1


# ---------------------------------------------------------------- CLI


def test_cli_claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from onus.cli import app
    from onus.llm.client import set_client

    paper = tmp_path / "split/papers/p1"
    make_paper(paper / "paper.pdf", sample_pages(), line_numbers=True)
    monkeypatch.setenv("ONUS_VAR_DIR", str(tmp_path / "var"))
    get_settings.cache_clear()
    set_client(_fake([{"quote": "Our method is efficient and simple.", "type": "method", "is_main": True}]))
    out = tmp_path / "claims.md"
    try:
        res = CliRunner().invoke(app, ["claims", str(tmp_path / "split"), "--report", str(out)])
    finally:
        get_settings.cache_clear()
    assert res.exit_code == 0, res.output
    cs = load_claims(tmp_path / "var/claims/p1.r0.json")
    assert [c.quote for c in cs.claims] == ["Our method is efficient and simple."]
    assert "Our method is efficient and simple." in out.read_text()
