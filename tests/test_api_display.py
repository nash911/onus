"""Phase 11c: display repairs on stored results (api/serialize.py). The stored result is never changed.

Fixture: tests/fixtures/live_2611.10459.json, trimmed from the production result for arXiv 2611.10459 (a public
paper). The paper prints "https://github.com/XiaoyuChenPub/ ROLS"; the frozen pipeline kept only the owner part.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from onus.api.serialize import repair_repo_links
from onus.db import repo
from onus.db.session import session_scope
from tests.test_api import FP
from tests.test_api import env as _env  # noqa: F401
from tests.test_api_code import (
    client,
    env,  # noqa: F401
)

FIXTURE = Path(__file__).parent / "fixtures" / "live_2611.10459.json"
CUT = "https://github.com/XiaoyuChenPub/"
FULL = "https://github.com/XiaoyuChenPub/ROLS"


def load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def shown(result: dict) -> list[str]:
    """Every reason, check detail and evidence label/snippet the report shows."""
    out: list[str] = []
    for item in result["claims"] + result["checklist"]:
        out += [item.get("reason") or ""]
        out += [c.get("detail") or "" for c in item.get("checks") or []]
        for e in item.get("evidence") or []:
            out += [e.get("label") or "", e.get("snippet") or ""]
    return [t for t in out if "XiaoyuChenPub" in t]


def test_fixture_has_the_cut_link():
    texts = shown(load())
    assert texts and all(FULL not in t for t in texts)
    assert "Supported by https://github.com/XiaoyuChenPub/." in texts


def test_cut_link_shows_the_code_stage_url():
    d = load()
    before = copy.deepcopy(d)
    out = repair_repo_links(d, d["code_stage"])
    texts = shown(out)
    assert texts and all(FULL in t for t in texts)
    claim = next(c for c in out["claims"] if c["type"] == "availability" and c["reason"].startswith("Supported"))
    assert claim["reason"] == "Supported by https://github.com/XiaoyuChenPub/ROLS."
    row = next(c for c in out["checklist"] if c["key"] == "reproducibility")
    assert row["reason"] == "The paper links a code repository (https://github.com/XiaoyuChenPub/ROLS)."
    # the paper's own quotes stay verbatim, and the stored result is not touched
    assert claim["quote"] == "The source code is available at https://github.com/XiaoyuChenPub/ ROLS."
    assert d == before


def test_without_a_code_stage_the_paper_quote_is_rejoined():
    out = repair_repo_links(load(), None)
    assert all(FULL in t for t in shown(out))


def test_code_stage_url_must_extend_the_cut_link():
    d = load()
    other = {"source": {"kind": "repo", "url": "https://github.com/someone/else"}}
    assert all(FULL in t for t in shown(repair_repo_links(d, other)))  # falls back to the quote text
    # no quote gives the rest of the link either: the text stays as stored
    for c in d["claims"]:
        c["quote"] = "The code is online."
    for c in d["checklist"]:
        c["justification"] = "Yes."
    assert repair_repo_links(d, other) is d


def test_full_links_and_unrelated_results_are_left_alone():
    r = {"claims": [{"quote": "q", "reason": f"Supported by {FULL}.", "evidence": [{"label": FULL}]}],
         "checklist": []}
    assert repair_repo_links(r, None) is r


def test_get_analysis_shows_the_full_link(env):  # noqa: F811
    d = load()
    stored = {k: v for k, v in d.items() if k not in ("code_stage", "_note")}
    with client(env) as c:
        with session_scope(c.app.state.ctx.factory) as s:
            p = repo.upsert_paper(s, source="arxiv", external_id="2611.10459", title=d["paper"]["title"])
            a = repo.create_analysis(s, paper_id=p.id, onus_version="1.0.0", fingerprint=FP, mode="live")
            repo.store_result(s, a, stored)
            a.progress = {"code": d["code_stage"]}
            aid = a.id
        body = c.get(f"/api/analyses/{aid}").json()
        assert all(FULL in t for t in shown(body))
        assert body["code_stage"]["source"]["url"] == FULL
        with session_scope(c.app.state.ctx.factory) as s:  # stored as it was
            assert shown(repo.get_analysis(s, aid).result) == shown(stored)
