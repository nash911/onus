# Evaluation protocol

How Onus is evaluated without tuning on the data it is scored on. This file is tracked in git so the record
is public and timestamped by the commit history. The run-by-run record is in [`eval_log.md`](eval_log.md).

## 1. Splits and the information barrier

The corpus is the 247 Agents4Science 2025 submissions with an OpenReview record (48 accepted, 199 rejected).
It is split once, before any pipeline code exists, with a fixed seed (20251114):

| Split | Papers | Accepted / rejected | Use | Who sees it |
| --- | ---: | ---: | --- | --- |
| dev | 10 | 5 / 5 | Building and tuning | Coding agent and author |
| eval | 20 | 10 / 10 | Go/no-go before the freeze; at most 3 runs | Author; the agent gets aggregate tables only |
| test_1 | 30 | 15 / 15 | Headline results, once, after the freeze | Author only |
| test_2 | 30 | 15 / 15 | Replication of test_1, once, after the freeze | Author only |
| test_remaining | 157 | 3 / 154 | Corpus findings, after the freeze | Author only |

Within each of the four main splits, accepted and rejected papers have identical counts per code-execution
tier (executable, blocked, fails, no code).

- Only `dev` is inside the repository. The other splits are staged outside it and kept as a zip archive,
  unpacked only for an author-run command and deleted afterwards.
- The coding agent works in a sandbox. It cannot read the held-out splits, the source corpus or the
  split-building scripts; this was tested before development started.
- Inside every split, model inputs (`papers/`: PDF and submitted code) and labels (`labels/`: decisions,
  reviews, scores, audits) are in separate folders. Pipeline code may open only `papers/`; a test fails the
  build if a pipeline module reaches `labels/`.

## 2. What is measured

Onus maps each claim in a paper to the evidence the paper gives for it and assigns a verdict (supported,
overstated, contradicted, untraced, not checkable). There is no ground truth for that mapping in the corpus,
so evaluation has three parts:

1. **Planted errors (recall).** Five kinds of error are planted into the parsed text of a paper. Each kind
   has preconditions (it is applied only where it can be detected) and a rule for what counts as caught.

   | Type | Mutation | Caught when |
   | --- | --- | --- |
   | abstract_number | An abstract number that also appears in a parsed table is changed to a value no table cell matches at that precision | The claim with that number is `contradicted` by the number check |
   | checklist_flip | A checklist "No"/"NA" becomes "Yes" on an item the paper really lacks | That item is `unbacked` or `contradicted` |
   | remove_table | A parsed table that at least one claim relies on is deleted | Each such claim is `untraced` or `not_checkable` with a dangling reference |
   | widen_scope | A specific claim is rewritten with universal wording | That claim is `overstated` or `contradicted` |
   | delete_errorbars | Error bars, spreads and significance tests are removed; the checklist keeps "Yes" | The error-bars item is `unbacked` |

   Mutations edit the parsed document, not the PDF, so recall measures checking, not PDF parsing.
   Recall is reported per type with Wilson 95% intervals. The **collateral flag rate** (flags in a mutated
   run that are not at the mutation and were absent from the original run) is reported next to it.
2. **Baseline.** The same extracted claims are judged by one prompt over the paper text. Recall alone
   flatters a system that flags a lot, so each system's flag rate on the unmutated papers is reported
   beside its recall.
3. **Precision by hand labels.** After the freeze, a blind, shuffled sample of Onus and baseline flags
   from test_1 is labelled correct / incorrect / unsure by the author, without knowing which system made
   each flag.

Stability is measured as agreement across three runs per paper.

## 3. Run limits and order

1. **Dev** is used freely for building and tuning.
2. **Eval** is run at most three times before the freeze:
   - eval #1 after the evaluation harness is complete and known dev problems are fixed;
   - eval #2 as the go/no-go;
   - eval #3 only if #2 shows a fixable problem, after one more fix round on dev.

   After each run the coding agent receives only the printed aggregate table: no paper ids, titles, quotes
   or per-paper rows, and no cell based on a single paper. Fixes must be general rules, tuned and checked
   on dev.
3. **Freeze.** The version is frozen with a fingerprint over the model settings, prompts, rubrics and
   pipeline source. A test fails if any of them changes afterwards without a version bump.
4. **Test sets** are run once each with the frozen version, and the results are reported as they come out.

Every held-out run is logged in [`eval_log.md`](eval_log.md) before it starts.

## 4. The evaluation harness is fixed from eval #1

The harness (mutations, their preconditions and detection rules, the baseline prompt, the metrics) has its
own fingerprint. It is fixed from eval #1 onward, so eval, test_1 and test_2 are scored by the same rules. If
a harness bug must be fixed after eval #1, the fix and the new harness fingerprint are logged, and earlier
runs are re-scored from their stored results.

### Harness changes made on dev before any held-out run

These were made while the harness was run on the dev split during Phase 7, before any held-out data had been
analysed. They correct the ground truth or remove errors in the harness; none was chosen to raise a
held-out number.

| Change | Why |
| --- | --- |
| abstract_number picks a new value that no table cell matches at the claim's precision (was: ×1.15, or +3 points for percentages) | The +3 change could land on another row's value, so the "error" matched the table |
| checklist_flip requires the paper to lack the item (no spread or significance language for error bars, no compute language for compute, no code or repository for open access) | Otherwise the flipped "Yes" could be true, and flagging it would be wrong |
| delete_errorbars also removes significance-test language (χ², p-values, FDR, Benjamini–Hochberg, …) | Otherwise the paper still backs its "Yes" and leaving it unflagged would be correct |
| A rewrite that fails after rate-limit retries counts as a failed run, not as an inapplicable paper | Failed rewrites were silently shrinking the denominator |
| Mutation targets are matched by span only (was: by span, else by quote similarity) | The quote fallback could match a restatement of the same finding elsewhere in the paper and score it as the target; offsets are remapped exactly, so the span is enough. A target the mutated run did not extract now counts as "target not extracted" (a miss) |
| The printed table shows each system's flag rate on the unmutated papers and its collateral flag rate beside its recall, with Wilson 95% intervals on every rate | Recall alone flatters a system that flags a lot (Phase 7b) |
| The harness has its own fingerprint (`eval/` scoring modules and the `baseline_support` and `widen_scope` prompts), printed by `onus version` and `onus eval` and stored in `metrics.json` | So eval, test_1 and test_2 can be shown to use the same harness |

### Harness changes after eval #1 (harness v2, before eval #2)

Eval #1 used harness `a010dfd35251`. Two harness bugs were found on dev while investigating eval #1's aggregate
table; both are fixed in harness v2, which is used from eval #2 onward. Eval #1 was not re-run.

| Change | Why |
| --- | --- |
| The baseline receives the claims in paper order (by position), not in the order of the RunResult | The RunResult lists claims by Onus's verdict, flagged first. So the baseline's input order and batches depended on Onus's verdicts, and any change to Onus re-sampled the baseline. **Eval #1's baseline numbers were produced with the old harness (claims ordered by Onus's verdicts) and are superseded from eval #2 onward.** |
| delete_errorbars also removes all wording the checklist audit accepts as error-bars backing (for example "stdev", IQR, credible intervals, variance across folds, "p < .05" without the leading zero), in body text, table and figure captions, and every table cell including header cells ("± SD", "95% CI"). The list is the audit's own pattern (`error_bars` in `rubrics/checklist_map.yaml`), read at run time, and its text is part of the harness fingerprint; a test fails if the audit accepts a form the mutation leaves in place | Found while investigating eval #1's delete_errorbars misses: the mutation removed less than the audit accepts, so a mutated paper could still report spreads and an unflagged "Yes" was then correct. The fix changes the ground truth, so it applies to Onus and the baseline equally. The pipeline's wording was not changed to match |

Two pipeline defects found by the dev harness were also fixed then: number mapping could never report a
mismatch (`map_number` v1), and the number check now tries the paper's other parsed tables when the linked
ones lack the claim's numbers. Phase 7b (before eval #1) fixed further dev misses; its changes are listed in
the first eval-log entry.

## 5. Budget

All model calls are priced and logged. Held-out runs use a separate cost ledger from development, each
capped by `ONUS_DEV_BUDGET_EUR` (€50), with a per-paper cap `ONUS_MAX_EUR_PER_PAPER` (€1.50). Eval #1 cost €14.41
(estimate was €9–11; the baseline's judge calls on the longer held-out papers cost about twice the dev rate).
Repeat eval runs reuse the held-out model cache, so only calls whose inputs changed are paid again.

## 6. Known limits

- **Accepted papers are camera-ready versions.** Their submitted versions need an OpenReview login that was
  not available, so accepted PDFs differ in form from rejected ones (named authors, no line numbers). Line
  numbers are stripped from all text and never used.
- **Small samples.** Eval has 20 papers and yields roughly 20–40 planted errors per run, so recall
  intervals are wide (about ±20 points). Differences smaller than that are not claimed.
- **Text-level mutations** test checking, not PDF parsing.
- **One author labels precision.** No inter-rater agreement is available.
