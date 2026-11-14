# Code-test harness (vendored)

`run_code_tests.py` is the harness that graded every submitted codebase before the build. It installs each
paper's code and runs up to 3 entry points in a CPU Modal sandbox, 5 minutes each, then grades the result:
runs / runs-long / runs-after-adding-deps / needs-args / blocked-external / partly-blocked / broken /
broken-version-drift / install-failed / stub-or-no-op / no-source-code.

Only the code is here. No results, logs, reports or grades.

Changes from the original:
- `--data-root` (default `data/dev`) and `--out` (default `var/code_tests`) replace the hard-coded corpus
  paths. Papers come from `<data-root>/papers/*/code`, not from a label file, and nothing is written into
  the data root.
- Keys come from `SANDBOX_OPENAI_API_KEY`, `SANDBOX_ANTHROPIC_API_KEY` and `SANDBOX_GEMINI_API_KEY`, exposed
  inside the sandbox as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` and `GOOGLE_API_KEY`. They
  go only to papers whose code mentions an LLM provider (`llm_hint`). `HF_TOKEN` is never passed. Use
  `--no-keys` to pass none.

Note: during runs, network access is limited to a fixed list of hosts (package indexes, GitHub, Hugging Face,
the provider APIs and common dataset hosts), not the provider APIs alone. That is wider than Phase 10's
re-run sandbox (BUILD_PLAN §11); reuse the planning and grading logic, not this network policy.

```bash
poetry run python vendor/code_test_harness/run_code_tests.py --plan-only      # entry points, no Modal
poetry run python vendor/code_test_harness/run_code_tests.py --ids <forum_id> --out var/code_tests
```
Uses `modal` (in `pyproject.toml`) and `python-dotenv` (installed through `pydantic-settings`).
