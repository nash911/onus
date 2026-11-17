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


# ---------------------------------------------------------------- Phase 14: table cells


def _first_table(dev):
    return next(t for t in dev["doc"].tables if t.parsed and t.rows)


def _locatable_cell(dev) -> tuple[str, str]:
    """(where, raw) of the first numeric body cell (reading order) the locator finds; chosen by rule."""
    from onus.checks.tables import TableView

    t = _first_table(dev)
    v = TableView(t)
    pdf = A.Pdf.open(dev["pdf"])
    try:
        for i, j, c in v.cells():
            if c.value is not None and A.locate_cells(pdf, t, [v.where(i, j)]):
                return v.where(i, j), c.raw
    finally:
        pdf.close()
    raise AssertionError("no locatable numeric cell")


def _number_check(cells: list[str]) -> dict:
    return {"check": "number", "outcome": "pass", "detail": "d",
            "data": {"numbers": [{"cells": cells, "tables": sorted({c.split(",")[0] for c in cells}),
                                  "status": "pass"}]}}


def _with_cells(dev, cells: list[str]) -> dict:
    r = build_result(dev)
    r["claims"][0]["checks"] = [_number_check(cells)]
    RunResult.model_validate(r)
    return r


def test_one_checked_cell_gets_one_table_cell_rect(dev):
    from onus.eval.anchors_eval import words_in

    where, raw = _locatable_cell(dev)
    t = _first_table(dev)
    an = A.compute_anchors(dev["doc"], dev["pdf"], _with_cells(dev, [where]))
    (it,) = an["items"]["c_line:ev1"]
    assert it["method"] == "table_cell" and it["page"] == t.page and len(it["rects"]) == 1
    (r,) = it["rects"]
    x0, y0, x1, y1 = t.bbox
    assert x0 - 1 <= r[0] < r[2] <= x1 + 1 and y0 - 1 <= r[1] < r[3] <= y1 + 1
    pdf = A.Pdf.open(dev["pdf"])
    try:
        assert words_in(pdf, [it]) == A.words_of(raw)
    finally:
        pdf.close()


def test_no_unknown_or_ambiguous_cell_falls_back_to_the_table_box(dev):
    from onus.checks.tables import TableView

    t = _first_table(dev)
    box = [A._r(t.bbox)]
    where, _ = _locatable_cell(dev)
    pdf = A.Pdf.open(dev["pdf"])
    try:
        assert A.by_table(dev["doc"], pdf, "table", t.id) == [{"page": t.page, "rects": box, "method": "table"}]
        unknown = f"{t.id}, row 'no such row', col 'no such col'"
        for cells in ([unknown], [where, unknown], ["Table 999, row 'a', col 'b'"]):
            got = A.by_table(dev["doc"], pdf, "table", t.id, cells)
            assert got[0]["method"] == "table" and got[0]["rects"] == box, cells
        # duplicated row labels make the reference ambiguous: the table box, never a guess
        v = TableView(t)
        i = next(i for i, j, c in v.cells() if v.where(i, j) == where)
        h = len(t.rows) - len(v.body)
        dup = t.model_copy(update={"rows": [*t.rows, t.rows[h + i]]})
        assert A.locate_cells(pdf, dup, [where]) is None
        assert A.locate_cells(pdf, t, [where]) is not None
    finally:
        pdf.close()


def test_located_cells_keep_the_table_geometry(dev):
    """Every cell the locator places sits left of the cells to its right and above the cells below it."""
    from onus.checks.tables import TableView

    t = _first_table(dev)
    v = TableView(t)
    pdf = A.Pdf.open(dev["pdf"])
    try:
        got = {}
        for i, j, c in v.cells():
            if c.raw.strip() and (found := A.locate_cells(pdf, t, [v.where(i, j)])):
                got[(i, j)] = found[0][1]
    finally:
        pdf.close()
    assert len(got) >= 5
    cx = {k: (r[0] + r[2]) / 2 for k, r in got.items()}
    cy = {k: (r[1] + r[3]) / 2 for k, r in got.items()}
    for (i, j), (k, m) in itertools.permutations(got, 2):
        if i == k and j < m:
            assert cx[(i, j)] < cx[(k, m)]
        if j == m and i < k:
            assert cy[(i, j)] < cy[(k, m)] + 0.5


def test_claim_cells_reads_any_cells_list():
    c = {"checks": [_number_check(["Table 1, row 'a', col 'b'"]),
                    {"check": "dominance", "data": {"x": [{"cells": ["Table 2, row 'c', col 'd'"]}]}}]}
    assert A.claim_cells(c) == ["Table 1, row 'a', col 'b'", "Table 2, row 'c', col 'd'"]


# ---------------------------------------------------------------- Phase 14: checklist evidence


def _with_checklist(dev) -> dict:
    r = build_result(dev)
    text_ev = dict(r["claims"][1]["evidence"][0], id="ck-1")
    code_ev = {"id": "ck-2", "kind": "code", "label": "src/x.py:1", "snippet": "x = 1", "location": None,
               "link_method": "code_search", "relation": "supports"}
    r["checklist"] = [{"key": "claims", "title": "Claims", "answer": "Yes", "justification": "j", "status": "backed",
                       "reason": "r", "evidence": [text_ev, code_ev]}]
    RunResult.model_validate(r)
    return r


def test_checklist_evidence_gets_checklist_keys(dev):
    an = A.compute_anchors(dev["doc"], dev["pdf"], _with_checklist(dev))
    (it,) = an["items"]["checklist:claims:ck-1"]
    assert it["method"] == "offsets"
    assert it == an["items"]["c_two:ev2"][0]  # the same passage as the claim evidence it copies
    assert "checklist:claims:ck-2" not in an["items"] and "checklist:claims:ck-2" not in an["missing"]


# ---------------------------------------------------------------- Phase 14: precompute and cache validity


def test_digest_covers_anchor_inputs_only(dev):
    r = _with_checklist(dev)
    d = A.result_digest(r)
    assert A.result_digest(json.loads(json.dumps(r))) == d
    same = json.loads(json.dumps(r))
    same["claims"][0]["reason"], same["created_at"] = "another reason", "2030-01-01T00:00:00Z"
    assert A.result_digest(same) == d  # not an anchor input
    for change in (lambda x: x["claims"][0].__setitem__("quote", "other words"),
                   lambda x: x["claims"][0]["evidence"][0].__setitem__("label", "Table 9"),
                   lambda x: x["claims"][0].__setitem__("checks", [_number_check(["Table 1, row 'a', col 'b'"])]),
                   lambda x: x["checklist"][0]["evidence"][0].__setitem__("snippet", "new snippet")):
        x = json.loads(json.dumps(r))
        change(x)
        assert A.result_digest(x) != d


class DevPipeline:
    """A fake live analyzer that returns the dev fixture's result for the uploaded dev PDF (no LLM calls)."""

    def __init__(self, dev):
        self.dev = dev

    async def __call__(self, pdf_path: Path, paper_id: str, source: str, progress) -> dict:
        from onus.ingest.document_builder import save_document

        progress("ingest", {})
        var = pdf_path.parent.parent  # $ONUS_VAR_DIR/uploads/<sha>.pdf
        save_document(self.dev["doc"].model_copy(update={"paper_id": paper_id}), var)  # as ingest would
        r = _with_checklist(self.dev)
        r["mode"], r["paper"]["id"], r["paper"]["source"] = "live", paper_id, source
        return r


def _wait_for(pred, timeout: float = 60.0) -> bool:
    import time

    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def live(dev, tmp_path: Path):
    settings = Settings(_env_file=None, onus_var_dir=tmp_path / "var", database_url=f"sqlite:///{tmp_path}/t.db",
                        onus_results_import_dir=tmp_path / "none", berget_api_key="test-key-not-real",
                        live_max_pdf_mb=50, featured_papers="")
    return {"settings": settings, "tmp": tmp_path, "dev": dev}


def _live_client(live, analyzer) -> TestClient:
    app = create_app(live["settings"], analyzer=analyzer, fetcher=lambda *a: b"", fingerprint=FP,
                     frontend_dist=live["tmp"] / "dist", eval_log=live["tmp"] / "none.md")
    return TestClient(app)


def _upload(c: TestClient, dev) -> str:
    from tests.test_api import wait_done

    r = c.post("/api/analyses", files={"file": ("p.pdf", dev["pdf"].read_bytes(), "application/pdf")})
    assert r.status_code in (200, 202), r.text
    aid = r.json()["analysis_id"]
    assert wait_done(c, aid, timeout=30)["status"] == "done"
    return aid


def test_live_job_precomputes_anchors_and_cache_stays_valid(live, monkeypatch):
    from onus.db import repo
    from onus.db.session import session_scope

    dev, var = live["dev"], live["settings"].var_dir
    with _live_client(live, DevPipeline(dev)) as c:
        aid = _upload(c, dev)
        cache = A.cache_path(var, aid)
        assert _wait_for(cache.is_file), "anchors were not precomputed after the job finished"
        stamp = json.loads(cache.read_text())["stamp"]
        assert stamp["version"] == A.ANCHORS_VERSION and stamp["pdf_sha256"] == dev["sha"]

        def boom(*a, **k):
            raise AssertionError("recomputed instead of reading the cache")

        monkeypatch.setattr(A, "compute_anchors", boom)
        body = c.get(f"/api/analyses/{aid}/anchors").json()  # the first open is a cache hit
        assert "checklist:claims:ck-1" in body["items"] and "c_line" in body["items"]
        monkeypatch.undo()

        # a changed result (as the code stage may save) gets new anchors
        ctx = c.app.state.ctx
        with session_scope(ctx.factory) as s:
            a = repo.get_analysis(s, aid)
            res = json.loads(json.dumps(a.result))
            res["checklist"] = []
            a.result = res
        body2 = c.get(f"/api/analyses/{aid}/anchors").json()
        assert "checklist:claims:ck-1" not in body2["items"]
        assert json.loads(cache.read_text())["stamp"]["result_digest"] == A.result_digest(res)

        # an ANCHORS_VERSION bump makes the cache stale
        monkeypatch.setattr(A, "ANCHORS_VERSION", A.ANCHORS_VERSION + 1)
        c.get(f"/api/analyses/{aid}/anchors")
        assert json.loads(cache.read_text())["stamp"]["version"] == A.ANCHORS_VERSION


def test_precompute_failure_does_not_touch_the_job(live, monkeypatch):
    from onus.api import anchors_warm

    def broken(*a, **k):
        raise RuntimeError("anchors broke")

    monkeypatch.setattr(anchors_warm, "anchors_for", broken)
    dev = live["dev"]
    with _live_client(live, DevPipeline(dev)) as c:
        aid = _upload(c, dev)
        warmer = c.app.state.ctx.runner.anchors
        assert warmer.warm(aid) is False  # logged and swallowed
        st = c.get(f"/api/analyses/{aid}/status").json()
        assert st["status"] == "done" and st["progress"]["stage"] == "done"
        assert not A.cache_path(live["settings"].var_dir, aid).exists()
        monkeypatch.undo()
        assert c.get(f"/api/analyses/{aid}/anchors").json()["pdf"] is True  # the request path still computes


def test_startup_warms_stale_caches(api):
    """The api fixture's bundle analysis has a stored PDF and no cache: startup warms it in the background."""
    var = api["settings"].var_dir
    assert _wait_for(lambda: A.cache_path(var, api["with"]).is_file())
    warmer = api["c"].app.state.ctx.runner.anchors
    assert _wait_for(lambda: warmer.stale() == [])


def test_live_analyzer_caches_the_document(dev, tmp_path, monkeypatch):
    """The live analyzer saves the ingested Document, so anchors never re-ingest the PDF (no LLM call: mocked)."""
    import asyncio

    import onus.ingest.document_builder as db
    from onus import pipeline
    from onus.api.jobs import make_pipeline_analyzer
    from onus.ingest.document_builder import doc_path

    monkeypatch.setattr(db, "ingest_pdf", lambda pdf, var, source, paper_id: dev["doc"].model_copy(
        update={"paper_id": paper_id}))

    class Done:
        def model_dump_json(self) -> str:
            return "{}"

    async def fake_runs(doc, runs, mode, **kw):
        assert kw["ingest_s"] >= 0
        return Done()

    monkeypatch.setattr(pipeline, "analyze_doc_runs", fake_runs)
    s = Settings(_env_file=None, onus_var_dir=tmp_path / "var", database_url=f"sqlite:///{tmp_path}/t.db",
                 berget_api_key="test-key-not-real")
    analyze = make_pipeline_analyzer(s, FP)
    asyncio.run(analyze(dev["pdf"], "upload:abc123", "upload", lambda *a: None))
    p = doc_path(s.var_dir, "upload:abc123")
    assert p.is_file() and db.load_document(p).pdf_sha256 == dev["doc"].pdf_sha256
