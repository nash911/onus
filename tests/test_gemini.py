"""Phase 15: the Gemini provider (ONUS_LLM_PROVIDER=gemini) with the transport patched. No network."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from onus.config import PRICES_EUR_PER_M, USD_TO_EUR, Settings
from onus.llm.cache import cache_key
from onus.llm.capabilities import Capabilities, RoleCaps, caps_file, reasoning_kwargs, reasoning_level
from onus.llm.client import E5_QUERY_PREFIX, GEMINI_DOCUMENT_PREFIX, GEMINI_QUERY_PREFIX, LLMClient
from onus.llm.costs import cost_eur
from onus.llm.prompts import PromptRegistry
from onus.version import fingerprint
from tests.conftest import write_prompt
from tests.test_client import Transport, reply


class City(BaseModel):
    city: str
    country: str


@pytest.fixture(autouse=True)
def _no_litellm(monkeypatch):
    """Importing LiteLLM loads the real .env into os.environ (its DEV mode), which would leak into other tests;
    the model-map lookup is stubbed here (tests that need a specific answer patch it themselves)."""
    import onus.llm.client as client_mod

    monkeypatch.setattr(client_mod, "gemini_takes_sampling_params", lambda m: True)


@pytest.fixture
def gsettings(tmp_path) -> Settings:
    return Settings(_env_file=None, onus_var_dir=tmp_path / "var", berget_api_key="test-berget-not-real",
                    gemini_api_key="test-gemini-not-real", onus_llm_provider="gemini")


def _client(s: Settings, prompts_tmp, monkeypatch, *replies, caps: Capabilities | None = None):
    write_prompt(prompts_tmp, "city", "# system\nAnswer.\n# user\nCapital of {{country}}?", role="extract")
    write_prompt(prompts_tmp, "judge_city", "Capital of {{country}}?", role="judge")
    if caps is not None:
        s.var_dir.mkdir(parents=True, exist_ok=True)
        (s.var_dir / caps_file(s.onus_llm_provider)).write_text(caps.model_dump_json())
    c = LLMClient(s, prompts=PromptRegistry(prompts_tmp))
    t = Transport(*replies)
    monkeypatch.setattr(c, "_acompletion", t)
    return c, t


def test_berget_is_the_default_and_keeps_the_v1_models_config(settings):
    assert settings.onus_llm_provider == "berget"
    assert settings.model_for("judge") == "moonshotai/Kimi-K3.1-Pro"
    assert "provider" not in settings.models_config  # v1.0's shape, so Berget hashes as before


def test_gemini_settings_map_every_role_to_a_pinned_model(gsettings):
    models = gsettings.models
    assert models == {"extract": "gemini-3.8-flash", "judge": "gemini-4-carbon", "fast": "gemini-3.5-flash-lite",
                      "embed": "gemini-embedding-2"}
    assert not any("latest" in m or "preview" in m for m in models.values())
    assert gsettings.models_config["provider"] == "gemini"
    assert gsettings.models_config["judge_reasoning"] == "medium"  # ONUS_GEMINI_JUDGE_REASONING default


def test_provider_moves_the_pipeline_fingerprint(settings, gsettings):
    assert fingerprint(settings=settings) != fingerprint(settings=gsettings)


async def test_gemini_requests_use_the_gemini_route_and_key(gsettings, prompts_tmp, monkeypatch):
    c, t = _client(gsettings, prompts_tmp, monkeypatch, reply('{"city": "Paris", "country": "France"}'))
    obj, meta = await c.complete_json("city", {"country": "France"}, City, paper_id="p1")
    call = t.calls[0]
    assert obj.city == "Paris" and call["model"] == "gemini/gemini-3.8-flash"
    assert call["api_key"] == "test-gemini-not-real" and "api_base" not in call
    p_in, _, p_out = PRICES_EUR_PER_M["gemini-3.8-flash"]
    assert meta.eur == pytest.approx((100 * p_in + 20 * p_out) / 1e6)


async def test_gemini_never_sends_the_berget_key(gsettings, prompts_tmp, monkeypatch):
    c, t = _client(gsettings, prompts_tmp, monkeypatch, reply('{"city": "Paris", "country": "France"}'))
    await c.complete_json("city", {"country": "France"}, City)
    assert "test-berget-not-real" not in repr(t.calls[0])


async def test_cache_keys_never_cross_providers(settings, gsettings, prompts_tmp, monkeypatch):
    # The same model id under both providers must not share a cache entry.
    gsettings.onus_gemini_model_extract = settings.onus_model_extract
    cb, _tb = _client(settings, prompts_tmp, monkeypatch, reply('{"city": "Paris", "country": "France"}'))
    await cb.complete_json("city", {"country": "France"}, City)
    cg, tg = _client(gsettings, prompts_tmp, monkeypatch, reply('{"city": "Lyon", "country": "France"}'))
    obj, meta = await cg.complete_json("city", {"country": "France"}, City)
    assert obj.city == "Lyon" and not meta.cache_hit and len(tg.calls) == 1


def test_berget_cache_key_is_unchanged_from_v1():
    # v1.0 keyed on the bare model id; Berget keys must stay byte-identical so the v1.0.1 cache still serves.
    msgs = [{"role": "user", "content": "x"}]
    assert cache_key("zai-org/GLM-5.3-Flash", msgs, {"temperature": 0.0}, 0) == cache_key(
        "zai-org/GLM-5.3-Flash", msgs, {"temperature": 0.0}, 0)
    c = LLMClient.__new__(LLMClient)
    c.settings = Settings(_env_file=None)
    assert c._key_model("zai-org/GLM-5.3-Flash") == "zai-org/GLM-5.3-Flash"
    c.settings = Settings(_env_file=None, onus_llm_provider="gemini")
    assert c._key_model("gemini-3.8-flash") == "gemini/gemini-3.8-flash"


async def test_models_that_ignore_sampling_params_do_not_get_them(gsettings, prompts_tmp, monkeypatch):
    import onus.llm.client as client_mod

    monkeypatch.setattr(client_mod, "gemini_takes_sampling_params", lambda m: m == "gemini-4-carbon")
    c, t = _client(gsettings, prompts_tmp, monkeypatch, reply('{"city": "Paris", "country": "France"}'),
                   reply('{"city": "Paris", "country": "France"}'))
    await c.complete_json("city", {"country": "France"}, City)
    await c.complete_json("judge_city", {"country": "France"}, City)
    assert "temperature" not in t.calls[0] and t.calls[0]["max_tokens"] == 256
    assert t.calls[1]["temperature"] == 0.0


async def test_judge_reasoning_maps_to_reasoning_effort(gsettings, prompts_tmp, monkeypatch):
    gsettings.onus_gemini_judge_reasoning = "high"
    caps = Capabilities(roles={"judge": RoleCaps(model="gemini-4-carbon", json_schema=True,
                                                 reasoning_style="reasoning_effort")})
    c, t = _client(gsettings, prompts_tmp, monkeypatch, reply('{"city": "Paris", "country": "France"}'), caps=caps)
    await c.complete_json("judge_city", {"country": "France"}, City)
    assert t.calls[0]["reasoning_effort"] == "high"  # the Gemini judge's own level, not ONUS_JUDGE_REASONING
    # json_object with the schema in the system message, even though the model supports json_schema
    assert t.calls[0]["response_format"] == {"type": "json_object"}
    assert '"city"' in t.calls[0]["messages"][0]["content"]
    assert reasoning_level("berget", "max") == "max" and reasoning_kwargs("reasoning_effort", "low") == {
        "reasoning_effort": "low"}


async def test_json_schema_mode_can_be_chosen_for_gemini(gsettings, prompts_tmp, monkeypatch):
    gsettings.onus_gemini_structured_output = "json_schema"
    caps = Capabilities(roles={"extract": RoleCaps(model="gemini-3.8-flash", json_schema=True)})
    c, t = _client(gsettings, prompts_tmp, monkeypatch, reply('{"city": "Paris", "country": "France"}'), caps=caps)
    await c.complete_json("city", {"country": "France"}, City)
    assert t.calls[0]["response_format"]["type"] == "json_schema"
    assert gsettings.models_config["structured_output"] == "json_schema"


def test_capabilities_are_kept_per_provider(tmp_path):
    var = tmp_path / "var"
    var.mkdir()
    (var / caps_file("gemini")).write_text(Capabilities(roles={"judge": RoleCaps(model="g", tools=True)})
                                           .model_dump_json())
    assert caps_file("berget") == "berget_capabilities.json"
    assert Capabilities.load(var, "gemini").for_role("judge", "g").tools is True
    assert Capabilities.load(var, "berget").roles == {}


def test_gemini_prices_are_converted_at_the_stated_rate():
    assert PRICES_EUR_PER_M["gemini-4-carbon"] == pytest.approx((2.00 * USD_TO_EUR, 0.10 * USD_TO_EUR,
                                                                 10.00 * USD_TO_EUR))
    for m in ("gemini-4-carbon", "gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-embedding-2"):
        assert m in PRICES_EUR_PER_M
    # cached input billed at the cached rate
    full = cost_eur("gemini-3.8-flash", 1_000_000, 0)
    cached = cost_eur("gemini-3.8-flash", 1_000_000, 0, cached_in=1_000_000)
    assert cached == pytest.approx(full / 10)


def test_long_context_price_applies_past_the_threshold(monkeypatch):
    from onus.llm import costs

    monkeypatch.setitem(costs.PRICES_EUR_PER_M, "m", (1.0, None, 2.0))
    monkeypatch.setitem(costs.LONG_CONTEXT_EUR_PER_M, "m", (200_000, (2.0, None, 3.0)))
    assert costs.cost_eur("m", 100_000, 0) == pytest.approx(0.1)
    assert costs.cost_eur("m", 300_000, 1_000_000) == pytest.approx(0.6 + 3.0)


async def test_embed_prefixes_depend_on_the_provider(settings, gsettings, monkeypatch):
    sent: list[list[str]] = []

    async def fake_embedding(**kw):
        sent.append(kw["input"])
        return {"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(kw["input"]))],
                "usage": {"prompt_tokens": 3}}

    for s in (settings, gsettings):
        c = LLMClient(s)
        monkeypatch.setattr(c, "_aembedding", fake_embedding)
        await c.embed(["claim"], task="query")
        await c.embed(["paragraph"])
    assert sent == [[E5_QUERY_PREFIX + "claim"], ["paragraph"],
                    [GEMINI_QUERY_PREFIX + "claim"], [GEMINI_DOCUMENT_PREFIX + "paragraph"]]


def _length_reply(tout: int = 8000) -> dict:
    r = reply(None, tout=tout)
    r["choices"][0]["finish_reason"] = "length"
    return r


_JUDGE_CAPS = Capabilities(roles={"judge": RoleCaps(model="gemini-4-carbon", json_schema=True,
                                                    reasoning_style="reasoning_effort")})


async def test_judge_that_thinks_out_its_budget_is_retried_at_low(gsettings, prompts_tmp, monkeypatch):
    c, t = _client(gsettings, prompts_tmp, monkeypatch, _length_reply(), reply('{"city": "Paris", "country": "France"}'),
                   caps=_JUDGE_CAPS)
    res, meta = await c.complete_json("judge_city", {"country": "France"}, City)
    assert res.city == "Paris" and len(t.calls) == 2 and not meta.repaired
    assert t.calls[0]["reasoning_effort"] == "medium" and t.calls[1]["reasoning_effort"] == "low"
    assert meta.extra["thinking_fallback"] == "low"
    assert gsettings.models_config["judge_fallback"] == "low"  # part of the fingerprint


async def test_thinking_fallback_off_or_not_judge_or_not_empty(gsettings, prompts_tmp, monkeypatch):
    gsettings.onus_gemini_judge_fallback = "off"
    c, t = _client(gsettings, prompts_tmp, monkeypatch, _length_reply(), reply('{"city": "Paris", "country": "France"}'),
                   caps=_JUDGE_CAPS)
    _, meta = await c.complete_json("judge_city", {"country": "France"}, City)
    assert meta.repaired and "thinking_fallback" not in meta.extra  # the usual repair path, no level change
    gsettings.onus_gemini_judge_fallback = "low"
    truncated = reply('{"city": "Paris", "country": "France"}')
    truncated["choices"][0]["finish_reason"] = "length"  # an answer came back: no retry
    c, t = _client(gsettings, prompts_tmp, monkeypatch, truncated, caps=_JUDGE_CAPS)
    _, meta = await c.complete_json("judge_city", {"country": "Spain"}, City)  # not the cached call above
    assert len(t.calls) == 1 and "thinking_fallback" not in meta.extra


async def test_berget_judge_never_gets_the_thinking_fallback(settings, prompts_tmp, monkeypatch):
    write_prompt(prompts_tmp, "judge_city", "Capital of {{country}}?", role="judge")
    c = LLMClient(settings, prompts=PromptRegistry(prompts_tmp))
    t = Transport(_length_reply(), reply('{"city": "Paris", "country": "France"}'))
    monkeypatch.setattr(c, "_acompletion", t)
    _, meta = await c.complete_json("judge_city", {"country": "France"}, City)
    assert meta.repaired and "thinking_fallback" not in meta.extra
    assert "judge_fallback" not in settings.models_config  # Berget's v1.0 shape is unchanged
