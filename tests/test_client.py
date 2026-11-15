"""LLMClient with the transport patched: cache, ledger, budget, structured output and repair."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from onus.llm.capabilities import CAPS_FILE, Capabilities, RoleCaps
from onus.llm.client import LLMCallError, LLMClient, LLMOutputError, parse_json_output
from onus.llm.costs import BudgetExceeded
from onus.llm.prompts import PromptRegistry
from tests.conftest import write_prompt


class City(BaseModel):
    city: str
    country: str


def reply(content: str | None, tin: int = 100, tout: int = 20, cached: int = 0, tool_calls=None) -> dict:
    return {
        "choices": [{"message": {"content": content, "tool_calls": tool_calls}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": tin, "completion_tokens": tout, "prompt_tokens_details": {"cached_tokens": cached}},
    }


class Transport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def __call__(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def make_client(settings, prompts_tmp, monkeypatch):
    write_prompt(prompts_tmp, "city", "# system\nAnswer.\n# user\nCapital of {{country}}?", role="extract")
    write_prompt(prompts_tmp, "judge_city", "Capital of {{country}}?", role="judge", temperature=0.3)

    def _make(*replies, caps: Capabilities | None = None):
        if caps is not None:
            settings.var_dir.mkdir(parents=True, exist_ok=True)
            (settings.var_dir / CAPS_FILE).write_text(caps.model_dump_json())
        c = LLMClient(settings, prompts=PromptRegistry(prompts_tmp))
        t = Transport(*replies)
        monkeypatch.setattr(c, "_acompletion", t)
        return c, t

    return _make


async def test_complete_json_defaults_to_json_object_with_schema_in_system(make_client):
    c, t = make_client(reply('{"city": "Paris", "country": "France"}'))
    obj, meta = await c.complete_json("city", {"country": "France"}, City, paper_id="p1")
    assert obj == City(city="Paris", country="France")
    call = t.calls[0]
    assert call["model"] == "openai/zai-org/GLM-5.3-Flash"
    assert call["api_base"] == "https://api.berget.ai/v1" and call["num_retries"] == 3 and call["timeout"] == 180
    assert call["response_format"] == {"type": "json_object"}
    assert call["messages"][0]["role"] == "system" and '"city"' in call["messages"][0]["content"]
    assert meta.calls == 1 and not meta.cache_hit and meta.eur == pytest.approx((100 * 0.25 + 20 * 0.5) / 1e6)


async def test_complete_json_uses_json_schema_when_supported(make_client, settings):
    caps = Capabilities(roles={"extract": RoleCaps(model=settings.onus_model_extract, json_schema=True)})
    c, t = make_client(reply('{"city": "Paris", "country": "France"}'), caps=caps)
    await c.complete_json("city", {"country": "France"}, City)
    rf = t.calls[0]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is False
    assert rf["json_schema"]["schema"]["properties"]["city"]["type"] == "string"
    assert t.calls[0]["messages"][0]["content"] == "Answer."  # no schema text when json_schema is used


async def test_caps_for_a_swapped_model_fall_back_to_defaults(make_client):
    caps = Capabilities(roles={"extract": RoleCaps(model="some/other-model", json_schema=True)})
    c, t = make_client(reply('{"city": "Paris", "country": "France"}'), caps=caps)
    await c.complete_json("city", {"country": "France"}, City)
    assert t.calls[0]["response_format"] == {"type": "json_object"}


async def test_cache_hit_on_repeat_and_run_index_misses(make_client, settings):
    c, t = make_client(reply('{"city": "Paris", "country": "France"}'), reply('{"city": "Paris", "country": "FR"}'))
    a, _m1 = await c.complete_json("city", {"country": "France"}, City, paper_id="p1")
    b, m2 = await c.complete_json("city", {"country": "France"}, City, paper_id="p1")
    assert a == b and len(t.calls) == 1 and m2.cache_hit and m2.eur == 0
    c2, _ = await c.complete_json("city", {"country": "France"}, City, run_index=1, paper_id="p1")
    assert len(t.calls) == 2 and c2.country == "FR"

    rows = [json.loads(x) for x in (settings.var_dir / "cost_ledger.jsonl").read_text().splitlines()]
    assert [r["cache_hit"] for r in rows] == [False, True, False]
    assert rows[0]["paper_id"] == "p1" and rows[0]["prompt_id"] == "city" and rows[0]["role"] == "extract"


async def test_repair_call_then_success(make_client):
    c, t = make_client(reply("Sure! The city is Paris."), reply('```json\n{"city": "Paris", "country": "France"}\n```'))
    obj, meta = await c.complete_json("city", {"country": "France"}, City)
    assert obj.city == "Paris" and meta.repaired and meta.calls == 2
    repair_msgs = t.calls[1]["messages"]
    assert repair_msgs[-2] == {"role": "assistant", "content": "Sure! The city is Paris."}
    assert "could not be parsed" in repair_msgs[-1]["content"]


async def test_two_failures_raise_llm_output_error(make_client):
    c, _ = make_client(reply('{"city": "Paris"}'), reply("nope"))
    with pytest.raises(LLMOutputError):
        await c.complete_json("city", {"country": "France"}, City)


async def test_transport_error_becomes_llm_call_error_without_key(make_client):
    c, _ = make_client(RuntimeError("boom with test-key-not-real inside"))
    with pytest.raises(LLMCallError) as ei:
        await c.complete_json("city", {"country": "France"}, City)
    assert "test-key-not-real" not in str(ei.value) and "boom" in str(ei.value)


async def test_budget_exceeded_stops_before_calling(make_client, settings):
    settings.onus_dev_budget_eur = 0.0
    c, t = make_client(reply('{"city": "Paris", "country": "France"}'))
    with pytest.raises(BudgetExceeded):
        await c.complete_json("city", {"country": "France"}, City)
    assert t.calls == []


async def test_judge_gets_reasoning_param_from_caps(make_client, settings):
    caps = Capabilities(roles={"judge": RoleCaps(model=settings.onus_model_judge, reasoning_style="extra_body.reasoning")})
    c, t = make_client(reply('{"city": "Paris", "country": "France"}'), caps=caps)
    await c.complete_json("judge_city", {"country": "France"}, City)
    assert t.calls[0]["extra_body"] == {"reasoning": {"effort": "high"}}
    assert t.calls[0]["temperature"] == 0.3
    assert t.calls[0]["model"] == "openai/moonshotai/Kimi-K3.1-Pro"


async def test_complete_tools_normalises_tool_calls(make_client):
    tc = [{"id": "c1", "function": {"name": "run", "arguments": '{"cmd": "ls"}'}}]
    c, t = make_client(reply(None, tool_calls=tc))
    out, meta = await c.complete_tools("judge", [{"role": "user", "content": "go"}], [{"type": "function"}],
                                       prompt_id="rerun_agent")
    assert out["tool_calls"] == [{"id": "c1", "name": "run", "arguments": '{"cmd": "ls"}'}]
    assert t.calls[0]["tool_choice"] == "auto" and meta.calls == 1


async def test_embed_caches_per_text(settings, monkeypatch):
    c = LLMClient(settings)
    seen: list[list[str]] = []

    async def fake_embedding(**kw):
        seen.append(kw["input"])
        return {"data": [{"index": i, "embedding": [float(len(t)), 1.0]} for i, t in enumerate(kw["input"])],
                "usage": {"prompt_tokens": 7}}

    monkeypatch.setattr(c, "_aembedding", fake_embedding)
    v1 = await c.embed(["ab", "abc"])
    v2 = await c.embed(["abc", "abcd"])
    assert v1 == [[2.0, 1.0], [3.0, 1.0]] and v2 == [[3.0, 1.0], [4.0, 1.0]]
    assert seen == [["ab", "abc"], ["abcd"]]


def test_parse_json_output_tolerates_fences_and_prose():
    assert parse_json_output('Here: {"city": "A", "country": "B"} done', City).city == "A"
    with pytest.raises(ValueError):
        parse_json_output("", City)
