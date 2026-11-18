"""Live checks against Gemini (a fraction of a cent). Run only when a phase asks: pytest -m live"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from onus.config import Settings
from onus.llm.capabilities import Capabilities
from onus.llm.client import LLMClient
from onus.llm.prompts import PromptRegistry
from tests.conftest import write_prompt

pytestmark = pytest.mark.live


class Point(BaseModel):
    label: str
    value: float


class Points(BaseModel):
    items: list[Point]


def _gemini() -> Settings:
    s = Settings(onus_llm_provider="gemini")
    if not s.gemini_api_key.get_secret_value():
        pytest.skip("GEMINI_API_KEY not set")
    return s


@pytest.mark.parametrize("role", ["extract", "judge", "fast"])
async def test_gemini_structured_output(tmp_path, prompts_tmp, role):
    write_prompt(prompts_tmp, "points", "List two points: alpha = 1.5 and beta = 2. Use the labels given.",
                 role=role, max_tokens=2000)
    s = _gemini()
    c = LLMClient(s, var_dir=tmp_path / "var", prompts=PromptRegistry(prompts_tmp))
    c.caps = Capabilities.load(s.var_dir, "gemini")  # the probed capabilities
    obj, meta = await c.complete_json("points", {}, Points)
    assert {p.label for p in obj.items} == {"alpha", "beta"} and meta.eur < 0.02


async def test_gemini_embed_queries_and_documents(tmp_path):
    c = LLMClient(_gemini(), var_dir=tmp_path / "var")
    q = await c.embed(["accuracy improves"], task="query")
    d = await c.embed(["Table 2 shows accuracy rising from 81% to 86%.", "We thank the reviewers."])
    sims = [sum(a * b for a, b in zip(q[0], v, strict=True)) for v in d]
    assert len(q[0]) == len(d[0]) > 100 and sims[0] > sims[1]
