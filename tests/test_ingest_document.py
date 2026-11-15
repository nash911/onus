"""End-to-end ingest on synthetic PDFs, the line-number barrier, and the dev corpus done-criteria."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from onus.config import get_settings, repo_root
from onus.ingest.document_builder import (
    build_document,
    ingest_paper_dir,
    ingest_stats,
    load_document,
    paper_dirs,
    save_document,
)
from onus.ingest.lines import Locator
from tests.pdfgen import make_paper, sample_pages


@pytest.fixture(scope="module")
def pair(tmp_path_factory: pytest.TempPathFactory):
    d = tmp_path_factory.mktemp("synthetic")
    with_nums = make_paper(d / "a/papers/p1/paper.pdf", sample_pages(), line_numbers=True,
                           footer="Submitted to a Conference on Widgets. Do not distribute.")
    without = make_paper(d / "b/papers/p1/paper.pdf", sample_pages(), line_numbers=False,
                         footer="Proceedings of the Conference on Widgets 2025.")
    a = build_document(with_nums, paper_id="p1", source="agents4science", var_dir=d / "var")
    b = build_document(without, paper_id="p1", source="agents4science", var_dir=d / "var")
    return a, b


def test_structure(pair) -> None:
    doc, _ = pair
    assert doc.title == "A Synthetic Paper About Widgets"
    roles = [(s.number, s.role) for s in doc.sections]
    assert roles == [(None, "abstract"), ("1", "introduction"), ("2", "method"), ("3", "limitations"),
                     (None, "references")]
    assert "efficient and simple" in doc.text  # de-hyphenated
    assert doc.limitations_spans
    t = next(t for t in doc.tables if t.id == "Table 1")
    assert t.parsed and t.caption.startswith("Table 1:")
    assert [c.raw for c in t.rows[-1]] == ["Ours", "85.2 ± 0.4", "0.74"]
    assert t.rows[-1][1].spread == pytest.approx(0.4)
    labels = {(m.label, m.resolved_id) for m in doc.mentions}
    assert ("Table 1", "Table 1") in labels and ("Table 2", None) in labels and ("Section 3", "sec:3") in labels


def test_line_numbers_are_metadata_only(pair) -> None:
    """The same paper with and without review line numbers (and with a review vs camera-ready footer) must
    give identical text, paragraphs, sentences and sections: nothing downstream may see the difference."""
    a, b = pair
    assert a.text == b.text
    assert [(p.page, p.index, p.start, p.end, p.sentences) for p in a.paragraphs] == \
           [(p.page, p.index, p.start, p.end, p.sentences) for p in b.paragraphs]
    assert [s.model_dump() for s in a.sections] == [s.model_dump() for s in b.sections]
    assert any(ln.line_no for ln in a.lines) and not any(ln.line_no for ln in b.lines)
    for word in ("Submitted", "distribute", "Proceedings"):
        assert word not in a.text and word not in b.text


def test_every_sentence_locates_with_and_without_line_numbers(pair) -> None:
    for doc in pair:
        loc = Locator(doc)
        for p in doc.paragraphs:
            for k, (s, e) in enumerate(p.sentences, start=1):
                got = loc.locate(s, e)
                assert got is not None and (got.page, got.paragraph, got.sentence) == (p.page, p.index, k)
    a, b = pair
    i = a.text.index("Widgets are everywhere")
    la, lb = Locator(a).locate(i, i + 20), Locator(b).locate(i, i + 20)
    assert la.cite().startswith(lb.cite()) and la.line_start is not None and lb.line_start is None
    assert la.section == "1 Introduction"


def test_save_and_load_roundtrip(pair, tmp_path: Path) -> None:
    doc, _ = pair
    p = save_document(doc, tmp_path)
    assert p == tmp_path / "docs" / "p1.json"
    assert load_document(p) == doc


def test_cli_ingest_and_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from onus.cli import app

    paper = tmp_path / "split/papers/p2"
    make_paper(paper / "paper.pdf", sample_pages(), line_numbers=False)
    runner = CliRunner()
    # data root outside the repo with var inside it: refused (hard rule 4)
    monkeypatch.setenv("ONUS_VAR_DIR", str(repo_root() / "var"))
    get_settings.cache_clear()
    res = runner.invoke(app, ["ingest", str(paper)])
    assert res.exit_code == 1 and "outside the repository" in res.output.replace("\n", " ")
    # var outside the repo too: allowed
    monkeypatch.setenv("ONUS_VAR_DIR", str(tmp_path / "var"))
    get_settings.cache_clear()
    res = runner.invoke(app, ["ingest-all", "--data-root", str(tmp_path / "split")])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "var/docs/p2.json").is_file()
    out = tmp_path / "report.md"
    res = runner.invoke(app, ["ingest-report", "--data-root", str(tmp_path / "split"), "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert "| p2 |" in out.read_text()
    get_settings.cache_clear()


# ---------------------------------------------------------------- dev corpus (Phase 2 done criteria)

DEV = repo_root() / "data" / "dev"


@pytest.fixture(scope="module")
def dev_docs(tmp_path_factory: pytest.TempPathFactory):
    dirs = paper_dirs(DEV)
    if not dirs:
        pytest.skip("data/dev not staged")
    var = tmp_path_factory.mktemp("devvar")
    return [ingest_paper_dir(d, var) for d in dirs]


def test_dev_corpus_done_criteria(dev_docs) -> None:
    assert len(dev_docs) == 10
    for doc in dev_docs:
        st = ingest_stats(doc)
        assert st["has_abstract"] and st["has_references"], doc.paper_id
        assert st["sections"] >= 3, doc.paper_id
        if any(s.role == "checklist" for s in doc.sections):
            assert len(doc.checklist) >= 7, doc.paper_id
            assert all(c.answer for c in doc.checklist), doc.paper_id
        if st["line_no_coverage"] is not None:
            assert st["line_no_coverage"] >= 0.9, (doc.paper_id, st["line_no_coverage"])
        assert st["sentences_located"] == st["sentences"] > 0, doc.paper_id


def test_dev_corpus_has_papers_with_and_without_line_numbers(dev_docs) -> None:
    numbered = [any(ln.line_no for ln in d.lines) for d in dev_docs]
    assert any(numbered) and not all(numbered)
