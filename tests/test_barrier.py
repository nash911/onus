"""Hard rule 2: labels never become inputs.

Pipeline modules (ingest, claims, evidence, checks, verdict, codecheck, pipeline.py) may not import
`onus.eval` or `onus.bundle`, and may not build a filesystem path with a "labels" component. Row and
column "labels" in table code are fine: only path-building contexts are flagged.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import onus

PKG = Path(onus.__file__).parent
PIPELINE_PACKAGES = ("ingest", "claims", "evidence", "checks", "verdict", "codecheck")
PIPELINE_MODULES = ("pipeline.py",)
FORBIDDEN_MODULES = ("onus.eval", "onus.bundle")
PATH_CALLS = {"Path", "PurePath", "PosixPath", "join", "joinpath", "open", "glob", "rglob", "iglob", "listdir",
              "scandir", "walk", "exists", "is_dir", "is_file", "read_csv"}
LABELS_COMPONENT = re.compile(r"(^|[/\\])labels([/\\]|$)")


def pipeline_files() -> list[Path]:
    files = [PKG / m for m in PIPELINE_MODULES if (PKG / m).is_file()]
    for pkg in PIPELINE_PACKAGES:
        files += sorted((PKG / pkg).rglob("*.py"))
    return files


def module_name(path: Path) -> str:
    rel = path.relative_to(PKG.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _forbidden(mod: str) -> bool:
    return any(mod == f or mod.startswith(f + ".") for f in FORBIDDEN_MODULES)


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def violations(source: str, modname: str, is_package: bool = False) -> list[str]:
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    out: list[str] = []
    pkg_parts = modname.split(".") if is_package else modname.split(".")[:-1]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [f"line {node.lineno}: import {a.name}" for a in node.names if _forbidden(a.name)]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            targets = [mod] + [f"{mod}.{a.name}" for a in node.names]
            if any(_forbidden(t) for t in targets):
                out.append(f"line {node.lineno}: from {'.' * node.level}{node.module or ''} import ...")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _forbidden(node.value):
                out.append(f"line {node.lineno}: string names a forbidden module ({node.value!r})")
            elif LABELS_COMPONENT.search(node.value):
                parent = parents.get(node)
                if isinstance(parent, ast.JoinedStr):
                    parent = parents.get(parent)
                path_ctx = (isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div)) or (
                    isinstance(parent, ast.Call) and _call_name(parent) in PATH_CALLS
                ) or "/" in node.value or "\\" in node.value
                if path_ctx:
                    out.append(f"line {node.lineno}: builds a path with a 'labels' component ({node.value!r})")
    return out


@pytest.mark.parametrize("path", pipeline_files(), ids=lambda p: str(p.relative_to(PKG)))
def test_pipeline_module_respects_barrier(path: Path):
    found = violations(path.read_text(encoding="utf-8"), module_name(path), path.name == "__init__.py")
    assert not found, f"{path.relative_to(PKG)} breaks the label barrier:\n" + "\n".join(found)


def test_pipeline_files_found():
    names = {str(p.relative_to(PKG)) for p in pipeline_files()}
    assert "pipeline.py" in names and "ingest/pdf.py" in names and "codecheck/rerun/agent.py" in names


def test_label_access_goes_through_one_helper(tmp_path: Path):
    from onus.eval import label_paths

    lp = label_paths(tmp_path)
    assert lp.root == tmp_path / "labels" and lp.papers_csv == tmp_path / "labels" / "papers.csv"


# ---- the checker itself


@pytest.mark.parametrize(
    "src",
    [
        "import onus.eval",
        "import onus.bundle as b",
        "from onus.eval import label_paths",
        "from onus import eval",
        "from onus import bundle",
        "from ..eval import labels",
        "from .. import bundle",
        "from ..eval.metrics import x",
        "import importlib\nimportlib.import_module('onus.eval')",
        "root / 'labels'",
        "root / 'labels' / 'papers.csv'",
        "Path(root, 'labels')",
        "os.path.join(root, 'labels', 'x.csv')",
        "open('data/dev/labels/papers.csv')",
        "p = f'{root}/labels/papers.csv'",
        "root.joinpath('labels')",
    ],
)
def test_checker_catches(src: str):
    assert violations(src, "onus.claims.extract"), src


@pytest.mark.parametrize(
    "src",
    [
        "row_labels = [c.raw for c in rows[0]]",
        "d = {'labels': header}",
        "x = table['labels']",
        "match_labels(row, 'labels')",
        "from ..schemas.document import Document",
        "from onus.llm.client import get_client",
        "msg = 'row and column labels must match'",
    ],
)
def test_checker_allows(src: str):
    assert not violations(src, "onus.claims.extract"), src
