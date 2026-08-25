# Leads Discovery Plan

## Status

- [x] M1 — core state and persistence
- [x] M2 — candidate discovery, research, extraction, replay safety, and cost tracking
- [x] M3 — evaluation, ranking, outputs, and calibration
- [ ] Repository cleanup — simplify the completed M1–M3 implementation without weakening safety

## Repository cleanup plan

### Slice 1 — paid-operation boundaries

- Add a failing regression for Exa pre-dispatch budget reservation.
- Replace reflection, exact-type checks, fake-specific fallbacks, and private researcher calls with explicit interfaces.
- Keep paid-operation replay state durable before dispatch and usage durable before completion.
- Keep Apify resumability and DeepSeek reservation behavior unchanged.

### Slice 2 — internal API and copying cleanup

- Centralize run/path validation shared by M2, M3, calibration, and CLI.
- Stop cloning immutable/owned values through `to_dict()`/`from_dict()` round trips.
- Remove cross-module imports of private evaluation helpers.
- Narrow CLI exception handling so programming defects are not silently converted into generic failures.

### Slice 3 — test and documentation cleanup

- Remove exact duplicate regressions and test-only contracts that force fake shapes into production.
- Remove ceremony-only smoke tests.
- Delete superseded implementation-plan/spec documents; Git history remains the archive.
- Retain one strong regression for every replay, spend, ranking, corruption, filesystem, and network invariant.

### Slice 4 — artifact source-of-truth reduction

- Keep append-only `usage_events.jsonl` authoritative; derive usage summaries when needed instead of persisting `usage.json`.
- Recompute deterministic deduplication from `companies_raw.jsonl` on resume instead of persisting `companies_deduped.jsonl`.
- Stop persisting raw Exa research copies once bounded canonical evidence is durably stored in company snapshots.
- Keep `companies_extracted.jsonl`, `checkpoint.json`, `companies_evaluated.jsonl`, `companies_ranked.csv`, `calibration_template.csv`, `run_summary.json`, `calibration_report.json`, and `companies_calibrated.csv`.
- Remove redundant rejected/uncertain CSV views; consumers can filter `companies_ranked.csv` by `final_decision`.

## Non-negotiable invariants

- Exact/conservative company deduplication and deterministic ranking.
- Explicit provider budgets and reservation before paid dispatch.
- No silent replay of ambiguous paid operations.
- Bounded provider calls and responses.
- Atomic writes, path containment, and symlink rejection.
- Append-only authoritative usage accounting.
- Zero live provider calls in tests.
- No public rich-contact publication without an explicit human approval boundary.

## Validation gate

For every slice: run the narrowest changed tests first, then `ruff check .`, `mypy src tests`, `pytest`, and `python -m build`. Before review, repeat the full validation gate on the final branch head and inspect the final diff for accidental scope growth.
