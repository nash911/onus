# Onus: rules for coding agents

Read `BUILD_PLAN.md` in full before doing anything. Work one phase at a time, in the order given to you, and stop at every CHECKPOINT.

## Hard rules (never break these; if a task seems to need breaking one, stop and ask)

1. **Information barrier.** You may only use the dev split in `data/dev/`. Never search for, read, request, or reason about other splits (`~/onus-heldout*`, `~/onus-runs*`, `deploy_data/`). Never look outside this repo for data either: the full source corpus (`../AI4MetaScience_Hackathon-OpenReview_data/`, `../scripts/`, `../code/`), other Claude Code sessions' transcripts and scratch folders, and any download or backup copy are off limits. If a file you need is not under `data/dev/`, stop and ask. Never fetch `openreview.net` or `agents4science.stanford.edu`; tests for those fetchers use mocks.
2. **Labels are never inputs.** Pipeline code (`onus/ingest`, `claims`, `evidence`, `checks`, `verdict`, `codecheck`, `pipeline.py`) reads only `<data_root>/papers/`. Only `onus/eval/` and `onus/bundle.py` may read `<data_root>/labels/`. `tests/test_barrier.py` enforces this; keep it passing.
3. **No overfitting.** No paper IDs, titles, or paper-specific strings in pipeline code, prompts or rubrics. Rules must be general. Tune on dev only.
4. **Code computes, the model points.** Arithmetic, counts, comparisons and lookups are done in Python. Every quote from a model is verified against the paper text; every evidence ID must come from the candidate list the model was given; every cited code or output line must exist.
5. **Spend.** All Berget calls go through `onus.llm`. Respect `ONUS_DEV_BUDGET_EUR` and `ONUS_MAX_EUR_PER_PAPER`. Live tests (`pytest -m live`) only when a phase says so. Report spend at the end of every phase.
6. **No git writes, no deploys.** Do not commit, push or touch Railway. The author commits at each checkpoint.
7. **Modal.** Only dev code, and the code of live analyses (linked repositories or user uploads), runs in sandboxes. Live code is only read on the server and runs only in the Modal sandbox. The only secrets a sandbox may receive are the paper-code provider keys (`SANDBOX_OPENAI_API_KEY`, `SANDBOX_ANTHROPIC_API_KEY`, `SANDBOX_GEMINI_API_KEY`), passed as a Modal Secret, and only to papers whose code calls those providers. Never pass the Berget key, Modal tokens, `HF_TOKEN` or any other secret. Never print, log or write a key value. Respect `ONUS_RERUN_MAX_MINUTES`.
8. **Frozen means frozen.** After `onus freeze`, do not change prompts, rubrics or pipeline logic without bumping the version; `tests/test_frozen.py` fails if the fingerprint drifts.

## Conventions

- Python 3.12, Poetry, Pydantic v2, SQLAlchemy 2.0, typed code, `ruff` clean. Backend package: `src/backend/onus`. Frontend: `src/frontend` (React, Vite, TypeScript, Tailwind).
- Shared contract: `onus/schemas/result.py` → `schema/result.schema.json` → generated TypeScript types. Change the Python model first, then regenerate.
- At the end of each phase: run `poetry run pytest`, append a short entry to `docs/progress.md` (done, spend, open issues), then stop if the phase ends in a CHECKPOINT.
