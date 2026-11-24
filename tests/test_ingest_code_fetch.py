"""Safe unzip, code index and live-mode PDF fetch (mocked HTTP only)."""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import httpx
import pytest

from onus.ingest import code_index as CI
from onus.ingest import fetch as F

# ---------------------------------------------------------------- safe unzip


def _zip(path: Path, members: dict[str, bytes], symlink: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return path


def test_safe_unzip_extracts_and_skips_binaries(tmp_path: Path) -> None:
    z = _zip(tmp_path / "s.zip", {"pkg/train.py": b"print(1)\n", "pkg/model.pt": b"\0\1", "__MACOSX/x": b"x",
                                  "pkg/README.md": b"run: python pkg/train.py\n"})
    warnings = CI.safe_unzip(z, tmp_path / "out")
    assert (tmp_path / "out/pkg/train.py").read_text() == "print(1)\n"
    assert not (tmp_path / "out/pkg/model.pt").exists()
    assert not (tmp_path / "out/__MACOSX").exists()
    assert any("binary" in w for w in warnings)


@pytest.mark.parametrize("bad", ["../evil.py", "/abs/evil.py", "a/../../evil.py", "C:/evil.py"])
def test_safe_unzip_rejects_traversal(tmp_path: Path, bad: str) -> None:
    z = _zip(tmp_path / "s.zip", {"ok.py": b"1", bad: b"2"})
    with pytest.raises(CI.UnsafeArchiveError):
        CI.safe_unzip(z, tmp_path / "out")
    assert not (tmp_path / "out").exists()  # nothing written before the check
    assert not (tmp_path / "evil.py").exists()


def test_safe_unzip_rejects_symlink(tmp_path: Path) -> None:
    z = _zip(tmp_path / "s.zip", {"ok.py": b"1"}, symlink="link")
    with pytest.raises(CI.UnsafeArchiveError):
        CI.safe_unzip(z, tmp_path / "out")


def test_safe_unzip_size_cap(tmp_path: Path) -> None:
    z = _zip(tmp_path / "s.zip", {"big.txt": b"x" * 5000})
    with pytest.raises(CI.UnsafeArchiveError):
        CI.safe_unzip(z, tmp_path / "out", max_bytes=1000)


# ---------------------------------------------------------------- index


def test_index_tree_entries_deps_languages(tmp_path: Path) -> None:
    root = tmp_path / "code"
    (root / "src").mkdir(parents=True)
    (root / "src/train_model.py").write_text("import x\n\nprint(2)\n")
    (root / "src/utils.py").write_text("def f():\n    return 1\n")
    (root / "src/cli.py").write_text("def main():\n    pass\n\nif __name__ == '__main__':\n    main()\n")
    (root / "analysis.ipynb").write_text("{}\n")
    (root / "requirements.txt").write_text("numpy\n")
    (root / "README.md").write_text("Run `python src/utils.py --all`\n")
    (root / ".git").mkdir()
    (root / ".git/config").write_text("x\n")
    (root / "weights.bin").write_bytes(b"\0" * 10)
    idx = CI.index_tree(root)
    paths = {f["path"] for f in idx.files}
    assert ".git/config" not in paths and "weights.bin" in paths
    assert idx.present
    assert idx.languages["Python"] == 3 + 2 + 5
    assert idx.entry_candidates[:2] == ["analysis.ipynb", "src/train_model.py"]
    assert "src/utils.py" in idx.entry_candidates  # from the README command
    assert idx.entry_candidates[-1] == "src/cli.py"  # __main__ guard
    assert idx.dependency_files == ["requirements.txt"]
    assert next(f for f in idx.files if f["path"] == "weights.bin")["is_source"] is False


def test_build_code_index_with_zip_uses_work_dir(tmp_path: Path) -> None:
    code = tmp_path / "paper/code"
    code.mkdir(parents=True)
    _zip(code / "submission.zip", {"proj/main.py": b"print(1)\n"})
    work = tmp_path / "var/work/p1/code"
    idx, warnings = CI.build_code_index(code, work, "Code: https://github.com/someone/proj.")
    assert idx.present and idx.root == str(work)
    assert "submission/proj/main.py" in idx.entry_candidates
    assert idx.repo_urls_in_paper == ["https://github.com/someone/proj"]
    assert warnings == []


def test_build_code_index_absent(tmp_path: Path) -> None:
    idx, _ = CI.build_code_index(None, tmp_path / "w", "no urls here")
    assert idx.present is False and idx.files == []


# ---------------------------------------------------------------- fetch (mocked)


@pytest.mark.parametrize(
    ("ref", "source", "ident"),
    [("arxiv:2401.01234", "arxiv", "2401.01234"), ("2401.01234v2", "arxiv", "2401.01234v2"),
     ("https://arxiv.org/abs/2401.01234", "arxiv", "2401.01234"),
     ("https://arxiv.org/pdf/2401.01234v1.pdf", "arxiv", "2401.01234v1"),
     ("https://arxiv.org/html/2609.03377v1", "arxiv", "2609.03377v1"),
     ("https://arxiv.org/html/2609.03377v1/", "arxiv", "2609.03377v1"),
     ("https://arxiv.org/html/2609.03377v1#S3", "arxiv", "2609.03377v1"),
     ("arxiv.org/abs/2401.01234?context=cs", "arxiv", "2401.01234"),
     ("https://export.arxiv.org/abs/hep-th/9901001", "arxiv", "hep-th/9901001"),
     ("https://doi.org/10.48550/arXiv.2401.01234", "arxiv", "2401.01234"),
     ("10.48550/arXiv.2401.01234", "arxiv", "2401.01234"),
     ("openreview:AbCdEf1234", "openreview", "AbCdEf1234"),
     ("https://openreview.net/forum?id=AbCdEf1234", "openreview", "AbCdEf1234")],
)
def test_parse_ref(ref: str, source: str, ident: str) -> None:
    r = F.parse_ref(ref)
    assert (r.source, r.external_id) == (source, ident)


def test_parse_ref_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        F.parse_ref("https://example.com/paper.pdf")


def test_pdf_urls_are_pdf_only() -> None:
    assert F.parse_ref("openreview:AbCdEf1234").pdf_url == "https://openreview.net/pdf?id=AbCdEf1234"
    assert F.parse_ref("arxiv:2401.01234").pdf_url == "https://arxiv.org/pdf/2401.01234"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_pdf_ok_and_requests_only_the_pdf() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"%PDF-1.7 fake")

    data = F.fetch_pdf(F.parse_ref("openreview:AbCdEf1234"), client=_client(handler))
    assert data.startswith(b"%PDF")
    assert seen == ["https://openreview.net/pdf?id=AbCdEf1234"]


def test_fetch_pdf_rejects_non_pdf_large_and_errors() -> None:
    ref = F.parse_ref("arxiv:2401.01234")
    with pytest.raises(F.FetchError, match="not a PDF"):
        F.fetch_pdf(ref, client=_client(lambda r: httpx.Response(200, content=b"<html>")))
    with pytest.raises(F.FetchError, match="larger"):
        F.fetch_pdf(ref, max_mb=0.001, client=_client(lambda r: httpx.Response(200, content=b"%PDF" + b"x" * 5000)))
    with pytest.raises(F.FetchError, match="404"):
        F.fetch_pdf(ref, client=_client(lambda r: httpx.Response(404)))


def test_store_upload(tmp_path: Path) -> None:
    p, sha = F.store_upload(b"%PDF-1.4 x", tmp_path)
    assert p == tmp_path / "uploads" / f"{sha}.pdf" and p.read_bytes() == b"%PDF-1.4 x"
    with pytest.raises(F.FetchError):
        F.store_upload(b"nope", tmp_path)
