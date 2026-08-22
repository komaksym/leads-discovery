# Leads Discovery Implementation Plan

## Summary

Build the PVF company-discovery pipeline in small reviewable milestones. Each milestone must be independently testable and must pass lint, typecheck, tests, and build/package checks before moving on.

## Milestones

- [ ] **M1 — Core state and persistence**
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

## Validation gate per milestone

1. Run the narrowest new/changed tests first.
2. Run `ruff check .`.
3. Run `mypy src tests`.
4. Run `pytest`.
5. Run `python -m build`.
6. Fix all failures before asking for review.

## Review rule

Do not start the next milestone until the current milestone has been reviewed and approved.
