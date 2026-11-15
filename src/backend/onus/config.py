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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(repo_root() / ".env"), env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # Berget AI
    berget_api_key: SecretStr = SecretStr("")
    berget_api_base: str = "https://api.berget.ai/v1"

    # Model roles
    onus_model_extract: str = "zai-org/GLM-5.3-Flash"
    onus_model_judge: str = "moonshotai/Kimi-K3.1-Pro"
    onus_model_fast: str = "google/gemma-4-31B-it"
    onus_model_embed: str = "intfloat/multilingual-e5-large-instruct"
    onus_judge_reasoning: Literal["low", "high", "max"] = "high"

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
        """Everything about the models that changes outputs; part of the fingerprint."""
        return {**self.models, "judge_reasoning": self.onus_judge_reasoning}

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
