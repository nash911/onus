"""What each role's model supports, as found by `onus llm probe`, and how the client uses it.

One file per provider (`ONUS_LLM_PROVIDER`): the probe writes `$ONUS_VAR_DIR/<provider>_capabilities.json`
(read at runtime) and `docs/<provider>_capabilities.md` (for the author). Without a probe file the client uses
safe defaults: `json_object` responses with the schema in the system message, and no reasoning parameter.

Reasoning: Berget's judge takes one of three parameter styles (found by the probe). Gemini models think by
default; LiteLLM maps `reasoning_effort` (low / medium / high) onto Gemini's thinking setting (thinking level
on Gemini 3 and later, a thinking budget on 2.5), so the judge's ONUS_JUDGE_REASONING goes there.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from onus.config import CHAT_ROLES, Settings, get_settings

CAPS_FILE = "berget_capabilities.json"  # the Berget file keeps its v1.0 name

REASONING_STYLES = ("reasoning_effort", "extra_body.reasoning_effort", "extra_body.reasoning")
PROVIDER_REASONING_STYLES = {"berget": REASONING_STYLES, "gemini": ("reasoning_effort",)}
PROVIDER_TITLES = {"berget": "Berget", "gemini": "Gemini"}


def caps_file(provider: str = "berget") -> str:
    return f"{provider}_capabilities.json"


def reasoning_level(provider: str, level: str) -> str:
    """ONUS_JUDGE_REASONING for the provider: Gemini's thinking levels stop at high, so "max" maps to high."""
    return "high" if provider == "gemini" and level == "max" else level


def reasoning_kwargs(style: str | None, level: str) -> dict[str, Any]:
    if style == "reasoning_effort":
        return {"reasoning_effort": level}
    if style == "extra_body.reasoning_effort":
        return {"extra_body": {"reasoning_effort": level}}
    if style == "extra_body.reasoning":
        return {"extra_body": {"reasoning": {"effort": level}}}
    return {}


class RoleCaps(BaseModel):
    model: str
    chat: bool = True
    json_schema: bool = False
    json_object: bool = True
    tools: bool | None = None
    cached_tokens_reported: bool | None = None
    reasoning_style: str | None = None
    embedding: bool | None = None
    embedding_dim: int | None = None
    notes: list[str] = Field(default_factory=list)


class Capabilities(BaseModel):
    probed_at: str | None = None
    models_listed: list[str] = Field(default_factory=list)
    roles: dict[str, RoleCaps] = Field(default_factory=dict)

    def for_role(self, role: str, model: str) -> RoleCaps:
        caps = self.roles.get(role)
        if caps is None or caps.model != model:  # unprobed or the role's model was swapped: defaults
            return RoleCaps(model=model)
        return caps

    @classmethod
    def load(cls, var_dir: Path, provider: str = "berget") -> Capabilities:
        p = var_dir / caps_file(provider)
        if p.is_file():
            return cls.model_validate_json(p.read_text())
        return cls()


# ---------------------------------------------------------------- probe

PROBE_MAX_EUR = 0.05

_PROBE_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
    "required": ["city", "country"],
    "additionalProperties": False,
}
_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_population",
        "description": "Return the population of a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}
_EFFORT_QUESTION = [{
    "role": "user",
    "content": "A bat and a ball cost 1.10 in total. The bat costs 1.00 more than the ball. A second bat costs "
    "twice the first bat. What do two bats and three balls cost? Give only the number.",
}]
# A long, fixed prefix, sent twice, to see whether usage reports cached prompt tokens.
_CACHE_PREFIX = " ".join(
    f"Reference item {i}: the quick brown fox jumps over the lazy dog near river {i % 17}." for i in range(75)
)


def _valid_city_json(text: str | None) -> bool:
    if not text:
        return False
    t = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        d = json.loads(t)
    except json.JSONDecodeError:
        return False
    return isinstance(d, dict) and isinstance(d.get("city"), str) and isinstance(d.get("country"), str)


async def probe(settings: Settings | None = None, client: Any = None) -> tuple[Capabilities, list[dict]]:
    """Probe every role's model. Returns the capabilities and a list of per-test rows for the report."""
    from onus.llm.client import LLMCallError, get_client

    s = settings or get_settings()
    c = client or get_client()
    provider = s.onus_llm_provider
    rows: list[dict] = []
    caps = Capabilities(probed_at=datetime.now(UTC).isoformat())

    try:
        caps.models_listed = await c.list_models()
    except Exception as e:  # noqa: BLE001 - listing is informational
        rows.append({"role": "-", "test": "list models", "ok": False, "note": type(e).__name__})

    def record(role: str, test: str, ok: bool | None, note: str = "", t0: float | None = None) -> None:
        secs = f"{time.monotonic() - t0:.1f}s" if t0 is not None else ""
        rows.append({"role": role, "test": test, "ok": ok, "note": note, "time": secs})

    city_msgs = [
        {"role": "system", "content": "Reply with JSON only."},
        {"role": "user", "content": 'Give the capital of France as {"city": ..., "country": ...}.'},
    ]

    start_eur = c.ledger.total_eur

    for role in CHAT_ROLES:
        model = s.model_for(role)
        rc = RoleCaps(model=model)
        # Reasoning models may spend output tokens thinking before they answer; give them room so a
        # capability is not misreported as missing because the reply was cut off.
        room = 1000 if provider == "gemini" else (600 if role == "judge" else 160)
        if caps.models_listed and model not in caps.models_listed:
            rc.notes.append("model id not in the provider's model list")

        async def call(test: str, role: str = role, **kw: Any) -> dict | None:
            if c.ledger.total_eur - start_eur > PROBE_MAX_EUR:
                record(role, test, None, f"skipped: probe spend passed €{PROBE_MAX_EUR}")
                return None
            t0 = time.monotonic()
            try:
                r = await c.raw_chat(role, prompt_id=f"probe:{test}", **kw)
                record(role, test, True, _usage_note(r), t0)
                return r
            except LLMCallError as e:
                record(role, test, False, e.short, t0)
                return None

        # 1. plain chat
        r = await call("chat", messages=[{"role": "user", "content": "Say OK."}], max_tokens=room)
        rc.chat = r is not None and bool((r.get("content") or "").strip() or r.get("reasoning_content"))

        # 2. json_schema
        rf = {"type": "json_schema", "json_schema": {"name": "city", "schema": _PROBE_SCHEMA, "strict": False}}
        r = await call("json_schema", messages=city_msgs, max_tokens=room, response_format=rf)
        rc.json_schema = r is not None and _valid_city_json(r.get("content"))
        if r is not None and not rc.json_schema:
            rows[-1]["ok"] = False
            rows[-1]["note"] = "accepted but output was not valid JSON for the schema"

        # 3. json_object
        r = await call("json_object", messages=city_msgs, max_tokens=room, response_format={"type": "json_object"})
        rc.json_object = r is not None and _valid_city_json(r.get("content"))
        if r is not None and not rc.json_object:
            rows[-1]["ok"] = False
            rows[-1]["note"] = "accepted but output was not valid JSON"

        # 4. tool calling (needed by the judge for the re-run agent; probed on every role, it is cheap)
        r = await call(
            "tools",
            messages=[{"role": "user", "content": "What is the population of Lyon? Use the tool."}],
            max_tokens=room,
            tools=[_PROBE_TOOL],
            tool_choice="auto",
        )
        rc.tools = r is not None and any(tc.get("name") == "get_population" for tc in r.get("tool_calls") or [])
        if r is not None and not rc.tools:
            rows[-1]["ok"] = False
            rows[-1]["note"] = "accepted but no tool call returned"

        # 5. cached-token reporting: the same long prefix twice
        msgs = [{"role": "system", "content": _CACHE_PREFIX}, {"role": "user", "content": "Reply with OK."}]
        await call("cache_warm", messages=msgs, max_tokens=room)
        r = await call("cache_repeat", messages=msgs, max_tokens=room)
        rc.cached_tokens_reported = bool(r and r["usage"].get("cached_tokens"))

        # 6. reasoning parameter (judge only), at the cheapest level. A provider may silently ignore an
        #    unknown parameter, so try every style and prefer one whose reasoning is visible in the reply.
        if role == "judge":
            accepted: list[tuple[str, bool]] = []
            for style in PROVIDER_REASONING_STYLES[provider]:
                r = await call(
                    f"reasoning:{style}",
                    messages=[{"role": "user", "content": "Is 17 prime? Answer yes or no."}],
                    max_tokens=512,
                    **reasoning_kwargs(style, "low"),
                )
                if r is not None:
                    shows = bool(r["usage"].get("reasoning_tokens") or r.get("reasoning_content"))
                    rows[-1]["note"] += "; reasoning visible" if shows else "; no reasoning reported"
                    accepted.append((style, shows))
            visible = [st for st, shows in accepted if shows]
            rc.reasoning_style = (visible or [st for st, _ in accepted] or [None])[0]
            if accepted and not visible:
                rc.notes.append("reasoning parameter accepted but no reasoning reported; it may be ignored")
            if rc.reasoning_style:  # does the level change anything? compare low and high on one question
                tokens = {}
                for level in ("low", "high"):
                    r = await call(f"effort:{level}", messages=_EFFORT_QUESTION, max_tokens=1500,
                                   **reasoning_kwargs(rc.reasoning_style, level))
                    tokens[level] = r["usage"].get("reasoning_tokens", 0) if r else None
                if tokens["low"] is not None and tokens["high"] is not None:
                    honoured = tokens["high"] > tokens["low"]
                    rc.notes.append(
                        f"reasoning tokens at low/high: {tokens['low']}/{tokens['high']}: level "
                        + ("honoured" if honoured else "has no visible effect")
                    )
            if rc.reasoning_style is None:
                rc.notes.append("no reasoning parameter accepted; judge runs without one")
        caps.roles[role] = rc

    # embeddings
    model = s.model_for("embed")
    rc = RoleCaps(model=model, chat=False, json_object=False)
    t0 = time.monotonic()
    try:
        vecs = await c.raw_embed([c.embed_input("probe", "query"), c.embed_input("probe", "document")]
                                 if hasattr(c, "embed_input") else ["query: probe", "passage: probe"],
                                 prompt_id="probe:embed")
        rc.embedding, rc.embedding_dim = True, len(vecs[0])
        record("embed", "embedding", True, f"dim {rc.embedding_dim}", t0)
    except LLMCallError as e:
        rc.embedding = False
        record("embed", "embedding", False, e.short, t0)
    caps.roles["embed"] = rc
    return caps, rows


def _usage_note(r: dict) -> str:
    u = r.get("usage", {})
    note = f"in {u.get('prompt_tokens', 0)} / out {u.get('completion_tokens', 0)}"
    if u.get("cached_tokens"):
        note += f" / cached {u['cached_tokens']}"
    if u.get("reasoning_tokens"):
        note += f" / reasoning {u['reasoning_tokens']}"
    return note


def write_probe_outputs(caps: Capabilities, rows: list[dict], var_dir: Path, docs_dir: Path, eur: float,
                        provider: str = "berget") -> None:
    var_dir.mkdir(parents=True, exist_ok=True)
    (var_dir / caps_file(provider)).write_text(caps.model_dump_json(indent=2) + "\n")
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / f"{provider}_capabilities.md").write_text(render_markdown(caps, rows, eur, provider))


def _yn(v: bool | None) -> str:
    return "—" if v is None else ("yes" if v else "no")


def render_markdown(caps: Capabilities, rows: list[dict], eur: float, provider: str = "berget") -> str:
    listing = "GET /v1/models" if provider == "berget" else "the provider's model list"
    out = [
        f"# {PROVIDER_TITLES.get(provider, provider)} capabilities",
        "",
        f"Probed {caps.probed_at} by `onus llm probe`. Probe cost: €{eur:.4f}.",
        f"Runtime copy: `$ONUS_VAR_DIR/{caps_file(provider)}`.",
        "",
        "## Summary",
        "",
        "| Role | Model | Chat | json_schema | json_object | Tools | Cached tokens reported | Reasoning param |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for role in CHAT_ROLES:
        rc = caps.roles.get(role)
        if rc:
            out.append(
                f"| {role} | `{rc.model}` | {_yn(rc.chat)} | {_yn(rc.json_schema)} | {_yn(rc.json_object)} | "
                f"{_yn(rc.tools)} | {_yn(rc.cached_tokens_reported)} | "
                f"{('`' + rc.reasoning_style + '`') if rc.reasoning_style else '—'} |"
            )
    e = caps.roles.get("embed")
    if e:
        out += ["", f"Embeddings (`{e.model}`): {_yn(e.embedding)}, dimension {e.embedding_dim or '—'}."]
    out += ["", "## How the client uses this", ""]
    for role in CHAT_ROLES:
        rc = caps.roles.get(role)
        if rc:
            mode = "`json_schema` (strict: false)" if rc.json_schema else "`json_object` + schema in system message"
            out.append(f"- **{role}**: structured output via {mode}.")
    notes = [(r, n) for r, rc in caps.roles.items() for n in rc.notes]
    if notes:
        out += ["", "## Notes", ""] + [f"- {r}: {n}" for r, n in notes]
    if caps.models_listed:
        out += ["", f"## Models listed by {listing}", ""] + [f"- `{m}`" for m in caps.models_listed]
    out += ["", "## Test log", "", "| Role | Test | OK | Time | Note |", "| --- | --- | --- | --- | --- |"]
    for r in rows:
        out.append(f"| {r['role']} | {r['test']} | {_yn(r['ok'])} | {r.get('time', '')} | {r['note']} |")
    return "\n".join(out) + "\n"
