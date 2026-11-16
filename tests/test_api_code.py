"""Live code checks (Phase 11b): the code stage after P0, its switch, limits, code sources, masking, titles.

Fake codecheck functions and no Modal: `CodeDeps` is injected. No Berget calls and no network (HTTP is mocked).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from onus.api.code_stage import (
    CodeDeps,
    CodeFetchError,
    RepoRef,
    download_repo,
    extract_zip,
    find_repo_links,
    parse_repo,
    rejoin_split_links,
)
from onus.api.limits import today
from onus.api.main import create_app
from onus.api.settings import ServingSettings
from onus.db import repo
from onus.db.session import session_scope
from onus.schemas.document import Document
from tests.test_api import FP, PDF, FakePipeline
from tests.test_api import env as _env  # noqa: F401

BERGET = "berget-secret-value-0123456789"
MODAL_ID = "ak-modaltokenid0123456789abcdef"
MODAL_SECRET = "as-modaltokensecret0123456789abcd"
SANDBOX_OPENAI = "sk-sandboxopenai0123456789abcdefgh"

# The code-availability sentence of arXiv 1706.03762 as the PDF text layer gives it: the link is broken after
# the host, so `repo_urls` alone finds no repository.
ATTENTION_TEXT = (
    "We are planning to extend the Transformer to problems involving input and output modalities other than "
    "text and to investigate local, restricted attention mechanisms to efficiently handle large inputs and "
    "outputs such as images, audio and video. Making generation less sequential is another research goals of "
    "ours.\nThe code we used to train and evaluate our models is available at https://github.com/\n"
    "tensorflow/tensor2tensor.\nAcknowledgements We are grateful to Nal Kalchbrenner and Stephan Gouws for "
    "their fruitful comments, corrections and inspiration."
)


@pytest.fixture
def env(_env, monkeypatch):  # noqa: F811
    for name in ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "HF_TOKEN", "LIVE_CODE_CHECKS"):
        monkeypatch.delenv(name, raising=False)
    _env["settings"] = _env["settings"].model_copy(update={
        "berget_api_key": _secret(BERGET), "modal_token_id": _secret(MODAL_ID),
        "modal_token_secret": _secret(MODAL_SECRET), "sandbox_openai_api_key": _secret(SANDBOX_OPENAI),
        "live_max_cost_eur_per_day": 10.0})
    (_env["tmp"] / "codetmp").mkdir()
    return _env


def _secret(v: str):
    from pydantic import SecretStr

    return SecretStr(v)


def code_zip(files: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in (files or {"proj/main.py": "print('acc', 0.91)\n", "proj/README.md": "python main.py\n"}).items():
            zf.writestr(name, text)
    return buf.getvalue()


class FakeLedger:
    def __init__(self):
        self.paper_eur: dict[str, float] = {}


class FakeLLM:
    def __init__(self):
        self.ledger = FakeLedger()


class FakeStatic:
    def __init__(self, status="done", reason=""):
        self.status, self.reason = status, reason


class FakeCode:
    """Fake codecheck (static, re-run, apply) and repository download. Gate `static` to hold the stage."""

    def __init__(self, *, text: str = "", static_fails: bool = False, rerun_minutes: float = 3.0,
                 rerun_output: str = "", download_fails: bool = False, static_eur: float = 0.02):
        self.text = text
        self.gate = threading.Event()
        self.gate.set()
        self.static_fails = static_fails
        self.rerun_minutes = rerun_minutes
        self.rerun_output = rerun_output
        self.download_fails = download_fails
        self.static_eur = static_eur
        self.calls: list[str] = []
        self.downloads: list[RepoRef] = []
        self.roots: list[Path] = []
        self.rerun_max: list[float] = []

    def deps(self) -> CodeDeps:
        return CodeDeps(ingest=self.ingest, llm=FakeLLM, static=self.static, rerun=self.rerun,
                        skip_rerun=self.skip_rerun, apply=self.apply, download=self.download)

    def ingest(self, pdf: Path, paper_id: str, source: str) -> Document:
        self.calls.append("ingest")
        return Document(paper_id=paper_id, source=source, pdf_sha256="0" * 64, title="t", pages=1, text=self.text)

    async def static(self, doc, llm):
        self.calls.append("static")
        self.roots.append(Path(doc.code.root))
        while not self.gate.is_set():
            await asyncio.sleep(0.01)
        llm.ledger.paper_eur[f"code:{doc.paper_id}"] = self.static_eur
        if self.static_fails:
            raise RuntimeError(f"static broke {BERGET}")
        return FakeStatic()

    async def rerun(self, doc, p0, llm, minutes):
        self.calls.append("rerun")
        self.rerun_max.append(minutes)
        rep = {"status": "runs", "reason": f"ran with OPENAI_API_KEY={SANDBOX_OPENAI}", "commands": [],
               "modifications": [], "stdout_tail": self.rerun_output, "extracted": [], "targets": [],
               "minutes": self.rerun_minutes}
        return rep, []

    def skip_rerun(self, doc, p0, reason):
        self.calls.append("skip")
        return {"status": "skipped", "reason": reason, "commands": [], "modifications": [], "minutes": 0.0}, []

    def apply(self, p0, doc, st, reruns, cmps):
        self.calls.append("apply")
        return {**p0, "code": {**p0["code"], "static": [{"kind": "paper_level", "status": "consistent",
                                                         "fact": "Paper level: fine"}],
                               "reruns": reruns, "code_version": "1.0.0+test"},
                "warnings": [*p0.get("warnings", []), "code: applied"]}

    def download(self, ref: RepoRef, dest: Path) -> Path:
        self.downloads.append(ref)
        if self.download_fails:
            raise CodeFetchError(f"The repository {ref.url} was not found (it may be private or deleted).")
        root = dest / "code"
        root.mkdir(parents=True)
        (root / "train.py").write_text("print('bleu', 28.4)\n")
        return root


def serving(**kw) -> ServingSettings:
    return ServingSettings(**({"live_code_checks": True} | kw))


def client(env, fake: FakeCode | None = None, *, pipeline=None, srv: ServingSettings | None = None,
           **kw) -> TestClient:
    def fetcher(source: str, ext: str, max_mb: int) -> bytes:
        return PDF + ext.encode()

    app = create_app(env["settings"], analyzer=pipeline or FakePipeline(), fetcher=fetcher, fingerprint=FP,
                     frontend_dist=env["tmp"] / "dist", eval_log=env["tmp"] / "no_eval_log.md",
                     serving=srv or serving(), code_deps=(fake or FakeCode()).deps(), code_tmp=env["tmp"] / "codetmp",
                     **kw)
    return TestClient(app)


TERMINAL = ("done", "failed", "skipped", "no_code")


def wait_code(c: TestClient, aid: str, timeout: float = 5.0) -> dict:
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        st = c.get(f"/api/analyses/{aid}/status").json()
        if st["status"] == "failed":
            raise AssertionError(st)
        if st["status"] == "done":
            r = c.get(f"/api/analyses/{aid}").json()
            cs = r.get("code_stage") or {}
            if cs.get("status") in TERMINAL:
                return r
        time.sleep(0.02)
    raise AssertionError(f"code stage of {aid} did not finish")


def wait_p0(c: TestClient, aid: str, timeout: float = 5.0) -> dict:
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if c.get(f"/api/analyses/{aid}/status").json()["status"] == "done":
            return c.get(f"/api/analyses/{aid}").json()
        time.sleep(0.02)
    raise AssertionError(f"P0 of {aid} did not finish")


def start_ref(c: TestClient, ref: str, code: bytes | None = None, ip: str = "1.2.3.4") -> dict:
    h = {"x-forwarded-for": ip}
    if code is None:
        r = c.post("/api/analyses", json={"ref": ref}, headers=h)
    else:
        r = c.post("/api/analyses", data={"ref": ref}, files={"code": ("code.zip", code, "application/zip")},
                   headers=h)
    assert r.status_code in (200, 202), r.text
    return r.json()


def start_pdf(c: TestClient, pdf: bytes, code: bytes | None = None, ip: str = "1.2.3.4"):
    files = {"file": ("p.pdf", pdf, "application/pdf")}
    if code is not None:
        files["code"] = ("code.zip", code, "application/zip")
    return c.post("/api/analyses", files=files, headers={"x-forwarded-for": ip})


# ---------------------------------------------------------------- the switch


def test_switch_off_by_default_and_needs_modal_tokens(env):
    with client(env, srv=ServingSettings()) as c:  # LIVE_CODE_CHECKS unset
        m = c.get("/api/meta").json()
        assert m["live"]["code_checks"] is False and m["live"]["code"] is None
        aid = start_pdf(c, PDF + b"off", code_zip()).json()["analysis_id"]  # a .zip is ignored when off
        r = wait_p0(c, aid)
        assert r["code_stage"] is None and r["code"]["available"] is False
        assert not list((env["tmp"] / "codetmp").iterdir())  # the ignored upload was never kept
        with session_scope(c.app.state.ctx.factory) as s:
            assert "+code-" not in repo.get_paper(s, r["analysis"]["paper_id"]).external_id
    no_tokens = env["settings"].model_copy(update={"modal_token_id": _secret(""), "modal_token_secret": _secret("")})
    with client({**env, "settings": no_tokens}) as c:  # switch on, Modal tokens missing: still off
        assert c.get("/api/meta").json()["live"]["code_checks"] is False


def test_switch_on_reports_limits(env):
    with client(env, srv=serving(live_max_code_zip_mb=7, live_max_reruns_per_day=4)) as c:
        live = c.get("/api/meta").json()["live"]
        assert live["code_checks"] is True
        assert live["code"]["max_zip_mb"] == 7 and live["code"]["max_reruns_per_day"] == 4
        assert live["code"]["max_reruns_per_ip_per_day"] == 2


# ---------------------------------------------------------------- P0 first, then the code stage


def test_p0_is_published_before_the_code_stage(env):
    fake = FakeCode()
    fake.gate.clear()
    with client(env, fake) as c:
        aid = start_ref(c, "arxiv:2401.00001", code_zip())["analysis_id"]
        r = wait_p0(c, aid)
        # P0 is readable while the code stage is still waiting or running
        assert r["claims"] and r["code"]["available"] is False
        assert r["code_stage"]["status"] in ("queued", "running")
        fake.gate.set()
        r = wait_code(c, aid)
        assert r["code_stage"]["status"] == "done"
        assert r["code"]["available"] is True and r["code"]["code_version"] == "1.0.0+test"
        assert r["code_stage"]["source"]["kind"] == "upload"
        assert fake.calls == ["ingest", "static", "rerun", "apply"]
        assert r["claims"] == wait_p0(c, aid)["claims"]
    assert not list((env["tmp"] / "codetmp").iterdir()), "the temporary code folder must be deleted"
    assert not fake.roots[0].exists()


def test_code_queue_does_not_block_p0(env):
    fake = FakeCode()
    fake.gate.clear()
    with client(env, fake) as c:
        first = start_ref(c, "arxiv:2401.00001", code_zip())["analysis_id"]
        wait_p0(c, first)
        second = start_ref(c, "arxiv:2401.00002", code_zip({"m.py": "print(1)\n"}))["analysis_id"]
        r2 = wait_p0(c, second)  # the code stage of the first paper is still held
        assert r2["code_stage"]["status"] == "queued"
        assert c.get(f"/api/analyses/{first}").json()["code_stage"]["status"] == "running"
        fake.gate.set()
        assert wait_code(c, first)["code_stage"]["status"] == "done"
        assert wait_code(c, second)["code_stage"]["status"] == "done"


def test_failed_code_stage_leaves_p0_intact(env, caplog):
    fake = FakeCode(static_fails=True)
    with client(env, fake) as c:
        aid = start_ref(c, "arxiv:2401.00003", code_zip())["analysis_id"]
        before = wait_p0(c, aid)
        r = wait_code(c, aid)
        assert r["code_stage"]["status"] == "failed"
        assert "report above is complete" in r["code_stage"]["message"]
        assert BERGET not in json.dumps(r)
        for k in ("claims", "summary", "code", "checklist", "cost"):
            assert r[k] == before[k]
        assert c.get(f"/api/analyses/{aid}/status").json()["status"] == "done"
        with session_scope(c.app.state.ctx.factory) as s:  # the stage's spend still counts toward the day
            assert repo.get_live_usage(s, today()).cost_eur == pytest.approx(0.05 + 0.02)
    # a download that fails is a message, not a crash
    fake = FakeCode(text=ATTENTION_TEXT, download_fails=True)
    with client(env, fake) as c:
        aid = start_ref(c, "arxiv:1706.03762")["analysis_id"]
        r = wait_code(c, aid)
        assert r["code_stage"]["status"] == "failed" and "was not found" in r["code_stage"]["message"]
        assert r["code"]["available"] is False


# ---------------------------------------------------------------- where the code comes from


def test_repository_link_split_by_a_line_break_is_used(env):
    fake = FakeCode(text=ATTENTION_TEXT)
    with client(env, fake) as c:
        aid = start_ref(c, "arxiv:1706.03762")["analysis_id"]
        r = wait_code(c, aid)
        assert fake.downloads == [RepoRef("github", "tensorflow/tensor2tensor")]
        src = r["code_stage"]["source"]
        assert src == {"kind": "repo", "url": "https://github.com/tensorflow/tensor2tensor", "host": "github",
                       "fetched_at": today().isoformat()}
        assert r["code"]["available"] is True and r["code"]["files"] == 1


def test_uploaded_code_wins_over_the_link(env):
    fake = FakeCode(text=ATTENTION_TEXT)
    with client(env, fake) as c:
        aid = start_ref(c, "arxiv:1706.03762", code_zip())["analysis_id"]
        assert wait_code(c, aid)["code_stage"]["source"]["kind"] == "upload"
        assert fake.downloads == []


def test_no_code_and_unsupported_host(env):
    fake = FakeCode(text="Code: https://bitbucket.org/someone/thing and nothing else.")
    with client(env, fake) as c:
        aid = start_ref(c, "arxiv:2401.00004")["analysis_id"]
        r = wait_code(c, aid)
        assert r["code_stage"]["status"] == "no_code"
        assert r["code_stage"]["message"] == "Repository host not supported: https://bitbucket.org/someone/thing"
        assert r["code"]["available"] is False and "static" not in fake.calls
    fake = FakeCode(text="No links here.")
    with client(env, fake) as c:
        aid = start_ref(c, "arxiv:2401.00005")["analysis_id"]
        r = wait_code(c, aid)
        assert r["code_stage"]["status"] == "no_code" and "message" not in r["code_stage"]


def test_cache_key_includes_the_code_hash(env):
    with client(env) as c:
        plain = start_pdf(c, PDF + b"cache")
        a0 = plain.json()["analysis_id"]
        wait_code(c, a0)
        z = code_zip()  # built once: a zip's bytes carry timestamps
        with_code = start_pdf(c, PDF + b"cache", z).json()
        assert with_code["analysis_id"] != a0 and with_code["cached"] is False  # not served from the plain one
        wait_code(c, with_code["analysis_id"])
        again = start_pdf(c, PDF + b"cache", z).json()
        assert again == {"analysis_id": with_code["analysis_id"], "status": "done", "cached": True}
        other = start_pdf(c, PDF + b"cache", code_zip({"x.py": "print(2)\n"})).json()
        assert other["analysis_id"] not in (a0, with_code["analysis_id"])
        wait_code(c, other["analysis_id"])
        assert start_pdf(c, PDF + b"cache").json() == {"analysis_id": a0, "status": "done", "cached": True}
        # the same for an arXiv id
        r1 = start_ref(c, "arxiv:2401.00006", ip="5.5.5.5")  # another visitor: 3 analyses per hour each
        wait_code(c, r1["analysis_id"])
        r2 = start_ref(c, "arxiv:2401.00006", code_zip(), ip="5.5.5.5")
        assert r2["analysis_id"] != r1["analysis_id"]
        wait_code(c, r2["analysis_id"])
        assert start_ref(c, "arxiv:2401.00006")["analysis_id"] == r1["analysis_id"]
        a = c.get(f"/api/analyses/{r2['analysis_id']}").json()["analysis"]
        assert a["source_url"] == "https://arxiv.org/abs/2401.00006"  # the code key is not part of the link


def test_bad_code_uploads(env):
    with client(env, srv=serving(live_max_code_zip_mb=1)) as c:
        r = start_pdf(c, PDF + b"z", b"not a zip at all")
        assert r.status_code == 400 and "must be a .zip" in r.json()["detail"]
        big = code_zip({"big.py": "x" * (2 * 1024 * 1024)})
        big = b"PK\x03\x04" + b"\0" * (1024 * 1024 + 10) if len(big) <= 1024 * 1024 else big
        r = start_pdf(c, PDF + b"z", big)
        assert r.status_code == 413 and "larger than 1 MB" in r.json()["detail"]
        r = c.post("/api/analyses", data={"ref": "not a ref"}, files={"code": ("c.zip", code_zip(), "application/zip")})
        assert r.status_code == 422
        assert c.post("/api/analyses", json={"ref": "arxiv:2401.00007"}).status_code == 202  # JSON still works


def test_extract_and_download_with_caps(tmp_path):
    z = tmp_path / "in" / "code.zip"
    z.parent.mkdir()
    z.write_bytes(code_zip())
    root = extract_zip(z, tmp_path / "out")
    assert root.name == "proj" and (root / "main.py").is_file() and not z.exists()
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../evil.py", "x")
    with pytest.raises(CodeFetchError, match="could not be extracted safely"):
        extract_zip(bad, tmp_path / "bad_out")

    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if "tensor2tensor" in str(req.url):
            return httpx.Response(200, content=code_zip({"tensor2tensor-abc/t2t/train.py": "print(1)\n"}))
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as cl:
        root = download_repo(RepoRef("github", "tensorflow/tensor2tensor"), tmp_path / "gh", client=cl)
        assert (root / "t2t" / "train.py").is_file()
        assert seen == ["https://github.com/tensorflow/tensor2tensor/archive/HEAD.zip"]
        with pytest.raises(CodeFetchError, match="was not found"):
            download_repo(RepoRef("gitlab", "group/sub/proj"), tmp_path / "gl", client=cl)
        assert seen[-1] == "https://gitlab.com/api/v4/projects/group%2Fsub%2Fproj/repository/archive.zip"


# ---------------------------------------------------------------- limits


def test_rerun_limits_skip_the_rerun_but_not_the_static_check(env):
    fake = FakeCode()
    with client(env, fake, srv=serving(live_max_reruns_per_day=1)) as c:
        a = start_ref(c, "arxiv:2401.00010", code_zip(), ip="10.0.0.1")["analysis_id"]
        assert wait_code(c, a)["code_stage"]["rerun"]["attempted"] is True
        b = start_ref(c, "arxiv:2401.00011", code_zip(), ip="10.0.0.2")["analysis_id"]
        r = wait_code(c, b)
        assert r["code_stage"]["status"] == "done" and r["code_stage"]["static"]["status"] == "done"
        assert r["code_stage"]["rerun"]["attempted"] is False
        assert "daily limit of 1 code re-runs" in r["code_stage"]["rerun"]["skipped"]
        assert r["code"]["reruns"][0]["status"] == "skipped"
        assert fake.calls.count("rerun") == 1 and fake.calls.count("static") == 2


def test_rerun_limit_per_visitor(env):
    fake = FakeCode()
    with client(env, fake, srv=serving(live_max_reruns_per_ip_per_day=1)) as c:
        a = start_ref(c, "arxiv:2401.00012", code_zip(), ip="10.0.0.3")["analysis_id"]
        wait_code(c, a)
        b = start_ref(c, "arxiv:2401.00013", code_zip(), ip="10.0.0.3")["analysis_id"]
        assert "per visitor" in wait_code(c, b)["code_stage"]["rerun"]["skipped"]
        other = start_ref(c, "arxiv:2401.00014", code_zip(), ip="10.0.0.4")["analysis_id"]
        assert wait_code(c, other)["code_stage"]["rerun"]["attempted"] is True


def test_modal_minutes_per_day(env):
    fake = FakeCode(rerun_minutes=4.0)
    settings = env["settings"].model_copy(update={"onus_rerun_max_minutes": 30})
    with client({**env, "settings": settings}, fake, srv=serving(live_max_modal_minutes_per_day=10)) as c:
        a = start_ref(c, "arxiv:2401.00015", code_zip(), ip="10.0.0.5")["analysis_id"]
        wait_code(c, a)
        assert fake.rerun_max == [10.0]  # min(ONUS_RERUN_MAX_MINUTES, what is left today)
        b = start_ref(c, "arxiv:2401.00016", code_zip(), ip="10.0.0.6")["analysis_id"]
        wait_code(c, b)
        assert fake.rerun_max == [10.0, 6.0]
        d = start_ref(c, "arxiv:2401.00017", code_zip(), ip="10.0.0.7")["analysis_id"]
        assert "sandbox time" in wait_code(c, d)["code_stage"]["rerun"]["skipped"]  # 2 minutes left < 5


def test_daily_cost_cap_skips_the_code_stage(env):
    settings = env["settings"].model_copy(update={"live_max_cost_eur_per_day": 0.05})  # one P0 (€0.05) uses it up
    fake = FakeCode()
    with client({**env, "settings": settings}, fake) as c:
        a = start_ref(c, "arxiv:2401.00018", code_zip())["analysis_id"]
        r = wait_code(c, a)
        assert r["code_stage"]["status"] == "skipped" and "budget" in r["code_stage"]["message"]
        assert fake.calls == [] and r["code"]["available"] is False
        r = c.post("/api/analyses", json={"ref": "arxiv:2401.00019"})
        assert r.status_code == 429  # and the P0 limit sees the same day budget


def test_code_spend_counts_toward_the_daily_cost(env):
    fake = FakeCode(static_eur=0.11)
    with client(env, fake) as c:
        aid = start_ref(c, "arxiv:2401.00020", code_zip())["analysis_id"]
        wait_code(c, aid)
        with session_scope(c.app.state.ctx.factory) as s:
            assert repo.get_live_usage(s, today()).cost_eur == pytest.approx(0.05 + 0.11)
            assert repo.get_analysis(s, aid).cost_eur == pytest.approx(0.05 + 0.11)


def test_restart_marks_unfinished_code_stage_failed(env):
    fake = FakeCode()
    fake.gate.clear()
    with client(env, fake) as c:
        aid = start_ref(c, "arxiv:2401.00021", code_zip())["analysis_id"]
        wait_p0(c, aid)
    fake.gate.set()
    with client(env, FakeCode()) as c:
        r = c.get(f"/api/analyses/{aid}").json()
        assert r["code_stage"]["status"] == "failed" and "restarted" in r["code_stage"]["message"]
        assert r["claims"]


# ---------------------------------------------------------------- secrets


def test_stored_report_and_logs_hold_no_secret(env, caplog):
    out = f"loss 0.1\nBERGET_API_KEY={BERGET}\nmodal {MODAL_ID} {MODAL_SECRET}\nAuthorization: Bearer {SANDBOX_OPENAI}"
    fake = FakeCode(rerun_output=out)
    caplog.set_level(logging.DEBUG)
    with client(env, fake) as c:
        aid = start_ref(c, "arxiv:2401.00022", code_zip())["analysis_id"]
        r = wait_code(c, aid)
        assert r["code_stage"]["status"] == "done"
        with session_scope(c.app.state.ctx.factory) as s:
            a = repo.get_analysis(s, aid)
            stored = json.dumps({"result": a.result, "progress": a.progress, "summary": a.summary, "error": a.error})
        shown = json.dumps(r) + json.dumps(c.get(f"/api/analyses/{aid}/status").json())
    for v in (BERGET, MODAL_ID, MODAL_SECRET, SANDBOX_OPENAI):
        assert v not in stored and v not in shown and v not in caplog.text
    assert "loss 0.1" in stored and "***" in stored  # masked, not dropped


# ---------------------------------------------------------------- links and titles


def test_rejoin_split_links_on_the_attention_paper_text():
    assert "https://github.com/tensorflow/tensor2tensor" in rejoin_split_links(ATTENTION_TEXT)
    from onus.ingest.code_index import repo_urls

    assert repo_urls(ATTENTION_TEXT) == []  # what the pipeline sees
    repos, other = find_repo_links(ATTENTION_TEXT)
    assert repos == [RepoRef("github", "tensorflow/tensor2tensor")] and other == []
    # the break after the owner, and in a Hugging Face link
    assert find_repo_links("see https://github.com/tensorflow/\ntensor2tensor.")[0] == \
        [RepoRef("github", "tensorflow/tensor2tensor")]
    assert find_repo_links("at https://huggingface.co/datasets/ org/data-set here")[0] == \
        [RepoRef("huggingface", "datasets/org/data-set")]
    # prose after a bare host is not glued on
    assert find_repo_links("hosted on https://github.com/ and elsewhere")[0] == []


def test_parse_repo():
    assert parse_repo("https://github.com/a-b/c.d.git") == RepoRef("github", "a-b/c.d")
    assert parse_repo("https://github.com/a/b/tree/main/src") == RepoRef("github", "a/b")
    assert parse_repo("https://github.com/orgs/x") is None
    assert parse_repo("https://github.com/onlyowner") is None
    assert parse_repo("https://gitlab.com/g/s/p/-/tree/main") == RepoRef("gitlab", "g/s/p")
    assert parse_repo("https://huggingface.co/spaces/u/demo") == RepoRef("huggingface", "spaces/u/demo")
    assert parse_repo("https://huggingface.co/papers/2401.00001") is None
    assert parse_repo("https://bitbucket.org/a/b") is None
    assert RepoRef("huggingface", "datasets/o/n").url == "https://huggingface.co/datasets/o/n"


def test_display_title_from_metadata(env):
    titles = {"1706.03762": "Attention Is All You Need"}
    with client(env, title_fetcher=lambda src, ext: titles.get(ext)) as c:
        aid = start_ref(c, "arxiv:1706.03762")["analysis_id"]
        assert wait_p0(c, aid)["analysis"]["title"] == "Attention Is All You Need"

    def broken(src, ext):
        raise RuntimeError("metadata down")

    with client(env, title_fetcher=broken) as c:
        aid = start_ref(c, "arxiv:2401.00030")["analysis_id"]
        assert wait_p0(c, aid)["analysis"]["title"] == "Live paper"  # the parsed title is the fallback


def test_fetch_title_arxiv_and_openreview():
    from onus.ingest.fetch import PaperRef, fetch_title

    atom = (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Attention Is All\n'
            b'  You Need</title></entry></feed>')

    def handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if u == "https://export.arxiv.org/api/query?id_list=1706.03762":
            return httpx.Response(200, content=atom)
        if u == "https://api2.openreview.net/notes?id=abcDEF123":
            return httpx.Response(200, json={"notes": [{"content": {"title": {"value": "An OpenReview Title"},
                                                                    "venue": {"value": "ignored"}}}]})
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as cl:
        assert fetch_title(PaperRef("arxiv", "1706.03762"), client=cl) == "Attention Is All You Need"
        assert fetch_title(PaperRef("openreview", "abcDEF123"), client=cl) == "An OpenReview Title"
        assert fetch_title(PaperRef("arxiv", "2401.99999"), client=cl) is None
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<not xml"))) as cl:
        assert fetch_title(PaperRef("arxiv", "1706.03762"), client=cl) is None
