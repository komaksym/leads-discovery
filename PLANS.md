# Leads Discovery Implementation Status

M1–M4 are integrated. The current production architecture and safety invariants live in [docs/architecture.md](docs/architecture.md).

## Completed milestones

- **M1 — Core state and persistence:** canonical models, durable JSONL/JSON state, checkpoint/resume, and usage ledgers.
- **M2 — Candidate intelligence:** bounded batch discovery, provenance-preserving deduplication, research, extraction, and shared paid-operation safety.
- **M3 — Evaluation and calibration:** proposition-aware evidence support, deterministic scoring/classification, review outputs, and calibration.
- **M4 — Contact discovery and enrichment:** accepted-only authorization, deterministic shortlist, bounded enrichment/verification, separate contact state, and runner-local artifacts.

## Current operating model

```text
search criteria -> M2 discovery/research/extraction -> M3 evaluation
               -> accepted companies -> M4 contacts/enrichment
```

Normal operation is batch-oriented. The one-company credentialed canary is a separate safety smoke test, not the product architecture.

## Validation gate

Every integration candidate must pass:

1. focused changed tests;
2. `ruff check .`;
3. `mypy src tests`;
4. full `pytest`;
5. `python -m build`;
6. offline dry-run safety;
7. relevant workflow/static safety checks.

Historical milestone design rationale remains under `docs/superpowers/specs/`. Completed implementation plans were removed after their load-bearing architecture and safety decisions were consolidated into `docs/architecture.md`.
