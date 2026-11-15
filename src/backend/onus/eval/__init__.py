"""Evaluation (F10). The only package, with `onus.bundle`, allowed to read `<data_root>/labels/`.

All label access goes through `label_paths()`. Pipeline modules must never import from here;
`tests/test_barrier.py` enforces it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

LABELS_DIRNAME = "labels"


@dataclass(frozen=True)
class LabelPaths:
    root: Path
    papers_csv: Path
    code_tests_csv: Path
    reviews_dir: Path
    conference_dir: Path

    def exists(self) -> bool:
        return self.root.is_dir()


def label_paths(data_root: Path) -> LabelPaths:
    root = Path(data_root) / LABELS_DIRNAME
    return LabelPaths(
        root=root,
        papers_csv=root / "papers.csv",
        code_tests_csv=root / "code_tests.csv",
        reviews_dir=root / "reviews",
        conference_dir=root / "conference",
    )
