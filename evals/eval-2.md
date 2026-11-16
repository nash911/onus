# Eval #2 (held-out eval split): go/no-go before the freeze

Run 2026-11-15 22:22 UTC, commit `f1deed3`, pipeline fingerprint `322c4db4d17b`, harness fingerprint
`b6018f89f149` (harness v2). `onus analyze --runs 1`, then `onus eval --mutations 10x5 --baseline` (seed 7).
Aggregate tables exactly as printed by `onus eval`; per-paper detail stays outside the repo.
Total held-out spend for this run: €10.61 (analysis €0.02, mostly cached; eval €10.58).

Mutation plan: abstract_number 6, checklist_flip 6, widen_scope 10, delete_errorbars 8, **remove_table 0**
(no eval paper met its preconditions); 30 mutated runs.

## Summary

| Metric | Value |
| --- | --- |
| Pipeline fingerprint / harness fingerprint | `322c4db4d17b` / `b6018f89f149` |
| Papers (results / labelled) | 20 / 20 |
| Runs per paper | 1 |
| Claims (all / non-checklist) | 406 / 248 |
| Verdict: contradicted | 6 (1%) |
| Verdict: overstated | 25 (6%) |
| Verdict: untraced | 53 (13%) |
| Verdict: not_checkable | 41 (10%) |
| Verdict: supported | 281 (69%) |
| Flagged share (claims) | 84/406 = 21% [17%–25%] |
| Flagged share (main claims) | 19/88 = 22% [14%–31%] |
| Checklist audit backed, unbacked, contradicted, not_checkable, not_applicable | 107 / 32 / 2 / 17 / 32 |
| Stability (claims with full agreement) | n/a (1 run) |
| Mutations: seed / plan | 7 / 10x5 |
| Code agreement (vs audit) | n/a (no code-check results yet); 10 labelled, 10 excluded |
| Eval spend / wall time | €10.58 / 40 min |

## Recall

| Mutation type | Attempted / applied / target not extracted | Onus recall | Baseline recall |
| --- | --- | --- | --- |
| abstract_number | 6 / 6 / 0 | 5/6 = 83% [44%–97%] | 5/6 = 83% [44%–97%] |
| checklist_flip | 6 / 6 / 0 | 5/6 = 83% [44%–97%] | 4/6 = 67% [30%–90%] |
| widen_scope | 10 / 10 / 1 | 7/10 = 70% [40%–89%] | 8/10 = 80% [49%–94%] |
| delete_errorbars | 8 / 8 / 0 | 8/8 = 100% [68%–100%] | 7/8 = 88% [53%–98%] |
| **all types** | 30 / 30 / 1 | 25/30 = 83% [66%–93%] | 24/30 = 80% [63%–90%] |
| Flag rate on unmutated papers | claims flagged (Onus) / not_supported (baseline) | 84/406 = 21% [17%–25%] | 125/406 = 31% [26%–35%] |
| Collateral flag rate | new flags away from the target | 9/592 = 2% [1%–3%] | 24/592 = 4% [3%–6%] |
