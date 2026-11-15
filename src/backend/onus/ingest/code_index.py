"""Safe unzip and static index of submitted code. No execution. (Phase 2)

`<paper_dir>/code/` may hold a source tree, one or more zip files, or both. Zips are extracted into
`$ONUS_VAR_DIR/work/<paper_id>/code/<zip stem>/` after checking every member: absolute paths, `..`
components and symlinks are rejected, total size is capped (500 MB by default), and binary files are
skipped. Source trees are indexed in place (read-only).
"""

from __future__ import annotations

import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

from onus.schemas.document import CodeIndex

MAX_UNZIP_BYTES = 500 * 1024 * 1024
MAX_TEXT_BYTES = 5 * 1024 * 1024

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".ipynb_checkpoints", ".idea", ".vscode",
             "__MACOSX", ".mypy_cache", ".pytest_cache", "wandb", ".tox", "site-packages"}
BINARY_EXT = {
    ".pt", ".pth", ".ckpt", ".bin", ".h5", ".hdf5", ".npy", ".npz", ".pkl", ".pickle", ".joblib", ".onnx",
    ".safetensors", ".tflite", ".pb", ".so", ".dll", ".dylib", ".exe", ".o", ".a", ".pyc", ".class", ".jar",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".whl", ".egg", ".png", ".jpg", ".jpeg",
    ".gif", ".bmp", ".tif", ".tiff", ".webp", ".ico", ".pdf", ".mp4", ".mov", ".avi", ".mp3", ".wav", ".flac",
    ".parquet", ".feather", ".arrow", ".db", ".sqlite", ".xlsx", ".xls", ".docx", ".pptx", ".ttf", ".woff",
    ".woff2", ".DS_Store",
}
LANGUAGES = {
    ".py": "Python", ".ipynb": "Jupyter", ".r": "R", ".jl": "Julia", ".m": "MATLAB", ".c": "C", ".h": "C",
    ".cc": "C++", ".cpp": "C++", ".hpp": "C++", ".cu": "CUDA", ".java": "Java", ".js": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".jsx": "JavaScript", ".go": "Go", ".rs": "Rust", ".scala": "Scala",
    ".sh": "Shell", ".bash": "Shell", ".sql": "SQL", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
    ".kt": "Kotlin", ".lua": "Lua", ".pl": "Perl", ".f90": "Fortran", ".stan": "Stan", ".do": "Stata",
}
DEPENDENCY_NAMES = {
    "requirements.txt", "environment.yml", "environment.yaml", "setup.py", "setup.cfg", "pyproject.toml",
    "pipfile", "pipfile.lock", "poetry.lock", "conda.yaml", "conda.yml", "package.json", "renv.lock",
    "description", "dockerfile", "cargo.toml", "go.mod", "project.toml", "manifest.toml", "uv.lock",
}


class UnsafeArchiveError(ValueError):
    pass


def _is_binary_name(name: str) -> bool:
    p = PurePosixPath(name)
    return p.suffix.lower() in {e.lower() for e in BINARY_EXT} or p.name == ".DS_Store"


def _member_ok(info: zipfile.ZipInfo) -> PurePosixPath:
    name = info.filename.replace("\\", "/")
    p = PurePosixPath(name)
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in p.parts:
        raise UnsafeArchiveError(f"unsafe path in archive: {info.filename!r}")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise UnsafeArchiveError(f"symlink in archive: {info.filename!r}")
    return p


def safe_unzip(zip_path: Path, dest: Path, *, max_bytes: int = MAX_UNZIP_BYTES) -> list[str]:
    """Extract `zip_path` into `dest`. Rejects unsafe members up front; skips binaries and skip-dirs.

    Returns warnings. Raises `UnsafeArchiveError` before writing anything if any member is unsafe or the
    declared total size exceeds `max_bytes`."""
    warnings: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        checked = [(info, _member_ok(info)) for info in infos]
        total = sum(i.file_size for i, _ in checked if not i.is_dir() and not _is_binary_name(i.filename))
        if total > max_bytes:
            raise UnsafeArchiveError(f"archive too large: {total} bytes > {max_bytes}")
        dest.mkdir(parents=True, exist_ok=True)
        root = dest.resolve()
        skipped = 0
        written = 0
        for info, p in checked:
            if info.is_dir() or any(part in SKIP_DIRS for part in p.parts):
                continue
            if _is_binary_name(info.filename):
                skipped += 1
                continue
            target = (dest / Path(*p.parts)).resolve()
            if root not in target.parents:
                raise UnsafeArchiveError(f"path escapes destination: {info.filename!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                remaining = max_bytes - written
                n = 0
                while chunk := src.read(1 << 16):
                    n += len(chunk)
                    if n > remaining:
                        raise UnsafeArchiveError("archive exceeded size cap while extracting")
                    out.write(chunk)
                written += n
        if skipped:
            warnings.append(f"{zip_path.name}: skipped {skipped} binary files")
    return warnings


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return b"\0" in f.read(4096)
    except OSError:
        return True


def _walk(root: Path):
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.is_symlink() or not p.is_file():
            continue
        yield p, rel


_URL = re.compile(
    r"https?://(?:www\.)?(?:github\.com|gitlab\.com|bitbucket\.org|huggingface\.co|zenodo\.org|osf\.io|"
    r"figshare\.com|anonymous\.4open\.science|codeocean\.com|colab\.research\.google\.com|doi\.org/10\.5281)"
    r"/[^\s)\]}>,;\"']+",
    re.IGNORECASE,
)


def repo_urls(text: str) -> list[str]:
    seen: list[str] = []
    for m in _URL.finditer(text):
        u = m.group(0).rstrip(".")
        if u not in seen:
            seen.append(u)
    return seen


_README_CMD = re.compile(r"(?:python3?|py)\s+(?:-m\s+)?(?P<script>[\w./-]+\.py)\b")
_ENTRY = re.compile(r"^(?:main|run[\w-]*|train[\w-]*|reproduce[\w-]*|experiment[s]?[\w-]*|demo)\.py$|^makefile$", re.IGNORECASE)


_MAIN_GUARD = re.compile(r"^if\s+__name__\s*==\s*['\"]__main__['\"]\s*:", re.MULTILINE)


def index_tree(root: Path, display_root: str | None = None) -> CodeIndex:
    files: list[dict] = []
    languages: dict[str, int] = {}
    entries: list[str] = []
    deps: list[str] = []
    readme_scripts: list[str] = []
    main_guarded: list[str] = []
    for p, rel in _walk(root):
        relp = rel.as_posix()
        ext = p.suffix.lower()
        size = p.stat().st_size
        binary = _is_binary_name(p.name) or size > MAX_TEXT_BYTES or _looks_binary(p)
        n_lines = 0
        if not binary:
            try:
                with p.open("rb") as f:
                    n_lines = sum(1 for _ in f)
            except OSError:
                binary = True
        is_source = ext in LANGUAGES and not binary
        files.append({"path": relp, "ext": ext, "lines": n_lines, "bytes": size, "is_source": is_source})
        if is_source:
            lang = LANGUAGES[ext]
            languages[lang] = languages.get(lang, 0) + n_lines
        if p.name.lower() in DEPENDENCY_NAMES or re.match(r"requirements[\w.-]*\.txt$", p.name, re.IGNORECASE):
            deps.append(relp)
        if _ENTRY.match(p.name) or ext == ".ipynb":
            entries.append(relp)
        if ext == ".py" and is_source and relp not in entries:
            try:
                if _MAIN_GUARD.search(p.read_text(errors="replace")):
                    main_guarded.append(relp)
            except OSError:
                pass
        if p.name.lower().startswith("readme") and not binary:
            try:
                txt = p.read_text(errors="replace")
            except OSError:
                txt = ""
            readme_scripts += [m.group("script") for m in _README_CMD.finditer(txt)]
    paths = {f["path"] for f in files}
    for s in readme_scripts:
        s = s.lstrip("./")
        match = s if s in paths else next((q for q in sorted(paths) if q.endswith("/" + s)), None)
        if match and match not in entries:
            entries.append(match)
    entries += [m for m in main_guarded if m not in entries]  # scripts with a __main__ guard come last
    return CodeIndex(present=any(f["is_source"] for f in files), root=display_root or str(root), files=files,
                     languages=languages, entry_candidates=entries, dependency_files=deps)


def build_code_index(code_dir: Path | None, work_dir: Path, paper_text: str = "") -> tuple[CodeIndex, list[str]]:
    """Index `<paper_dir>/code/`. Zips are extracted under `work_dir` (which is wiped first)."""
    warnings: list[str] = []
    urls = repo_urls(paper_text)
    if code_dir is None or not code_dir.is_dir():
        return CodeIndex(present=False, repo_urls_in_paper=urls), warnings
    zips = sorted(p for p in code_dir.rglob("*.zip") if p.is_file())
    root = code_dir
    if zips:
        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True)
        for z in zips:
            try:
                warnings += safe_unzip(z, work_dir / z.stem)
            except (UnsafeArchiveError, zipfile.BadZipFile) as e:
                warnings.append(f"{z.name}: not extracted ({e})")
        # loose (non-zip) files next to the zips are indexed too
        for p, rel in _walk(code_dir):
            if p.suffix.lower() == ".zip":
                continue
            dest = work_dir / "_tree" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
        root = work_dir
    idx = index_tree(root)
    idx.repo_urls_in_paper = urls
    if not idx.present:
        warnings.append("code directory has no source files")
    return idx, warnings
