from __future__ import annotations

import json

import pytest

from onus.llm.costs import BudgetExceeded, BudgetGuard, CostLedger, LedgerEntry, UnknownModelPrice, cost_eur


def entry(eur: float, paper: str | None = "p1", hit: bool = False, role: str = "extract") -> LedgerEntry:
    return LedgerEntry(role=role, model="zai-org/GLM-5.3-Flash", prompt_id="x", paper_id=paper, tokens_in=10,
                       tokens_out=5, cached_in=0, eur=eur, cache_hit=hit)


def test_cost_maths():
    # GLM-5.3-Flash: 0.25 in / 0.50 out per million
    assert cost_eur("zai-org/GLM-5.3-Flash", 1_000_000, 1_000_000) == pytest.approx(0.75)
    # Kimi: 3.00 in, 0.90 cached, 10.00 out; 1M in of which 400k cached, 100k out
    assert cost_eur("moonshotai/Kimi-K3.1-Pro", 1_000_000, 100_000, 400_000) == pytest.approx(
        (600_000 * 3.0 + 400_000 * 0.9 + 100_000 * 10.0) / 1e6
    )
    # no cached price listed -> cached tokens billed at the input rate
    assert cost_eur("google/gemma-4-31B-it", 1000, 0, 1000) == pytest.approx(1000 * 0.25 / 1e6)
    # cached can never exceed input
    assert cost_eur("moonshotai/Kimi-K3.1-Pro", 100, 0, 500) == pytest.approx(100 * 0.9 / 1e6)
    assert cost_eur("intfloat/multilingual-e5-large-instruct", 2_000_000, 0) == pytest.approx(0.06)


def test_unknown_model_price():
    with pytest.raises(UnknownModelPrice):
        cost_eur("someone/unknown", 1, 1)


def test_ledger_appends_and_totals(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = CostLedger(path)
    led.append(entry(0.10))
    led.append(entry(0.05, paper="p2", role="judge"))
    led.append(entry(0.0, hit=True))
    assert led.total_eur == pytest.approx(0.15)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 3 and set(rows[0]) == {
        "ts", "role", "model", "prompt_id", "paper_id", "tokens_in", "tokens_out", "cached_in", "eur", "cache_hit",
        "latency_s",
    }
    summ = led.summary()
    assert summ["total"]["calls"] == 3 and summ["total"]["cache_hits"] == 1
    assert summ["role"]["judge"]["eur"] == pytest.approx(0.05)
    assert summ["paper_id"]["p1"]["calls"] == 2

    # a new process sees the cumulative total, but starts per-paper counts afresh
    led2 = CostLedger(path)
    assert led2.total_eur == pytest.approx(0.15)
    assert led2.paper_eur == {}


def test_budget_guard_dev_cap(tmp_path):
    led = CostLedger(tmp_path / "l.jsonl")
    guard = BudgetGuard(led, dev_budget_eur=0.2, max_eur_per_paper=None)
    led.append(entry(0.15))
    guard.check("p1")
    led.append(entry(0.06))
    with pytest.raises(BudgetExceeded, match="ONUS_DEV_BUDGET_EUR"):
        guard.check("p1")


def test_budget_guard_per_paper_cap(tmp_path):
    led = CostLedger(tmp_path / "l.jsonl")
    guard = BudgetGuard(led, dev_budget_eur=None, max_eur_per_paper=0.1)
    led.append(entry(0.1, paper="p1"))
    with pytest.raises(BudgetExceeded, match="p1"):
        guard.check("p1")
    guard.check("p2")
    guard.check(None)
    led.reset_paper("p1")
    guard.check("p1")
