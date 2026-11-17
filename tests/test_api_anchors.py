"""PDF anchors (Phase 13a): placement methods on a dev PDF, the /pdf and /anchors endpoints, the cache, dev-seed and
anchors-eval. No network and no LLM calls: the RunResult is built from the ingested Document."""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from onus.api import anchors as A
from onus.api.main import create_app
from onus.config import Settings, repo_root
from onus.ingest.document_builder import ingest_pdf, paper_dirs
from onus.schemas.result import RunResult
from tests.test_api import FP, FakePipeline, make_result, write_bundle

DEV = repo_root() / "data" / "dev"


@pytest.fixture(scope="module")
def dev(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """The first dev paper (sorted) whose ingested Document has a parsed table (chosen by rule, not by id)."""
    dirs = paper_dirs(DEV)
    if not dirs:
        pytest.skip("data/dev not staged")
    var = tmp_path_factory.mktemp("anchorvar")
    for d in dirs:
        doc = ingest_pdf(d / "paper.pdf", var, source="agents4science", paper_id=d.name)
        if any(t.parsed for t in doc.tables):
            pdf = d / "paper.pdf"
            return {"dir": d, "pdf": pdf, "doc": doc, "sha": hashlib.sha256(pdf.read_bytes()).hexdigest()}
    pytest.skip("no dev paper with a parsed table")


def _with_doc(dev: dict, var: Path) -> Path:
    """A var dir whose ingest cache already holds the paper's Document (as after `onus analyze`)."""
    from onus.ingest.document_builder import save_document

    save_document(dev["doc"], var)
    return var


def _loc(doc, start: int, end: int) -> dict:
    para = next(p for p in doc.paragraphs if p.start <= start < p.end)
    return {"page": para.page, "paragraph": para.index, "sentence": 1, "start": start, "end": end}


def _two_lines(doc):
    """Two consecutive lines of one paragraph on one page, each with at least 6 words."""
    for p in doc.paragraphs:
        ls = [ln for ln in doc.lines if p.start <= ln.start < p.end]
        for a, b in itertools.pairwise(ls):
            if a.page == b.page and len(a.text.split()) >= 6 and len(b.text.split()) >= 6 and b.bbox[1] > a.bbox[1]:
                return a, b
    raise AssertionError("no two-line paragraph")


def _claim(cid: str, quote: str, loc: dict, evidence: list | None = None) -> dict:
    return {"id": cid, "type": "result", "quote": quote, "location": loc, "is_main": False,
            "wording": {"strength": "plain", "scope": "specific", "markers": []}, "evidence": evidence or [],
            "verdict": "supported", "reason": "r", "risk": 0}


def build_result(dev: dict) -> dict:
    doc = dev["doc"]
    a, b = _two_lines(doc)
    sent_start = a.start
    sent_end = a.end
    two_start = a.start + len(a.text) // 2
    two_start = doc.text.index(" ", two_start) + 1
    two_end = b.start + len(b.text) // 2
    two_end = doc.text.index(" ", two_end)
    table = next(t for t in doc.tables if t.parsed)
    tab_ev = {"id": "ev1", "kind": "table", "label": table.id, "snippet": table.caption or table.id,
              "location": _loc(doc, sent_start, sent_end), "link_method": "explicit_ref", "relation": "supports"}
    text_ev = {"id": "ev2", "kind": "text", "label": "Section", "snippet": doc.text[two_start:two_end],
               "location": _loc(doc, two_start, two_end), "link_method": "retrieval", "relation": "supports"}
    code_ev = {"id": "ev3", "kind": "code", "label": "src/x.py:1", "snippet": "x = 1", "location": None,
               "link_method": "code_search", "relation": "supports"}
    shifted = _loc(doc, sent_start + 9, sent_end + 9)
    r = make_result(dev["dir"].name, sha=dev["sha"])
    r["paper"]["pages"] = doc.pages
    r["claims"] = [
        _claim("c_line", doc.text[sent_start:sent_end], _loc(doc, sent_start, sent_end), [tab_ev, code_ev]),
        _claim("c_two", doc.text[two_start:two_end], _loc(doc, two_start, two_end), [text_ev]),
        _claim("c_shift", doc.text[sent_start:sent_end], shifted),
    ]
    RunResult.model_validate(r)
    return r


def _inside(rect: list[float], size: list[float]) -> bool:
    return 0 <= rect[0] < rect[2] <= size[0] + 0.5 and 0 <= rect[1] < rect[3] <= size[1] + 0.5


# ---------------------------------------------------------------- placement


@pytest.fixture(scope="module")
def placed(dev) -> tuple[dict, dict]:
    r = build_result(dev)
    return r, A.compute_anchors(dev["doc"], dev["pdf"], r)


def test_claim_offsets_on_the_right_page_inside_the_page(dev, placed):
    r, an = placed
    assert an["pdf"] is True and len(an["page_sizes"]) == dev["doc"].pages
    (it,) = an["items"]["c_line"]
    loc = r["claims"][0]["location"]
    assert it["method"] == "offsets" and it["page"] == loc["page"]
    assert len(it["rects"]) == 1
    size = an["page_sizes"][it["page"] - 1]
    assert all(_inside(x, size) for x in it["rects"])


def test_two_line_span_gives_two_trimmed_rects(dev, placed):
    _, an = placed
    a, b = _two_lines(dev["doc"])
    (it,) = an["items"]["c_two"]
    assert it["method"] == "offsets" and len(it["rects"]) == 2
    first, second = it["rects"]
    assert first[0] > a.bbox[0] + 5 and abs(first[2] - a.bbox[2]) < 2  # starts mid-line, runs to the line end
    assert abs(second[0] - b.bbox[0]) < 2 and second[2] < b.bbox[2] - 5  # starts at the line start, ends mid-line
    assert first[1] < second[1]
    assert an["items"]["c_two:ev2"][0]["method"] == "offsets"


def test_table_evidence_uses_the_table_box(dev, placed):
    _, an = placed
    table = next(t for t in dev["doc"].tables if t.parsed)
    (it,) = an["items"]["c_line:ev1"]
    assert it["method"] == "table" and it["page"] == table.page
    assert it["rects"] == [A._r(table.bbox)]


def test_shifted_offsets_fall_back_to_search(dev, placed):
    r, an = placed
    (it,) = an["items"]["c_shift"][:1]
    assert it["method"] == "search" and it["page"] == r["claims"][2]["location"]["page"]
    assert an["items"]["c_shift"][0]["rects"]


def test_code_evidence_is_left_out_and_nothing_missing(placed):
    _, an = placed
    assert "c_line:ev3" not in an["items"] and "c_line:ev3" not in an["missing"]
    assert an["missing"] == []


def test_span_check_rules(dev):
    doc = dev["doc"]
    a, _ = _two_lines(doc)
    assert A.checked_span(doc, a.start, a.end, a.text) == (a.start, a.end)
    words = a.text.split()
    cut = A.checked_span(doc, a.start, a.end, " ".join(words[:4]))  # a clipped snippet: up to its last word
    assert cut is not None and doc.text[cut[0]:cut[1]].split() == words[:4]
    assert A.checked_span(doc, a.start + 3, a.end, a.text) is None
    assert A.snippet_core("one two thr…") == ("one two", True)


def test_unplaceable_item_is_missing(dev):
    r = build_result(dev)
    r["claims"] = [_claim("ghost", "zzqx wvyk " * 6, {"page": 1, "paragraph": 999, "sentence": 1, "start": 0,
                                                       "end": 5})]
    an = A.compute_anchors(dev["doc"], dev["pdf"], r)
    assert an["items"] == {} and an["missing"] == ["ghost"]


# ---------------------------------------------------------------- endpoints and cache


@pytest.fixture
def api(dev, tmp_path: Path):
    from onus.ingest.fetch import store_upload

    r = build_result(dev)
    nopdf = make_result("PaperNOPDF1", sha="ab" * 32)
    import_dir = tmp_path / "deploy"
    write_bundle(import_dir / "bundle_t.jsonl.gz", [r, nopdf])
    settings = Settings(_env_file=None, onus_var_dir=tmp_path / "var", database_url=f"sqlite:///{tmp_path}/t.db",
                        onus_results_import_dir=import_dir, berget_api_key="", featured_papers="")
    store_upload(dev["pdf"].read_bytes(), _with_doc(dev, settings.var_dir))
    app = create_app(settings, analyzer=FakePipeline(), fetcher=lambda *a: b"", fingerprint=FP,
                     frontend_dist=tmp_path / "dist", eval_log=tmp_path / "none.md")
    with TestClient(app) as c:
        ids = {row["ref"]: row["analysis_id"] for row in c.get("/api/papers").json()["items"]}
        yield {"c": c, "with": ids[dev["dir"].name], "without": ids["PaperNOPDF1"], "settings": settings,
               "sha": dev["sha"]}


def test_pdf_endpoint_streams_inline_with_etag(api):
    c = api["c"]
    res = c.get(f"/api/analyses/{api['with']}/pdf")
    assert res.status_code == 200 and res.headers["content-type"] == "application/pdf"
    assert res.headers["content-disposition"].startswith("inline")
    assert res.headers["etag"] == f'"{api["sha"]}"' and "max-age" in res.headers["cache-control"]
    assert res.content.startswith(b"%PDF")
    again = c.get(f"/api/analyses/{api['with']}/pdf", headers={"If-None-Match": res.headers["etag"]})
    assert again.status_code == 304


def test_no_pdf_and_unknown_id(api):
    c = api["c"]
    res = c.get(f"/api/analyses/{api['without']}/anchors")
    assert res.status_code == 200 and res.json() == {"pdf": False, "page_sizes": [], "items": {}, "missing": []}
    res = c.get(f"/api/analyses/{api['without']}/pdf")
    assert res.status_code == 404 and res.json() == {"detail": "no_pdf"}
    for path in ("pdf", "anchors"):
        assert c.get(f"/api/analyses/no-such-id/{path}").status_code == 404


def test_anchors_endpoint_and_cache_reuse(api, monkeypatch):
    c = api["c"]
    first = c.get(f"/api/analyses/{api['with']}/anchors")
    assert first.status_code == 200
    body = first.json()
    assert body["pdf"] is True and {"c_line", "c_two", "c_line:ev1", "c_two:ev2", "c_shift"} <= set(body["items"])
    cache = A.cache_path(api["settings"].var_dir, api["with"])
    assert cache.is_file() and json.loads(cache.read_text())["stamp"]["pdf_sha256"] == api["sha"]

    def boom(*a, **k):
        raise AssertionError("recomputed instead of reading the cache")

    monkeypatch.setattr(A, "compute_anchors", boom)
    assert c.get(f"/api/analyses/{api['with']}/anchors").json() == body
    # a stale stamp (another anchors version) is recomputed
    data = json.loads(cache.read_text())
    data["stamp"]["version"] = -1
    cache.write_text(json.dumps(data))
    monkeypatch.undo()
    assert c.get(f"/api/analyses/{api['with']}/anchors").json() == body
    assert json.loads(cache.read_text())["stamp"]["version"] == A.ANCHORS_VERSION


def test_stored_pdf_never_takes_a_path():
    assert A.stored_pdf(Path("/tmp"), "../../etc/passwd") is None
    assert A.stored_pdf(Path("/tmp"), None) is None


# ---------------------------------------------------------------- dev-seed and anchors-eval


def _results_dir(dev, tmp_path: Path) -> tuple[Path, Path]:
    res = tmp_path / "results"
    res.mkdir()
    r = build_result(dev)
    r["claims"][0]["verdict"], r["claims"][0]["risk"] = "overstated", 3
    (res / f"{dev['dir'].name}.json").write_text(json.dumps(r))
    other = make_result("PaperNOPDF2", sha="cd" * 32)
    (res / "PaperNOPDF2.json").write_text(json.dumps(other))
    split = tmp_path / "split" / "papers"
    for pid, src in ((dev["dir"].name, dev["pdf"]), ("PaperNOPDF2", dev["pdf"])):
        (split / pid).mkdir(parents=True)
        (split / pid / "paper.pdf").symlink_to(src)
    return res, tmp_path / "split"


def test_dev_seed_is_idempotent_and_local(dev, tmp_path):
    from onus.api.devseed import SeedError, seed

    res, split = _results_dir(dev, tmp_path)
    s = Settings(_env_file=None, onus_var_dir=tmp_path / "var", database_url=f"sqlite:///{tmp_path}/var/app.db")
    first = seed(s, res, split, n=1, no_pdf=True)
    assert [x.pdf for x in first] == [True, False]
    assert first[0].paper_id == dev["dir"].name  # table evidence ranks first
    assert all(x.status == "inserted" and x.url == f"/analyses/{x.analysis_id}" for x in first)
    again = seed(s, res, split, n=1, no_pdf=True)
    assert [x.analysis_id for x in again] == [x.analysis_id for x in first]
    assert all(x.status == "unchanged" for x in again)
    with pytest.raises(SeedError):
        seed(Settings(_env_file=None, onus_var_dir=tmp_path / "var", database_url=f"sqlite:///{tmp_path}/out.db"),
             res, split)
    with pytest.raises(SeedError):
        seed(Settings(_env_file=None, onus_var_dir=tmp_path / "var", database_url="postgresql://x/y"), res, split)


def test_anchors_eval_scores(dev, tmp_path):
    from onus.eval.anchors_eval import render, run, token_f1

    assert token_f1(["a", "b"], ["a", "b"]) == 1.0 and token_f1([], ["a"]) == 0.0
    assert token_f1(["a", "b", "c", "d"], ["a", "b"]) == pytest.approx(2 / 3)
    res, split = _results_dir(dev, tmp_path)
    (res / "PaperNOPDF2.json").unlink()
    summary, rows, skipped = run(res, split, _with_doc(dev, tmp_path / "var"))
    assert skipped == []
    by = {r["key"]: r for r in rows}
    assert by["c_line"]["method"] == "offsets" and by["c_line"]["f1"] >= 0.95 and by["c_line"]["page_match"]
    assert by["c_shift"]["method"] == "search" and "c_line:ev3" not in by
    assert summary["by_kind"]["claim"]["n"] == 3
    assert "Worst" in render(summary, rows)


def test_rotated_page_rects_match_the_displayed_page():
    import pymupdf

    d = pymupdf.open()
    p = d.new_page(width=600, height=800)
    p.insert_text((100, 100), "hello rotate")
    p.set_rotation(90)
    pdf = A.Pdf(d)
    (w,) = [x for x in p.get_text("words") if x[4] == "hello"]
    (r,) = pdf.displayed(1, [A._r(w[:4])])
    assert pdf.sizes() == [[800.0, 600.0]]
    assert 0 <= r[0] < r[2] <= 800 and 0 <= r[1] < r[3] <= 600 and r[2] - r[0] < r[3] - r[1]  # tall: rotated
