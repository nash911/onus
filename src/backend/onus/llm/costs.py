"""Cost maths, the cost ledger and the budget guard.

Ledger: `$ONUS_VAR_DIR/cost_ledger.jsonl`, one line per call:
`{ts, role, model, prompt_id, paper_id, tokens_in, tokens_out, cached_in, eur, cache_hit}`.
Cache hits are logged with eur = 0 so call counts stay honest.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from onus.config import LONG_CONTEXT_EUR_PER_M, PRICES_EUR_PER_M


class BudgetExceeded(RuntimeError):
    """Stops a run: dev budget or per-paper cap reached."""


class UnknownModelPrice(KeyError):
    pass


def cost_eur(model: str, tokens_in: int, tokens_out: int, cached_in: int = 0) -> float:
    """EUR for one call. `tokens_in` includes `cached_in`; cached tokens are billed at the cached rate."""
    if model not in PRICES_EUR_PER_M:
        raise UnknownModelPrice(f"No price for model {model!r}; add it to PRICES_EUR_PER_M in config.py")
    p_in, p_cached, p_out = PRICES_EUR_PER_M[model]
    if model in LONG_CONTEXT_EUR_PER_M:  # the whole call is billed at the long-context rate past the threshold
        threshold, long_prices = LONG_CONTEXT_EUR_PER_M[model]
        if tokens_in > threshold:
            p_in, p_cached, p_out = long_prices
    cached_in = max(0, min(cached_in, tokens_in))
    p_cached = p_in if p_cached is None else p_cached
    return ((tokens_in - cached_in) * p_in + cached_in * p_cached + tokens_out * p_out) / 1_000_000


@dataclass
class LedgerEntry:
    role: str
    model: str
    prompt_id: str
    paper_id: str | None
    tokens_in: int
    tokens_out: int
    cached_in: int
    eur: float
    cache_hit: bool
    ts: str = ""
    latency_s: float = 0.0  # wall time of the provider call (0 for cache hits)

    def to_json(self) -> str:
        d = self.__dict__.copy()
        d["ts"] = d["ts"] or datetime.now(UTC).isoformat()
        return json.dumps(d, ensure_ascii=False)


class CostLedger:
    """Append-only JSONL ledger with running totals kept in memory.

    `total_eur` is cumulative over the whole ledger file (the dev budget). `paper_eur` counts only calls
    made by this process, so the per-paper cap applies to one analysis, not to every dev re-run of a paper;
    `reset_paper()` starts a fresh count.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.total_eur = 0.0
        self.paper_eur: dict[str, float] = defaultdict(float)
        if path.is_file():
            self.total_eur = sum(float(r.get("eur", 0.0)) for r in self.rows())

    def _add(self, paper_id: str | None, eur: float) -> None:
        self.total_eur += eur
        if paper_id:
            self.paper_eur[paper_id] += eur

    def reset_paper(self, paper_id: str) -> None:
        with self._lock:
            self.paper_eur.pop(paper_id, None)

    def append(self, entry: LedgerEntry) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(entry.to_json() + "\n")
            self._add(entry.paper_id, entry.eur)

    def rows(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def summary(self) -> dict:
        """Totals by role, model, prompt and paper: {key: {eur, calls, cache_hits, tokens_in, tokens_out}}."""
        groups: dict[str, dict[str, dict[str, float]]] = {g: {} for g in ("role", "model", "prompt_id", "paper_id")}
        total = {"eur": 0.0, "calls": 0, "cache_hits": 0, "tokens_in": 0, "tokens_out": 0}
        for r in self.rows():
            for acc in [total] + [
                groups[g].setdefault(str(r.get(g)), {k: 0 for k in total}) for g in groups
            ]:
                acc["eur"] += float(r.get("eur", 0.0))
                acc["calls"] += 1
                acc["cache_hits"] += int(bool(r.get("cache_hit")))
                acc["tokens_in"] += int(r.get("tokens_in", 0))
                acc["tokens_out"] += int(r.get("tokens_out", 0))
        return {"total": total, **groups}


class BudgetGuard:
    """Raises BudgetExceeded before a call when the dev budget or a paper's cap is already reached.

    `dev_budget_eur=None` disables the cumulative cap (live mode uses daily caps in the API instead).
    """

    def __init__(self, ledger: CostLedger, dev_budget_eur: float | None, max_eur_per_paper: float | None):
        self.ledger = ledger
        self.dev_budget_eur = dev_budget_eur
        self.max_eur_per_paper = max_eur_per_paper

    def check(self, paper_id: str | None) -> None:
        if self.dev_budget_eur is not None and self.ledger.total_eur >= self.dev_budget_eur:
            raise BudgetExceeded(
                f"Budget reached: €{self.ledger.total_eur:.4f} spent of ONUS_DEV_BUDGET_EUR="
                f"€{self.dev_budget_eur:.2f} (ledger {self.ledger.path})"
            )
        if paper_id and self.max_eur_per_paper is not None:
            spent = self.ledger.paper_eur.get(paper_id, 0.0)
            if spent >= self.max_eur_per_paper:
                raise BudgetExceeded(
                    f"Per-paper cap reached for {paper_id}: €{spent:.4f} of ONUS_MAX_EUR_PER_PAPER="
                    f"€{self.max_eur_per_paper:.2f}"
                )
