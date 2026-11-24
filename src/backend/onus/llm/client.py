"""The only way Onus talks to its model provider (Berget AI or Gemini, `ONUS_LLM_PROVIDER`).

Every chat and embedding call goes through `LLMClient`: LiteLLM (its generic OpenAI provider pointed at Berget,
or its `gemini/` provider with GEMINI_API_KEY), a response cache, the cost ledger and the budget guard, and a
per-process concurrency limit. Berget requests and cache keys are exactly those of v1.0; a Gemini cache key
carries the provider, so a cached answer from one provider is never served for the other.

- `complete_json(prompt_id, variables, schema)`: structured output, parsed with Pydantic; one repair call
  on failure, then `LLMOutputError`. Callers catch `LLMError`, mark their check `inconclusive` and log it:
  a model error never crashes a paper. `BudgetExceeded` is not an `LLMError`: it stops the run.
- `complete_tools(...)`: one turn of a tool-calling loop (the re-run agent).
- `embed(texts)`: embeddings, cached per text.

Structured outputs with a list at the top level should wrap it in an object (`{"items": [...]}`):
`json_object` mode requires an object.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from onus.config import Role, Settings, get_settings
from onus.llm.cache import LLMCache, cache_key
from onus.llm.capabilities import Capabilities, reasoning_kwargs, reasoning_level
from onus.llm.costs import BudgetExceeded, BudgetGuard, CostLedger, LedgerEntry, cost_eur
from onus.llm.prompts import PromptRegistry

# Keep LiteLLM offline and quiet: no remote cost-map fetch, no telemetry.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_TELEMETRY", "False")

T = TypeVar("T", bound=BaseModel)

DEFAULT_CONCURRENCY = 8
TIMEOUT_S = 180
# A Gemini judge call thinks at length (thinking counts against its max_tokens, which ONUS_GEMINI_JUDGE_TOKEN_FACTOR
# raises), so it gets this many seconds per unit of the factor (4x -> 20 minutes), and a timeout is not retried:
# a call that ran out of time would most likely run out again, and each attempt is billed.
GEMINI_JUDGE_TIMEOUT_S_PER_FACTOR = 300
NUM_RETRIES = 3
# provider throttling / outages: retried here with backoff (LiteLLM's own retries are immediate)
TRANSIENT_ERRORS = ("RateLimitError", "ServiceUnavailableError", "APIConnectionError", "InternalServerError",
                    "Timeout")
TRANSIENT_BACKOFF_S = (5.0, 20.0, 60.0)
EMBED_BATCH = 64
# Retrieval embeddings. e5-instruct (Berget): documents plain, queries with an instruction prefix (v1.0).
# gemini-embedding-2 takes no task_type; its documented retrieval prefixes are used instead.
E5_QUERY_PREFIX = "Instruct: Retrieve evidence that supports or refutes this scientific claim\nQuery: "
GEMINI_QUERY_PREFIX = "task: fact checking | query: "
GEMINI_DOCUMENT_PREFIX = "title: none | text: "

REPAIR_INSTRUCTION = (
    "Your previous reply could not be parsed against the required JSON schema.\n"
    "Error:\n{error}\n\n"
    "Reply again with only the corrected JSON object, no prose and no code fences."
)
SCHEMA_INSTRUCTION = (
    "Reply with a single JSON object, no prose and no code fences, that validates against this JSON schema:\n"
    "{schema}"
)


class LLMError(Exception):
    """A model call failed or its output was unusable. Callers degrade, never crash."""


class LLMCallError(LLMError):
    def __init__(self, message: str):
        super().__init__(message)
        self.short = message[:240]


class LLMOutputError(LLMError):
    pass


@dataclass
class CallMeta:
    prompt_id: str
    role: str
    model: str
    prompt_version: str | None = None
    calls: int = 0
    cache_hits: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cached_in: int = 0
    eur: float = 0.0
    latency_s: float = 0.0
    repaired: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def cache_hit(self) -> bool:
        return self.calls > 0 and self.cache_hits == self.calls

    def add(self, usage: dict, eur: float, hit: bool, latency: float) -> None:
        self.calls += 1
        self.cache_hits += int(hit)
        self.tokens_in += int(usage.get("prompt_tokens", 0))
        self.tokens_out += int(usage.get("completion_tokens", 0))
        self.cached_in += int(usage.get("cached_tokens", 0))
        self.eur += eur
        self.latency_s += latency


def _litellm() -> Any:
    import warnings

    import litellm

    # LiteLLM's own response models trigger harmless pydantic serializer warnings on every call.
    warnings.filterwarnings("ignore", message="Pydantic serializer warnings", category=UserWarning)

    litellm.telemetry = False
    litellm.suppress_debug_info = True
    litellm.drop_params = False  # an unsupported parameter must fail loudly, not vanish
    return litellm


SAMPLING_PARAMS = ("temperature", "top_p", "top_k")


def gemini_takes_sampling_params(model: str) -> bool:
    """False for Gemini models that ignore temperature / top_p (LiteLLM's model map, `supports_sampling_params`);
    LiteLLM refuses to send them there, so the client leaves them out of the request (not out of the cache key)."""
    try:
        from litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini import VertexGeminiConfig
    except ImportError:  # pragma: no cover - older LiteLLM: forward as before
        return True
    _litellm()
    return bool(VertexGeminiConfig.supports_sampling_params(f"gemini/{model}"))


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def normalize_response(resp: Any) -> dict:
    """LiteLLM ModelResponse -> plain dict (what the cache stores)."""
    choice = _get(resp, "choices")[0]
    msg = _get(choice, "message")
    tool_calls = []
    for tc in _get(msg, "tool_calls") or []:
        fn = _get(tc, "function")
        tool_calls.append({"id": _get(tc, "id"), "name": _get(fn, "name"), "arguments": _get(fn, "arguments")})
    usage = _get(resp, "usage")
    return {
        "content": _get(msg, "content"),
        "reasoning_content": _get(msg, "reasoning_content"),
        "tool_calls": tool_calls,
        "finish_reason": _get(choice, "finish_reason"),
        "usage": {
            "prompt_tokens": int(_get(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(_get(usage, "completion_tokens", 0) or 0),
            "cached_tokens": int(_get(_get(usage, "prompt_tokens_details"), "cached_tokens", 0) or 0),
            "reasoning_tokens": int(_get(_get(usage, "completion_tokens_details"), "reasoning_tokens", 0) or 0),
        },
    }


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_output[M: BaseModel](text: str | None, schema: type[M]) -> M:
    """Parse a model reply into `schema`. Tolerates code fences and prose around one JSON value."""
    if not text or not text.strip():
        raise ValueError("empty reply")
    t = _FENCE.sub("", text.strip()).strip()
    try:
        return schema.model_validate_json(t)
    except ValidationError:
        starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
        end = max(t.rfind("}"), t.rfind("]"))
        if starts and end > min(starts):
            candidate = t[min(starts) : end + 1]
            if candidate != t:
                return schema.model_validate_json(candidate)
        raise


class LLMClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        var_dir: Path | None = None,
        prompts: PromptRegistry | None = None,
        mode: Literal["batch", "live"] = "batch",
        concurrency: int | None = None,
    ):
        self.settings = settings or get_settings()
        self.var_dir = var_dir or self.settings.var_dir
        self.cache = LLMCache(self.var_dir / "llm_cache")
        self.ledger = CostLedger(self.var_dir / "cost_ledger.jsonl")
        self.guard = BudgetGuard(
            self.ledger,
            dev_budget_eur=self.settings.onus_dev_budget_eur if mode == "batch" else None,
            max_eur_per_paper=self.settings.onus_max_eur_per_paper,
        )
        self.caps = Capabilities.load(self.var_dir, self.settings.onus_llm_provider)
        self.prompts = prompts or PromptRegistry()
        self.mode = mode
        self._concurrency = concurrency or self.settings.onus_llm_concurrency or DEFAULT_CONCURRENCY
        self._sems: dict[int, asyncio.Semaphore] = {}

    # ------------------------------------------------------------ transport (patched in tests)

    async def _acompletion(self, **kwargs: Any) -> Any:
        return await _litellm().acompletion(**kwargs)

    async def _aembedding(self, **kwargs: Any) -> Any:
        return await _litellm().aembedding(**kwargs)

    def _sem(self) -> asyncio.Semaphore:
        loop_id = id(asyncio.get_running_loop())
        if loop_id not in self._sems:
            self._sems[loop_id] = asyncio.Semaphore(self._concurrency)
        return self._sems[loop_id]

    async def _with_backoff(self, fn, *, retry_timeouts: bool = True, **kwargs: Any) -> Any:
        """Retry throttling and transient provider errors with backoff; other errors propagate at once.
        With retry_timeouts=False a timeout propagates at once too."""
        for i, wait in enumerate((*TRANSIENT_BACKOFF_S, None)):
            try:
                return await fn(**kwargs)
            except Exception as e:
                name = type(e).__name__
                if wait is None or not any(t in name for t in TRANSIENT_ERRORS) or (
                        not retry_timeouts and "Timeout" in name):
                    raise
                await asyncio.sleep(wait * (1 + 0.25 * i))

    @property
    def provider(self) -> str:
        return self.settings.onus_llm_provider

    def _route(self, model: str) -> str:
        """The LiteLLM model string for a role's model id."""
        return f"gemini/{model}" if self.provider == "gemini" else f"openai/{model}"

    def _key_model(self, model: str) -> str:
        """The model as the cache key sees it: v1.0's bare id for Berget, provider-qualified otherwise."""
        return model if self.provider == "berget" else f"{self.provider}/{model}"

    def _send_params(self, model: str, params: dict[str, Any]) -> dict[str, Any]:
        """The parameters actually sent (the cache key always uses the full `params`)."""
        if self.provider == "gemini" and not gemini_takes_sampling_params(model):
            return {k: v for k, v in params.items() if k not in SAMPLING_PARAMS}
        return params

    def _long_judge(self, role: Role) -> bool:
        """A Gemini judge call: long timeout, no retry on timeout (see GEMINI_JUDGE_TIMEOUT_S_PER_FACTOR)."""
        return self.provider == "gemini" and role == "judge"

    def _common(self, role: Role | None = None) -> dict[str, Any]:
        if role is not None and self._long_judge(role):
            return {"api_key": self.settings.gemini_api_key.get_secret_value(),
                    "timeout": GEMINI_JUDGE_TIMEOUT_S_PER_FACTOR * max(1, self.settings.onus_gemini_judge_token_factor),
                    "num_retries": 0}
        if self.provider == "gemini":
            return {"api_key": self.settings.gemini_api_key.get_secret_value(), "timeout": TIMEOUT_S,
                    "num_retries": NUM_RETRIES}
        return {
            "api_base": self.settings.berget_api_base,
            "api_key": self.settings.berget_api_key.get_secret_value(),
            "timeout": TIMEOUT_S,
            "num_retries": NUM_RETRIES,
        }

    def _scrub(self, text: str) -> str:
        for secret in (self.settings.berget_api_key, self.settings.gemini_api_key):
            key = secret.get_secret_value()
            if key:
                text = text.replace(key, "***")
        return text

    def judge_reasoning_kwargs(self, prompt_id: str | None = None) -> dict[str, Any]:
        caps = self.caps.for_role("judge", self.settings.model_for("judge"))
        level = self.settings.judge_reasoning_for(prompt_id)
        return reasoning_kwargs(caps.reasoning_style, reasoning_level(self.provider, level))

    # ------------------------------------------------------------ core chat call

    async def _chat(
        self,
        role: Role,
        messages: list[dict],
        params: dict[str, Any],
        meta: CallMeta,
        *,
        run_index: int = 0,
        paper_id: str | None = None,
        use_cache: bool = True,
    ) -> dict:
        model = self.settings.model_for(role)
        key = cache_key(self._key_model(model), messages, params, run_index)
        if use_cache and (hit := self.cache.get(key)) is not None:
            self._log(role, model, meta.prompt_id, paper_id, hit["usage"], 0.0, True, 0.0)
            meta.add(hit["usage"], 0.0, True, 0.0)
            return hit

        self.guard.check(paper_id)
        t0 = time.monotonic()
        try:
            async with self._sem():
                resp = await self._with_backoff(self._acompletion, retry_timeouts=not self._long_judge(role),
                                                model=self._route(model), messages=messages,
                                                **self._send_params(model, params), **self._common(role))
            out = normalize_response(resp)
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001 - every transport/provider error becomes LLMCallError
            # failed calls are ledgered too (no usage is known; the provider may still bill them)
            self._log(role, model, meta.prompt_id, paper_id, {}, 0.0, False, time.monotonic() - t0,
                      error=type(e).__name__)
            raise LLMCallError(self._scrub(f"{type(e).__name__}: {e}")) from None
        latency = time.monotonic() - t0
        u = out["usage"]
        eur = cost_eur(model, u["prompt_tokens"], u["completion_tokens"], u["cached_tokens"])
        self._log(role, model, meta.prompt_id, paper_id, u, eur, False, latency)
        meta.add(u, eur, False, latency)
        if use_cache:
            self.cache.put(key, out)
        return out

    def _log(self, role: str, model: str, prompt_id: str, paper_id: str | None, usage: dict, eur: float, hit: bool,
             latency: float = 0.0, error: str | None = None):
        self.ledger.append(
            LedgerEntry(
                role=role, model=model, prompt_id=prompt_id, paper_id=paper_id,
                tokens_in=int(usage.get("prompt_tokens", 0)), tokens_out=int(usage.get("completion_tokens", 0)),
                cached_in=int(usage.get("cached_tokens", 0)), eur=round(eur, 8), cache_hit=hit,
                latency_s=round(latency, 2), error=error,
            )
        )

    def _judge_max_tokens(self, role: Role, max_tokens: int) -> int:
        """Gemini counts thinking against max_tokens, so its judge gets ONUS_GEMINI_JUDGE_TOKEN_FACTOR times the
        prompt's limit. Berget requests are unchanged."""
        if self.provider == "gemini" and role == "judge":
            return max_tokens * self.settings.onus_gemini_judge_token_factor
        return max_tokens

    # ------------------------------------------------------------ public API

    async def complete_json(
        self,
        prompt_id: str,
        variables: dict[str, object],
        schema: type[T],
        run_index: int = 0,
        paper_id: str | None = None,
    ) -> tuple[T, CallMeta]:
        prompt = self.prompts.get(prompt_id)
        role: Role = prompt.role  # type: ignore[assignment]
        model = self.settings.model_for(role)
        caps = self.caps.for_role(role, model)
        messages = prompt.render(variables)
        params: dict[str, Any] = {"temperature": prompt.temperature,
                                  "max_tokens": self._judge_max_tokens(role, prompt.max_tokens)}
        if role == "judge":
            params |= self.judge_reasoning_kwargs(prompt_id)
        schema_mode = caps.json_schema and not (
            self.provider == "gemini" and self.settings.onus_gemini_structured_output == "json_object")
        if schema_mode:
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(), "strict": False},
            }
        else:
            if caps.json_object:
                params["response_format"] = {"type": "json_object"}
            instruction = SCHEMA_INSTRUCTION.format(schema=json.dumps(schema.model_json_schema()))
            if messages[0]["role"] == "system":
                messages[0] = {"role": "system", "content": messages[0]["content"] + "\n\n" + instruction}
            else:
                messages.insert(0, {"role": "system", "content": instruction})

        meta = CallMeta(prompt_id=prompt_id, role=role, model=model, prompt_version=prompt.version)
        kw = {"run_index": run_index, "paper_id": paper_id}
        out = await self._chat(role, messages, params, meta, **kw)
        try:
            return parse_json_output(out["content"], schema), meta
        except (ValidationError, ValueError) as err:
            repair = messages + [
                {"role": "assistant", "content": out["content"] or ""},
                {"role": "user", "content": REPAIR_INSTRUCTION.format(error=str(err)[:2000])},
            ]
            meta.repaired = True
            out2 = await self._chat(role, repair, params, meta, **kw)
            try:
                return parse_json_output(out2["content"], schema), meta
            except (ValidationError, ValueError) as err2:
                raise LLMOutputError(f"{prompt_id}: output failed validation twice: {str(err2)[:300]}") from None

    async def complete_tools(
        self,
        role: Role,
        messages: list[dict],
        tools: list[dict],
        *,
        prompt_id: str,
        run_index: int = 0,
        paper_id: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> tuple[dict, CallMeta]:
        """One assistant turn with tools. Returns {content, tool_calls: [{id, name, arguments}], ...}."""
        model = self.settings.model_for(role)
        params: dict[str, Any] = {
            "temperature": temperature, "max_tokens": self._judge_max_tokens(role, max_tokens), "tools": tools, "tool_choice": "auto",
        }
        if role == "judge":
            params |= self.judge_reasoning_kwargs(prompt_id)
        meta = CallMeta(prompt_id=prompt_id, role=role, model=model)
        out = await self._chat(role, messages, params, meta, run_index=run_index, paper_id=paper_id)
        return out, meta

    def embed_input(self, text: str, task: Literal["document", "query"]) -> str:
        """The text the embedding model is sent for a retrieval document or query (provider-specific prefix)."""
        if self.provider == "gemini":
            return (GEMINI_QUERY_PREFIX if task == "query" else GEMINI_DOCUMENT_PREFIX) + text
        return E5_QUERY_PREFIX + text if task == "query" else text

    async def embed(self, texts: list[str], *, paper_id: str | None = None,
                    task: Literal["document", "query"] = "document") -> list[list[float]]:
        """Embed retrieval documents or queries with the embed role, cached per text."""
        texts = [self.embed_input(t, task) for t in texts]
        model = self.settings.model_for("embed")
        keys = [cache_key(self._key_model(model), [t], {}, 0) for t in texts]
        result: list[list[float] | None] = [None] * len(texts)
        todo: list[int] = []
        for i, k in enumerate(keys):
            hit = self.cache.get(k)
            if hit is not None:
                result[i] = hit["embedding"]
            else:
                todo.append(i)
        for start in range(0, len(todo), EMBED_BATCH):
            batch = todo[start : start + EMBED_BATCH]
            vecs = await self._embed_call([texts[i] for i in batch], prompt_id="embed", paper_id=paper_id)
            for i, v in zip(batch, vecs, strict=True):
                result[i] = v
                self.cache.put(keys[i], {"embedding": v})
        return [v for v in result if v is not None]

    async def _embed_call(self, texts: list[str], *, prompt_id: str, paper_id: str | None) -> list[list[float]]:
        model = self.settings.model_for("embed")
        self.guard.check(paper_id)
        try:
            async with self._sem():
                resp = await self._with_backoff(self._aembedding, model=self._route(model), input=texts,
                                                **self._common())
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001
            raise LLMCallError(self._scrub(f"{type(e).__name__}: {e}")) from None
        data = sorted(_get(resp, "data"), key=lambda d: _get(d, "index", 0))
        vecs = [list(_get(d, "embedding")) for d in data]
        usage = _get(resp, "usage")
        tokens = int(_get(usage, "prompt_tokens", 0) or _get(usage, "total_tokens", 0) or 0)
        eur = cost_eur(model, tokens, 0)
        self._log("embed", model, prompt_id, paper_id, {"prompt_tokens": tokens}, eur, False)
        return vecs

    # ------------------------------------------------------------ probe helpers (uncached)

    async def raw_chat(
        self, role: Role, *, prompt_id: str, messages: list[dict], max_tokens: int = 64,
        temperature: float = 0.0, **params: Any,
    ) -> dict:
        meta = CallMeta(prompt_id=prompt_id, role=role, model=self.settings.model_for(role))
        p = {"temperature": temperature, "max_tokens": max_tokens, **params}
        return await self._chat(role, messages, p, meta, use_cache=False)

    async def raw_embed(self, texts: list[str], *, prompt_id: str) -> list[list[float]]:
        return await self._embed_call(texts, prompt_id=prompt_id, paper_id=None)

    async def list_models(self) -> list[str]:
        import httpx

        if self.provider == "gemini":
            headers = {"x-goog-api-key": self.settings.gemini_api_key.get_secret_value()}
            async with httpx.AsyncClient(timeout=30) as http:
                r = await http.get("https://generativelanguage.googleapis.com/v1beta/models",
                                   params={"pageSize": 1000}, headers=headers)
                r.raise_for_status()
                return sorted(m["name"].removeprefix("models/") for m in r.json().get("models", []))
        headers = {"Authorization": f"Bearer {self.settings.berget_api_key.get_secret_value()}"}
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.get(self.settings.berget_api_base.rstrip("/") + "/models", headers=headers)
            r.raise_for_status()
            return sorted(m["id"] for m in r.json().get("data", []))


_client: LLMClient | None = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def set_client(client: LLMClient | None) -> None:
    """Install a client (e.g. FakeLLM in tests, or a live-mode client in the API)."""
    global _client
    _client = client
