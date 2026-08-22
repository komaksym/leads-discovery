# Leads Discovery Implementation Plan

## Summary

Build the PVF company-discovery pipeline in small reviewable milestones. Each milestone must be independently testable and must pass lint, typecheck, tests, and build/package checks before moving on.

## Milestones

- [x] **M1 — Core state and persistence**
  - Python package/config skeleton
  - canonical company/evidence/run models
  - JSONL/JSON persistence
  - checkpoint/resume primitives
  - usage/cost ledger primitives
  - focused tests
- [ ] **M2 — Discovery and deduplication**
  - query generation
  - Exa discovery adapter
  - optional Apify adapter boundary
  - domain/name+location deduplication with provenance merge
- [ ] **M3 — Research and structured extraction**
  - Exa evidence collection
  - bounded evidence bundles
  - DeepSeek schema-constrained extraction
  - explicit unknown/confidence/evidence links
- [ ] **M4 — Deterministic scoring and decisions**
  - workload/economic/incumbent/direct-pain scoring
  - coverage-aware missing-value policy
  - hard rejection rules
  - accepted/rejected/uncertain classification
- [ ] **M5 — End-to-end calibration run**
  - CLI orchestration
  - provider budget pause/resume
  - CSV/JSON output views
  - ~20 evaluated-company cap
  - usage/cost report
  - documentation and final validation

## GitHub PR stacking

Milestones are developed as a stacked series of branches and pull requests, so each PR shows only the incremental diff for that milestone:

```text
main
└── feat/milestone-1-core                 PR1 -> main
    └── feat/milestone-2-discovery        PR2 -> feat/milestone-1-core
        └── feat/milestone-3-research     PR3 -> feat/milestone-2-discovery
            └── feat/milestone-4-scoring  PR4 -> feat/milestone-3-research
                └── feat/milestone-5-e2e  PR5 -> feat/milestone-4-scoring
```

Rules:

1. Create each milestone branch from the previous milestone branch, not from `main`.
2. Target each PR at the immediately preceding milestone branch so reviewers see only that milestone's changes.
3. Keep every PR independently green under the validation gate below.
4. Do not squash or rewrite an earlier branch in a way that silently invalidates descendant branches; if an earlier stack layer changes, propagate/rebase the dependent stack deliberately.
5. After a parent PR merges, retarget/rebase the next PR onto its new base so the remaining stack stays clean.
6. The review approval gate still applies: do not begin implementation of the next milestone until the current milestone is reviewed and approved.

## Validation gate per milestone

1. Run the narrowest new/changed tests first.
2. Run `ruff check .`.
3. Run `mypy src tests`.
4. Run `pytest`.
5. Run `python -m build`.
6. Fix all failures before asking for review.

## Review rule

Do not start the next milestone until the current milestone has been reviewed and approved.
