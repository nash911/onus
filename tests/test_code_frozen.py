"""Hard rule 8 for code-v1: after `onus freeze --code-version X.Y.Z`, the code-v1 fingerprint (codecheck/,
prompts/code/, rubrics/code/, the shared tolerance rubric and the models config) may not drift."""

from __future__ import annotations

import pytest

from onus.codecheck import version as cv
from onus.version import load_frozen


def test_code_fingerprint_matches_frozen():
    code = (load_frozen() or {}).get("code")
    if code is None:
        pytest.skip("code-v1 not frozen yet (onus freeze --code-version X.Y.Z)")
    cur = cv.code_parts()
    changed = [k for k in ("version", "models", "prompts", "rubrics", "shared_rubrics", "source")
               if cur[k] != code.get(k)]
    assert cv.code_fingerprint() == code["fingerprint"], (
        f"code-v1 fingerprint drifted ({code['fingerprint']} -> {cv.code_fingerprint()}); changed: {changed}. "
        "Bump with `onus freeze --code-version X.Y.Z`.")
