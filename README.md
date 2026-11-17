# Onus

**Claim-to-evidence maps for research papers, with a measured error rate.**

**Live app: [www.onuscheck.com](https://www.onuscheck.com)** · Paper: [`paper/onus.pdf`](paper/onus.pdf) ·
One-pager: [`paper/onepager.pdf`](paper/onepager.pdf) · Evaluation protocol: [`docs/public/eval_protocol.md`](docs/public/eval_protocol.md) ·
Run log: [`docs/public/eval_log.md`](docs/public/eval_log.md)

Built by **[Avinash Ranganath](https://avinashranganath.com)** for the AI4MetaScience Hackathon 2026, Task 3 (claim-to-evidence
linker). More about the author and other projects: **[avinashranganath.com](https://avinashranganath.com)**.

---

## What it does

Paste an arXiv or OpenReview id, or upload a PDF (and optionally its code). Onus returns a **claim map**: every
major claim in the paper, quoted verbatim with its page and paragraph, linked to the table cells, passages,
checklist answers and code lines that bear on it, with exactly one verdict and a one-sentence reason.

The report shows the paper next to its claims: click a claim and the PDF scrolls to it and highlights the quoted
text; click an evidence item and its passage or table is highlighted too. Export the map as Markdown for a review
or a rebuttal.

| Verdict | Meaning |
| --- | --- |
| **supported** | Evidence found, and as strong as the claim's wording |
| **overstated** | Evidence found but weaker than the wording ("consistently" over one seed, two of five datasets) |
| **untraced** | No evidence located in the paper or its code |
| **contradicted** | Evidence disagrees: numbers differ, code differs from the method, or a re-run lands outside tolerance |
| **not_checkable** | Needs something Onus cannot access (external data, GPU-days), with the reason |

Every report also has a **coverage ledger**: which checks ran, and what was *not* checked and why. There is no
overall paper score: Onus routes a reviewer's attention; people decide.

## How it works

```
paper.pdf ─► ingest ─► claims ─► evidence ─► checks ─► verdicts ─► claim map (× 3 runs → stability)
code/     ─► (text,    (quotes   (explicit    (numbers,   (precedence
             tables,   verified) refs, BM25,  dominance,   rules; an
             checklist)          embeddings,  strength,    adjudicator may
                                 LLM selects) scope,       only downgrade)
                                              checklist,
                                              code*)
* code checks: implementation facts vs. code lines, and a re-run in an isolated Modal sandbox
```

- **The model points, code computes.** Language models extract claims and choose evidence from verified
  candidates. Every quote is checked against the paper text, every evidence id must come from the candidate list,
  every cited code line must exist. Arithmetic, table comparisons, checklist rules and verdict precedence are code.
- **Code checks.** Where code is submitted or linked (GitHub, GitLab, Hugging Face), Onus matches the paper's
  implementation facts to code lines and re-runs the code in a sandbox (network on to install with no secrets,
  network off to run), capped in time and cost.
- **Frozen, named versions.** Each report names its version, models and a fingerprint over prompts, rubrics,
  model settings and pipeline source. Same paper + same version = same report.

## How we know how well it works

All development and tuning used only a 10-paper development split. The evaluation followed a
protocol written before any held-out data was touched:

- **Corpus:** the 247 Agents4Science 2025 submissions, split once with a fixed seed into dev / eval / test_1 /
  test_2 / remainder.
- **Information barrier:** held-out splits lived outside the repository in an encrypted archive and were run only
  by the author; agents received aggregate tables only.
- **Freeze:** after two eval runs the pipeline was frozen at v1.0.0 (fingerprint `9ad544b97609`); a test fails
  on any drift. The two test splits were then run once each, and every run was logged before it started.
- **Measures:** recall on five kinds of planted errors, against a single-prompt baseline, with each system's flag
  rate on unmutated papers alongside; precision on a blind, labelled sample of flags (AI-drafted, author-reviewed); stability across three runs;
  agreement of code checks with the conference's code audit. All proportions carry Wilson 95% intervals.

Held-out results (frozen v1.0.0, test_1 / test_2, three runs per paper; full tables in
[`docs/public/submission_numbers.md`](docs/public/submission_numbers.md) and the [paper](paper/onus.pdf)):

| | Onus | Single-prompt baseline |
| --- | --- | --- |
| Planted-error recall | 70% [58–80] / 75% [62–84] | 76% [64–85] / 78% [66–87] |
| Claims flagged on unmutated papers | 15% / 15% | 29% / 32% |
| Precision on blind labels (test_1) | 67% [54–78] | 89% [72–96] |
| Same verdict in all three runs | 84% / 84% | — |

Onus reaches recall within the baseline's interval while flagging half as many claims, but its flags are less
precise; the `untraced` verdict is the main source of wrong flags. Precision labels were drafted by an AI
assistant blind to the system and reviewed by the author (none changed).

## Run it locally

Requirements: Python 3.12, [Poetry](https://python-poetry.org/), Node 20. Models are called through an
OpenAI-compatible API (the deployed app uses [Berget AI](https://berget.ai)); code re-runs use [Modal](https://modal.com).

```bash
git clone https://github.com/nash911/onus && cd onus
poetry install
cp .env.example .env          # add BERGET_API_KEY; MODAL_TOKEN_ID/SECRET only for code re-runs
poetry run onus version       # version, pipeline and harness fingerprints

# Analyse a paper from the command line (one run, results as JSON)
mkdir -p mypaper/papers/p1 && cp your_paper.pdf mypaper/papers/p1/paper.pdf   # optional: code in mypaper/papers/p1/code/
poetry run onus analyze --data-root mypaper --out results --runs 1
poetry run onus report results

# Web app: API on :8000, frontend dev server on :5173 (proxies /api)
poetry run uvicorn onus.api.main:app --reload
cd src/frontend && npm ci && npm run dev

# Or the production image
docker build -t onus . && docker run -p 8000:8000 -e PORT=8000 --env-file .env onus
```

At startup the API imports the result bundles in `ONUS_RESULTS_IMPORT_DIR` (default `./deploy_data`); point it at
an empty folder to serve only your own analyses.

To open your command-line results in the two-pane report view (PDF on the left, claims on the right), import
them as local analyses with their PDFs. This writes only to the SQLite database inside `ONUS_VAR_DIR` and prints
each report's URL:

```bash
poetry run onus dev-seed --results results --split-root mypaper --papers 1
```

Other commands (all without model calls):

| Command | What |
| --- | --- |
| `onus anchors-eval --results … --split-root … --out anchors.json` | How well PDF highlights land: page, method and token F1 for each claim and evidence item |
| `onus export-numbers --metrics … --findings … [--labels …] --out numbers.md` | Every headline number with its 95% interval, n, source file and command |

Tests: `poetry run pytest` (no network; live tests are opt-in with `-m live`). The corpus data is not
redistributed here; the Agents4Science submissions are public on OpenReview.

Serving settings (rate limits, live code checks, analytics) are read from the environment; the full table is in
[`src/backend/onus/api/settings.py`](src/backend/onus/api/settings.py).

## Privacy

The web app records anonymous usage so the author can see how reports are read: one row per request, and
interaction events such as opening a claim or clicking evidence. There are no cookies and no free text. The
session id is random per browser tab, and visitors are identified only by a truncated, salted hash of their IP.
Requests with `DNT: 1` or `Sec-GPC: 1` are not recorded, and rows are deleted after 90 days
(`ANALYTICS_RETENTION_DAYS`). Set `ONUS_ANALYTICS=0` to turn it off. Uploaded PDFs are stored to serve the report.

## Repository layout

| Path | What |
| --- | --- |
| `src/backend/onus/` | The `onus` package: `ingest/`, `claims/`, `evidence/`, `checks/`, `verdict/`, `pipeline.py` (frozen P0), `codecheck/` (code-v1), `eval/` (harness), `api/` (FastAPI), `db/` |
| `src/frontend/` | React + TypeScript + Tailwind single-page app |
| `prompts/`, `rubrics/` | Every prompt and rule table; part of the fingerprint |
| `schema/` | The `RunResult` JSON schema shared by backend and frontend |
| `frozen.json` | The frozen version and fingerprints |
| `docs/public/` | Evaluation protocol, run log, submission numbers |
| `evals/` | Aggregate tables of the pre-freeze eval runs |
| `paper/` | The paper and a one-pager (LaTeX source and PDF) |
| `deploy_data/` | Result bundles, metrics and findings imported by the app at startup |

## Author

**Avinash Ranganath** · [avinashranganath.com](https://avinashranganath.com) · [github.com/nash911](https://github.com/nash911)

If you use Onus or its evaluation protocol, please cite the paper in [`paper/`](paper/).
