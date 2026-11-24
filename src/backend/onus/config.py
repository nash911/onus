"""Settings, price table and path guards.

Settings are read from the environment and from `<repo>/.env` (pydantic-settings). Secrets are
`SecretStr`, so they never appear in reprs or logs.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Role = Literal["extract", "judge", "fast", "embed"]
Provider = Literal["berget", "gemini"]
PROVIDERS: tuple[Provider, ...] = ("berget", "gemini")
ROLES: tuple[Role, ...] = ("extract", "judge", "fast", "embed")
CHAT_ROLES: tuple[Role, ...] = ("extract", "judge", "fast")


def repo_root() -> Path:
    """The repository root: the nearest ancestor of this file holding `pyproject.toml`."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("Could not locate the repository root (no pyproject.toml above onus/config.py)")


# EUR per million tokens: (input, cached input, output). From Berget's model list, 14 Nov 2026.
# Cached input falls back to the input price when Berget lists none.
PRICES_EUR_PER_M: dict[str, tuple[float, float | None, float]] = {
    "moonshotai/Kimi-K3.1-Pro": (3.00, 0.90, 10.00),
    "zai-org/GLM-5.3-Flash": (0.25, None, 0.50),
    "google/gemma-4-31B-it": (0.25, None, 0.50),
    "mistralai/Mistral-Small-3.2-24B-Instruct-2506": (0.30, None, 0.30),
    "intfloat/multilingual-e5-large-instruct": (0.03, None, 0.02),
}

# Gemini (ONUS_LLM_PROVIDER=gemini), from Google's Gemini API price list (paid tier, standard, not batch), read
# 18 Nov 2026 at https://aistudio.google.com/docs/pricing. USD per million tokens: (input, cached input, output;
# output includes thinking tokens). None of these four models lists a long-context (>200k) tier; a model that
# does goes in GEMINI_LONG_CONTEXT_USD_PER_M. gemini-4-carbon's list price doubles on 1 Dec 2026 (to 4.00 /
# 0.20 / 20.00); re-check before a run after that date.
USD_TO_EUR = 0.891  # 1 / 1.1224, the ECB EUR/USD reference rate of 5 Nov 2026 (latest found on 18 Nov 2026)
GEMINI_PRICES_USD_PER_M: dict[str, tuple[float, float | None, float]] = {
    "gemini-4-carbon": (2.00, 0.10, 10.00),
    "gemini-3.8-flash": (0.75, 0.075, 3.75),
    "gemini-3.5-flash-lite": (0.30, 0.03, 2.50),
    "gemini-embedding-2": (0.20, None, 0.0),
}
# model -> (threshold in prompt tokens, (input, cached input, output) above it), USD per million tokens
GEMINI_LONG_CONTEXT_USD_PER_M: dict[str, tuple[int, tuple[float, float | None, float]]] = {}


def _eur(t: tuple[float, float | None, float]) -> tuple[float, float | None, float]:
    return (round(t[0] * USD_TO_EUR, 6), None if t[1] is None else round(t[1] * USD_TO_EUR, 6),
            round(t[2] * USD_TO_EUR, 6))


PRICES_EUR_PER_M.update({m: _eur(p) for m, p in GEMINI_PRICES_USD_PER_M.items()})
LONG_CONTEXT_EUR_PER_M: dict[str, tuple[int, tuple[float, float | None, float]]] = {
    m: (n, _eur(p)) for m, (n, p) in GEMINI_LONG_CONTEXT_USD_PER_M.items()
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(repo_root() / ".env"), env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # Berget AI
    berget_api_key: SecretStr = SecretStr("")
    berget_api_base: str = "https://api.berget.ai/v1"

    # Provider for every model role: berget (v1.0.x, the default) or gemini (v1.1)
    onus_llm_provider: Provider = "berget"
    gemini_api_key: SecretStr = SecretStr("")  # Onus's own key; never SANDBOX_GEMINI_API_KEY

    # Model roles (Berget)
    onus_model_extract: str = "zai-org/GLM-5.3-Flash"
    onus_model_judge: str = "moonshotai/Kimi-K3.1-Pro"
    onus_model_fast: str = "google/gemma-4-31B-it"
    onus_model_embed: str = "intfloat/multilingual-e5-large-instruct"
    onus_judge_reasoning: Literal["low", "high", "max"] = "high"

    # Model roles (Gemini): pinned, generally available ids (docs/gemini_capabilities.md says why)
    onus_gemini_model_extract: str = "gemini-3.8-flash"
    onus_gemini_model_judge: str = "gemini-4-carbon"
    onus_gemini_model_fast: str = "gemini-3.5-flash-lite"
    onus_gemini_model_embed: str = "gemini-embedding-2"
    # The Gemini judge's thinking level. Gemini counts thinking tokens against max_tokens, so judge calls on Gemini
    # get ONUS_GEMINI_JUDGE_TOKEN_FACTOR times the prompt's max_tokens (at 1x, 113 judge calls on dev ran out of
    # tokens while thinking: adjudicate, scope_conflict, baseline_support, impl_match).
    onus_gemini_judge_reasoning: Literal["low", "medium", "high"] = "high"
    onus_gemini_judge_token_factor: int = 4
    # Per-prompt thinking levels for the Gemini judge, "prompt:level,prompt:level". At "high" with the 4x budget these
    # prompts thought until their limit or the timeout on dev: scope_conflict used all 32,000 tokens on 8 of 15 calls,
    # baseline_support passed 20 minutes on 16 of 16, impl_match used all 24,000 tokens on 2 of 8. They run at "medium".
    onus_gemini_judge_reasoning_overrides: str = "scope_conflict:medium,baseline_support:medium,impl_match:medium"
    # Structured output on Gemini: "json_object" sends the schema in the system message (as for Berget models
    # without json_schema). Gemini's schema-constrained mode dropped optional fields (is_main) and pushed the number
    # mapper to "not_in_tables" on dev, so json_object is the default.
    onus_gemini_structured_output: Literal["json_object", "json_schema"] = "json_object"

    # Model calls in flight at once per process (does not change outputs, so not in the fingerprint)
    onus_llm_concurrency: int = 8

    # Spend guards (EUR)
    onus_dev_budget_eur: float = 25.0
    onus_max_eur_per_paper: float = 1.50

    # Paths and storage
    onus_data_root: Path = Path("./data/dev")
    onus_var_dir: Path = Path("./var")
    database_url: str = "sqlite:///./var/onus.db"
    onus_results_import_dir: Path = Path("./deploy_data")

    # Live analysis limits
    live_max_per_day: int = 40
    live_max_per_ip_per_hour: int = 5
    live_max_cost_eur_per_day: float = 10.0
    live_max_pdf_mb: int = 20
    featured_papers: str = ""

    # Modal
    modal_token_id: SecretStr = SecretStr("")
    modal_token_secret: SecretStr = SecretStr("")
    onus_modal_app: str = "onus-rerun"
    onus_rerun_max_minutes: int = 30

    # Dedicated low-limit keys for the paper code's own model calls in re-runs (never used by Onus)
    sandbox_openai_api_key: SecretStr = SecretStr("")
    sandbox_anthropic_api_key: SecretStr = SecretStr("")
    sandbox_gemini_api_key: SecretStr = SecretStr("")

    # ---- derived ----
    def model_for(self, role: Role) -> str:
        if self.onus_llm_provider == "gemini":
            return {
                "extract": self.onus_gemini_model_extract,
                "judge": self.onus_gemini_model_judge,
                "fast": self.onus_gemini_model_fast,
                "embed": self.onus_gemini_model_embed,
            }[role]
        return {
            "extract": self.onus_model_extract,
            "judge": self.onus_model_judge,
            "fast": self.onus_model_fast,
            "embed": self.onus_model_embed,
        }[role]

    @property
    def models(self) -> dict[str, str]:
        return {role: self.model_for(role) for role in ROLES}

    @property
    def models_config(self) -> dict[str, str]:
        """Everything about the models that changes outputs; part of the fingerprint.

        Berget keeps the v1.0 shape (no provider key), so a Berget configuration hashes as it did before."""
        if self.onus_llm_provider == "berget":
            return {**self.models, "judge_reasoning": self.onus_judge_reasoning}
        return {"provider": self.onus_llm_provider, **self.models, "judge_reasoning": self.judge_reasoning,
                "structured_output": self.onus_gemini_structured_output,
                "judge_token_factor": str(self.onus_gemini_judge_token_factor),
                "judge_reasoning_overrides": ",".join(f"{k}:{v}" for k, v in
                                                      sorted(self.gemini_judge_reasoning_overrides.items()))}

    @property
    def gemini_judge_reasoning_overrides(self) -> dict[str, str]:
        """ONUS_GEMINI_JUDGE_REASONING_OVERRIDES as {prompt_id: level}; levels must be low, medium or high."""
        out: dict[str, str] = {}
        for item in self.onus_gemini_judge_reasoning_overrides.split(","):
            if not item.strip():
                continue
            prompt, sep, level = item.partition(":")
            if not sep or level.strip() not in ("low", "medium", "high") or not prompt.strip():
                raise ValueError(f"ONUS_GEMINI_JUDGE_REASONING_OVERRIDES: bad entry {item!r} (want prompt:low|medium|high)")
            out[prompt.strip()] = level.strip()
        return out

    def judge_reasoning_for(self, prompt_id: str | None = None) -> str:
        """The judge's reasoning level for one prompt: a Gemini per-prompt override, else the provider's level."""
        if self.onus_llm_provider == "gemini" and prompt_id:
            return self.gemini_judge_reasoning_overrides.get(prompt_id, self.judge_reasoning)
        return self.judge_reasoning

    @property
    def judge_reasoning(self) -> str:
        """The judge's reasoning level for the configured provider."""
        return self.onus_gemini_judge_reasoning if self.onus_llm_provider == "gemini" else self.onus_judge_reasoning

    @property
    def featured_paper_ids(self) -> list[str]:
        return [p.strip() for p in self.featured_papers.split(",") if p.strip()]

    def resolve(self, p: Path) -> Path:
        """Resolve a configured path; relative paths are relative to the repo root."""
        return (p if p.is_absolute() else repo_root() / p).expanduser().resolve()

    @property
    def data_root(self) -> Path:
        return self.resolve(self.onus_data_root)

    @property
    def var_dir(self) -> Path:
        return self.resolve(self.onus_var_dir)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def prompts_dir() -> Path:
    return repo_root() / "prompts"


def rubrics_dir() -> Path:
    return repo_root() / "rubrics"


# ---------------------------------------------------------------- Modal auth


def ensure_modal_auth(settings: Settings | None = None) -> bool:
    """Copy Modal tokens from settings into the process environment, which is all the Modal client reads.

    Call before any `import modal`. Values already in the environment win. Returns True when both are
    available. Never prints the values.
    """
    s = settings or get_settings()
    for env_name, value in (
        ("MODAL_TOKEN_ID", s.modal_token_id),
        ("MODAL_TOKEN_SECRET", s.modal_token_secret),
    ):
        if not os.environ.get(env_name) and value.get_secret_value():
            os.environ[env_name] = value.get_secret_value()
    return bool(os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"))


# ---------------------------------------------------------------- path guards


class UnsafePathsError(RuntimeError):
    """A held-out run would write paper text, cache or ledger into the repository."""


def is_within(path: Path, root: Path) -> bool:
    path, root = Path(path).expanduser().resolve(), Path(root).expanduser().resolve()
    return path == root or root in path.parents


def assert_safe_paths(data_root: Path, var_dir: Path, *, extra_outputs: tuple[Path, ...] = ()) -> None:
    """Hard rule 4: a data root outside the repo (held-out) requires every output outside the repo.

    `var_dir` holds the LLM cache (which contains paper text), the cost ledger and scratch files;
    `extra_outputs` are any other output directories of the command (e.g. `--out`).
    """
    root = repo_root()
    if is_within(data_root, root):
        return
    offenders = [p for p in (var_dir, *extra_outputs) if is_within(p, root)]
    if offenders:
        names = ", ".join(str(p) for p in offenders)
        raise UnsafePathsError(
            f"Data root {data_root} is outside the repository (held-out), but these outputs are inside it: "
            f"{names}. Set ONUS_VAR_DIR (and --out) to a directory outside the repository."
        )
