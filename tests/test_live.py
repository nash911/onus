"""Live checks against Berget (cost a fraction of a cent). Run only when a phase asks: pytest -m live"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from onus.config import get_settings
from onus.llm.client import LLMClient
from onus.llm.prompts import PromptRegistry
from tests.conftest import write_prompt

pytestmark = pytest.mark.live


class Point(BaseModel):
    label: str
    value: float


class Points(BaseModel):
    items: list[Point]


async def test_complete_json_with_nested_schema(tmp_path, prompts_tmp):
    write_prompt(prompts_tmp, "points", "List two points: alpha = 1.5 and beta = 2. Use the labels given.",
                 role="extract", max_tokens=600)
    s = get_settings()
    c = LLMClient(s, var_dir=tmp_path / "var", prompts=PromptRegistry(prompts_tmp))
    c.caps = type(c.caps).load(s.var_dir)  # use the probed capabilities
    obj, meta = await c.complete_json("points", {}, Points)
    assert {p.label for p in obj.items} == {"alpha", "beta"} and meta.eur < 0.01


async def test_embed(tmp_path):
    c = LLMClient(get_settings(), var_dir=tmp_path / "var")
    vecs = await c.embed(["Instruct: Retrieve evidence\\nQuery: accuracy", "accuracy table"])
    assert len(vecs) == 2 and len(vecs[0]) == len(vecs[1]) > 100
