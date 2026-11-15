from __future__ import annotations

import pytest
from pydantic import BaseModel

from onus.llm.client import LLMOutputError, get_client, set_client
from onus.llm.fake import FakeLLM, hashed_embedding
from onus.llm.prompts import PromptError, PromptRegistry
from tests.conftest import write_prompt


class Items(BaseModel):
    items: list[str]


async def test_canned_sequence_and_callable():
    fake = FakeLLM({"a": [{"items": ["1"]}, {"items": ["2"]}], "b": lambda vars, run: {"items": [vars["x"], str(run)]}})
    assert (await fake.complete_json("a", {}, Items))[0].items == ["1"]
    assert (await fake.complete_json("a", {}, Items))[0].items == ["2"]
    assert (await fake.complete_json("a", {}, Items))[0].items == ["2"]  # last repeats
    assert (await fake.complete_json("b", {"x": "q"}, Items, run_index=2))[0].items == ["q", "2"]
    assert [c["prompt_id"] for c in fake.calls] == ["a", "a", "a", "b"]


async def test_invalid_output_and_errors():
    fake = FakeLLM({"bad": {"items": "nope"}, "err": LLMOutputError("x")})
    with pytest.raises(LLMOutputError):
        await fake.complete_json("bad", {}, Items)
    with pytest.raises(LLMOutputError):
        await fake.complete_json("err", {}, Items)
    with pytest.raises(KeyError):
        await fake.complete_json("missing", {}, Items)


async def test_checks_variables_against_real_template(prompts_tmp):
    write_prompt(prompts_tmp, "t", "{{needed}}")
    fake = FakeLLM({"t": {"items": []}}, prompts=PromptRegistry(prompts_tmp))
    with pytest.raises(PromptError):
        await fake.complete_json("t", {}, Items)
    await fake.complete_json("t", {"needed": 1}, Items)


async def test_embeddings_deterministic_and_similar():
    fake = FakeLLM()
    a, b, c = await fake.embed(["accuracy on ImageNet", "ImageNet accuracy", "learning rate schedule"])
    dot = lambda x, y: sum(i * j for i, j in zip(x, y, strict=True))
    assert a == hashed_embedding("accuracy on ImageNet", 64)
    assert dot(a, b) > dot(a, c)


def test_set_client_installs_fake():
    fake = FakeLLM()
    set_client(fake)  # type: ignore[arg-type]
    assert get_client() is fake
