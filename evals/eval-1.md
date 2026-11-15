# Eval #1 (held-out eval split)

Run 2026-11-15 17:07 UTC, commit `172ed05`, pipeline fingerprint `a1c46b3e07ad`, harness fingerprint
`a010dfd35251`. `onus analyze --runs 1`, then `onus eval --mutations 10x5 --baseline` (seed 7).
Aggregate tables exactly as printed by `onus eval`; per-paper detail stays outside the repo.
Total held-out spend for this run: €14.41 (analysis €2.25 + eval €12.15).

Mutation plan: abstract_number 6, checklist_flip 6, widen_scope 10, delete_errorbars 8, **remove_table 0**
(no eval paper met its preconditions); 30 mutated runs.

## Summary

| Metric | Value |
| --- | --- |
| Pipeline fingerprint / harness fingerprint | `a1c46b3e07ad` / `a010dfd35251` |
| Papers (results / labelled) | 20 / 20 |
| Runs per paper | 1 |
| Claims (all / non-checklist) | 406 / 248 |
| Verdict: contradicted | 6 (1%) |
| Verdict: overstated | 25 (6%) |
| Verdict: untraced | 59 (15%) |
| Verdict: not_checkable | 41 (10%) |
| Verdict: supported | 275 (68%) |
| Flagged share (claims) | 90/406 = 22% [18%–26%] |
| Flagged share (main claims) | 19/88 = 22% [14%–31%] |
| Checklist audit backed, unbacked, contradicted, not_checkable, not_applicable | 103 / 36 / 2 / 17 / 32 |
| Stability (claims with full agreement) | n/a (1 run) |
| Mutations: seed / plan | 7 / 10x5 |
| Code agreement (vs audit) | n/a (no code-check results yet); 10 labelled, 10 excluded |
| Eval spend / wall time | €12.15 / 55 min |

## Recall

| Mutation type | Attempted / applied / target not extracted | Onus recall | Baseline recall |
| --- | --- | --- | --- |
| abstract_number | 6 / 6 / 0 | 5/6 = 83% [44%–97%] | 5/6 = 83% [44%–97%] |
| checklist_flip | 6 / 6 / 0 | 5/6 = 83% [44%–97%] | 5/6 = 83% [44%–97%] |
| widen_scope | 10 / 10 / 1 | 7/10 = 70% [40%–89%] | 8/10 = 80% [49%–94%] |
| delete_errorbars | 8 / 8 / 0 | 6/8 = 75% [41%–93%] | 8/8 = 100% [68%–100%] |
| **all types** | 30 / 30 / 1 | 23/30 = 77% [59%–88%] | 26/30 = 87% [70%–95%] |
| Flag rate on unmutated papers | claims flagged (Onus) / not_supported (baseline) | 90/406 = 22% [18%–26%] | 131/406 = 32% [28%–37%] |
| Collateral flag rate | new flags away from the target | 9/592 = 2% [1%–3%] | 21/592 = 4% [2%–5%] |
