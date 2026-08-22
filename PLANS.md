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

## Native GitHub stacked pull requests

Use GitHub's public-preview stacked pull request feature, not a manually managed approximation. Prefer the official `gh stack` CLI extension when it is available; GitHub's `Create stack` / `Add to stack` web flow is the equivalent fallback.

```text
main
└── M1
    └── M2
        └── M3
            └── M4
                └── M5
```

Rules:

1. Each milestone is one native stack layer with its own focused pull request and CI.
2. Create later layers on top of the current top layer so each PR shows only its incremental diff.
3. Prefer `gh stack init`, `gh stack add`, and `gh stack submit` for creating/managing the stack when the CLI is available.
4. If using github.com instead, explicitly select `Create stack` / `Add to stack`; merely pointing PR bases at each other is not treated as completion of the native-stack setup.
5. Let GitHub's stack machinery handle stack navigation and post-merge rebase/retarget behavior instead of manually maintaining it where the native feature supports the operation.
6. Keep every layer independently green under the validation gate below.
7. The review approval gate still applies: do not begin implementation of the next milestone until the current milestone is reviewed and approved.

## Validation gate per milestone

1. Run the narrowest new/changed tests first.
2. Run `ruff check .`.
3. Run `mypy src tests`.
4. Run `pytest`.
5. Run `python -m build`.
6. Fix all failures before asking for review.

## Review rule

Do not start the next milestone until the current milestone has been reviewed and approved.
