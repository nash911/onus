"""Hard rule 9: after `onus freeze`, the fingerprint may not drift without a version bump."""

from __future__ import annotations

import pytest

from onus import version as v


def test_fingerprint_matches_frozen():
    frozen = v.load_frozen()
    if frozen is None:
        pytest.skip("not frozen yet (no frozen.json)")
    current = v.fingerprint_parts()
    changed = [k for k in ("version", "models", "prompts", "rubrics") if current[k] != frozen.get(k)]
    assert v.fingerprint() == frozen["fingerprint"], (
        f"Fingerprint drifted from frozen {frozen['version']} ({frozen['fingerprint']} -> {v.fingerprint()}); "
        f"changed: {changed}. Bump the version with `onus freeze --version X.Y.Z` instead of editing frozen logic."
    )
