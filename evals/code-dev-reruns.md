# Code checks on dev: re-runs (Phase 10, Checkpoint 5)

Run by the author's session outside the Claude Code sandbox, because Session A's sandbox could not reach Modal.
Dev split only.

- Code-v1 `1.0.0+69c86ebf7d17` (not frozen yet); pipeline `9ad544b97609`.
- Results: `var/results/dev_code`; re-run reports in `var/results/dev_code/code/*.rerun.json`.
- Commands, in order:
  1. `onus codecheck plan --data-root data/dev --results var/results/dev_code --rerun-ids var/codecheck_dev_rerun_ids.txt`
  2. `onus codecheck ... --ids d7Uvmmpo0z --max-eur 1`
  3. `onus codecheck ... --ids NYJJoGFtr2,pjpkEHH5YS,xE6L2AQLex --max-eur 2.5`
  4. `python -m onus.eval.code_agreement --split-root data/dev --results var/results/dev_code`
- Spend: Berget **€0.61** (€0.231 + €0.379); Modal **14.1 sandbox minutes**, of which 10.2 were one install.

## Re-run table (printed by `onus codecheck`)

| paper | static | paper level | core p/a/c | re-run | code_rerun | minutes | € | verdicts Δ |
| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: |
| d7Uvmmpo0z | done | mismatch | 0/0/3 | failed | not_run, not_run, not_run | 1.5 | 0.231 | 1 |
| NYJJoGFtr2 | done | n/a | 1/0/0 | ran_no_values | - | 0.7 | 0.047 | 0 |
| pjpkEHH5YS | done | n/a | 1/0/0 | consistent | pass | 1.1 | 0.233 | 0 |
| xE6L2AQLex | done | mismatch | 1/5/0 | needs_external | not_run, not_run | 10.8 | 0.100 | 0 |

## Agreement with the dev audit labels (`code_agreement`)

| paper | audit | static | core p/a/c | tier | grade | rerun | code_rerun | rerun_minutes | verdicts_changed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 3PWDmzgjbb | - | consistent | 3/1/0 | blocked | needs-args | - | - | - | 0 |
| EJ8cQFi5cU | - | mismatch | 0/6/0 | fails | broken | - | - | - | 0 |
| NYJJoGFtr2 | - | not_checkable | 1/0/0 | executable | runs | ran_no_values | | 0.7 | 0 |
| OG8sFxeNHv | CRITICAL | mismatch | 0/2/0 | fails | broken | - | - | - | 0 |
| X0rpsv88au | PASSED | consistent | 4/0/0 | blocked | blocked-external | - | - | - | 1 |
| d7Uvmmpo0z | CRITICAL | mismatch | 0/0/3 | executable | runs | failed | not_run, not_run, not_run | 1.52 | 1 |
| pjpkEHH5YS | PASSED | not_checkable | 1/0/0 | executable | runs | consistent | pass | 1.07 | 0 |
| xE6L2AQLex | - | mismatch | 1/5/0 | executable | runs | needs_external | not_run, not_run | 10.77 | 0 |

The static checks agree with the audit on all 3 labelled papers that got a paper-level verdict (2 mismatch, 1
consistent); 1 labelled paper was not checkable. 2 of the 4 re-runs ran the code in the sandbox.

## What the re-run reports show (author review)

1. **d7Uvmmpo0z:** the code crashes as shipped.
   - The crash is a shape mismatch in the diffusion model: `mat1 and mat2 shapes cannot be multiplied (24897x17 and 18x64)`.
   - The agent edited the code three times (`fcaf_rl.py` once, `action_dim` 9→10 in two files). It made the edits with Python heredocs inside `run`, so `modifications` is empty.
   - It used all 25 steps, so the status reads "failed: step limit". It does not say that the code fails as shipped.
   - Every `run` command reports `rc: 0` even when Python raised, because the agent's commands pipe through `| tee`.
2. **pjpkEHH5YS: pass.**
   - The only target is "95%", a claim with no resolved table cells.
   - What was checked is the code's default `confidence_level = 0.95`, not a reported result. That is weak evidence for a receipt.
3. **xE6L2AQLex:** the input data is not in the submission.
   - The code reads a hard-coded absolute path on the author's own machine, and the `.npz` files are missing.
   - The status is `needs_external`, although this is missing data, not a missing external model.
   - The install phase took 10.2 minutes, ended `install_failed: true`, and the run went ahead anyway.
   - Both targets (99%, 0.98) have no resolved cells.
4. **NYJJoGFtr2:** there were no targets. The code ran cleanly (exit 0), which is a valid "runs" receipt, with no values to compare.
5. **Target selection does not follow the plan.** Phase 10 picks "numeric claims whose number check resolved table cells", but 6 of the 8 targets across the four papers have `cells: []`.
6. **Log noise.** Hundreds of `AsyncUsageWarning` lines: `sandbox.py` calls blocking Modal methods inside async code.
