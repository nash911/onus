"""Hard rule 8: after `onus freeze`, neither the pipeline fingerprint (models, prompts, rubrics, pipeline source)
nor the harness fingerprint (eval/ scoring modules and the harness prompts) may drift without a version bump."""

from __future__ import annotations

import pytest

from onus import version as v


def _frozen() -> dict:
    frozen = v.load_frozen()
    if frozen is None:
        pytest.skip("not frozen yet (no frozen.json)")
    return frozen


def test_fingerprint_matches_frozen():
    frozen = _frozen()
    current = v.fingerprint_parts()
    changed = [k for k in ("version", "models", "prompts", "rubrics", "source") if current[k] != frozen.get(k)]
    files = sorted(set(current["source"]) ^ set(frozen.get("source", {}))
                   | {f for f, h in current["source"].items() if frozen.get("source", {}).get(f) not in (None, h)})
    assert v.fingerprint() == frozen["fingerprint"], (
        f"Pipeline fingerprint drifted from frozen {frozen['version']} ({frozen['fingerprint']} -> {v.fingerprint()}); "
        f"changed: {changed} {files[:10]}. Bump the version with `onus freeze --version X.Y.Z` instead of editing "
        "frozen logic."
    )


def test_harness_fingerprint_matches_frozen():
    frozen = _frozen()
    assert "harness_fingerprint" in frozen, "frozen.json has no harness fingerprint; re-freeze"
    assert v.harness_fingerprint() == frozen["harness_fingerprint"], (
        f"Harness fingerprint drifted ({frozen['harness_fingerprint']} -> {v.harness_fingerprint()}). The harness is "
        "fixed from eval #1: log the fix and the new fingerprint in docs/public/eval_protocol.md §4 and re-freeze."
    )
