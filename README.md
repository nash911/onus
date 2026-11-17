# Onus

**Claim-to-evidence maps for research papers, with a measured error rate.**

**Live app: [www.onuscheck.com](https://www.onuscheck.com)** · Paper: [`paper/onus.pdf`](paper/onus.pdf) ·
Evaluation protocol: [`docs/public/eval_protocol.md`](docs/public/eval_protocol.md) ·
Run log: [`docs/public/eval_log.md`](docs/public/eval_log.md)

Built by **[Avinash Ranganath](https://avinashranganath.com)** for the AI4MetaScience Hackathon 2026, Task 3 (claim-to-evidence
linker). More about the author and other projects: **[avinashranganath.com](https://avinashranganath.com)**.

---

## What it does

Paste an arXiv or OpenReview id, or upload a PDF (and optionally its code). Onus returns a **claim map**: every
major claim in the paper, quoted verbatim with its page and paragraph, linked to the table cells, passages,
checklist answers and code lines that bear on it, with exactly one verdict and a one-sentence reason.

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

Onus was built by AI coding agents that could only see a 10-paper development split. The evaluation followed a
protocol written before any held-out data was touched:

- **Corpus:** the 247 Agents4Science 2025 submissions, split once with a fixed seed into dev / eval / test_1 /
  test_2 / remainder.
- **Information barrier:** held-out splits lived outside the repository in an encrypted archive and were run only
  by the author; agents received aggregate tables only.
- **Freeze:** after two eval runs the pipeline was frozen at v1.0.0 (fingerprint `9ad544b97609`); a test fails
  on any drift. The two test splits were then run once each, and every run was logged before it started.
- **Measures:** recall on five kinds of planted errors, against a single-prompt baseline, with each system's flag
  rate on unmutated papers alongside; precision on a blind, labelled sample of flags; stability across three runs;
  agreement of code checks with the conference's code audit. All proportions carry Wilson 95% intervals.

Headline numbers: [`docs/public/submission_numbers.md`](docs/public/submission_numbers.md) (also on the app's
[How well Onus works](https://www.onuscheck.com/trust) page). Pre-freeze go/no-go run (eval #2): planted-error
recall 83% [66–93%] vs. 80% [63–90%] for the baseline, while flagging 21% of claims on unmutated papers vs. 31%.

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

Tests: `poetry run pytest` (no network; live tests are opt-in with `-m live`). The corpus data is not
redistributed here; the Agents4Science submissions are public on OpenReview.

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
| `paper/` | Workshop paper (LaTeX) |
| `deploy_data/` | Result bundles, metrics and findings imported by the app at startup |

## Author

**Avinash Ranganath** · [avinashranganath.com](https://avinashranganath.com) · [github.com/nash911](https://github.com/nash911)

If you use Onus or its evaluation protocol, please cite the paper in [`paper/`](paper/).
