# Held-out run log

Written by the author only, **before** each held-out run starts; the result line is added afterwards.
Aggregate numbers only: no paper ids, titles or per-paper results. Rules are in
[`eval_protocol.md`](eval_protocol.md). Commit after each entry.

| # | Split | Started (UTC) | Git commit | Pipeline fingerprint | Harness fingerprint | Changes since last run | Result (aggregate) | € |
| --- | --- | --- | --- | --- | --- | --- | --- | ---: |
| eval-1 | eval | 2026-11-15 17:07 | `172ed05` | `a1c46b3e07ad` | `a010dfd35251` | Phase 7b fixes on dev (see docs/progress.md) | Recall Onus 23/30 = 77% [59–88%] vs baseline 26/30 = 87% [70–95%]; flag rate on unmutated papers 22% vs 32%; collateral 2% vs 4%; remove_table not applicable (0 papers). Tables: `evals/eval-1.md` | 14.41 |
