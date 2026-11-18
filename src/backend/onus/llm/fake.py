"""`FakeLLM`: a drop-in for `LLMClient` in tests. No network, no cost, no cache.

    fake = FakeLLM({"extract_claims": {"items": [...]}, "adjudicate": lambda vars, run: {...}})
    set_client(fake)

A response may be a dict, a list of dicts (returned in turn; the last one repeats), a callable
`(variables, run_index) -> dict`, or an exception instance to raise. Output that fails the schema raises
`LLMOutputError`, as the real client does after its repair call. Embeddings are deterministic hashed
bags of words, so similar texts get similar vectors.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from onus.llm.client import CallMeta, LLMOutputError
from onus.llm.prompts import PromptError, PromptRegistry

T = TypeVar("T", bound=BaseModel)
Response = dict | list | Callable[[dict, int], dict] | BaseException


class FakeLLM:
    def __init__(
        self,
        responses: dict[str, Response] | None = None,
        *,
        embed_dim: int = 64,
        prompts: PromptRegistry | None = None,
        strict: bool = True,
    ):
        self.responses: dict[str, Response] = dict(responses or {})
        self.embed_dim = embed_dim
        self.prompts = prompts
        self.strict = strict
        self.calls: list[dict[str, Any]] = []
        self._turn: dict[str, int] = {}

    def _next(self, prompt_id: str, variables: dict, run_index: int) -> dict:
        if prompt_id not in self.responses:
            if self.strict:
                raise KeyError(f"FakeLLM has no response for prompt {prompt_id!r}")
            raise LLMOutputError(f"{prompt_id}: no fake response")
        r = self.responses[prompt_id]
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            return r(variables, run_index)
        if isinstance(r, list):
            i = self._turn.get(prompt_id, 0)
            self._turn[prompt_id] = i + 1
            return r[min(i, len(r) - 1)]
        return r

    def _meta(self, prompt_id: str, role: str = "fake") -> CallMeta:
        m = CallMeta(prompt_id=prompt_id, role=role, model="fake")
        m.calls = 1
        return m

    async def complete_json(
        self,
        prompt_id: str,
        variables: dict[str, object],
        schema: type[T],
        run_index: int = 0,
        paper_id: str | None = None,
    ) -> tuple[T, CallMeta]:
        role = "fake"
        if self.prompts is not None:  # check the variables against the real template when available
            try:
                prompt = self.prompts.get(prompt_id)
                prompt.render(variables)
                role = prompt.role
            except PromptError as e:
                if "No prompt file" not in str(e):
                    raise
        self.calls.append({"prompt_id": prompt_id, "variables": variables, "run_index": run_index,
                           "paper_id": paper_id})
        raw = self._next(prompt_id, dict(variables), run_index)
        try:
            return schema.model_validate(raw), self._meta(prompt_id, role)
        except ValidationError as e:
            raise LLMOutputError(f"{prompt_id}: fake output failed validation: {e}") from None

    async def complete_tools(
        self, role: str, messages: list[dict], tools: list[dict], *, prompt_id: str, run_index: int = 0,
        paper_id: str | None = None, temperature: float = 0.0, max_tokens: int = 2048,
    ) -> tuple[dict, CallMeta]:
        self.calls.append({"prompt_id": prompt_id, "messages": messages, "run_index": run_index})
        out = self._next(prompt_id, {"messages": messages}, run_index)
        return {"content": None, "tool_calls": [], **out}, self._meta(prompt_id, role)

    async def embed(self, texts: list[str], *, paper_id: str | None = None, task: str = "document"
                    ) -> list[list[float]]:
        if task == "query":  # the e5 query instruction, as the real Berget client sends it
            from onus.llm.client import E5_QUERY_PREFIX

            texts = [E5_QUERY_PREFIX + t for t in texts]
        return [hashed_embedding(t, self.embed_dim) for t in texts]


def hashed_embedding(text: str, dim: int) -> list[float]:
    v = [0.0] * dim
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:4], "little")
        v[h % dim] += 1.0 if (h >> 31) & 1 == 0 else -1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]
