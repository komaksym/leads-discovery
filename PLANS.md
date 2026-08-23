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
  - optional live Apify Google Maps adapter
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

Use GitHub's public-preview stacked pull request feature to decompose a larger milestone/change into small atomic review layers. A milestone is not itself a single stack layer by default.

For M1 the review stack is:

```text
main
└── M1.1 foundation
    └── M1.2 canonical models
        └── M1.3 durable state
            └── M1.4 cost accounting
                └── M1.5 milestone metadata
```

M2 is an explicit exception to milestone decomposition. Per product-owner direction,
query generation, Exa discovery, optional live Apify discovery, and deduplication are
implemented and reviewed together as one atomic pull request:

```text
main
└── M2 discovery and deduplication
```

The implementation contract is
`docs/superpowers/specs/2026-08-23-m2-discovery-deduplication-design.md`. M2 must not be
split into sub-milestones, stack layers, or separately approved implementation sections.

Rules, except where a milestone-specific exception above says otherwise:

1. Split a milestone into the smallest coherent dependent PRs that make review easier; each layer should have one clear purpose.
2. Submit all layers of a milestone together once the milestone implementation is ready, so reviewers can navigate and review the whole stack without waiting for each layer to merge first.
3. Each PR targets the branch immediately below it, so its diff contains only that layer.
4. Use GitHub's native stack object (`gh stack submit` / `gh stack link` or the github.com `Create stack` / `Add to stack` flow), not only a chain of manually based PRs.
5. Keep every layer green under the validation gate. A layer without behavior-specific tests must still pass the repo-wide lint/typecheck/test/build gate.
6. Review comments are fixed on the layer that owns the code; dependent layers are updated only when the parent change affects them.
7. The milestone review gate applies after the whole milestone stack has been submitted and reviewed. It does not block creating or submitting higher layers within that same stack.
8. Do not begin the next milestone until the current milestone stack is approved, unless the user explicitly overrides that gate.

## Validation gate per layer

1. Run the narrowest new/changed tests first when the layer adds behavior.
2. Run `ruff check .`.
3. Run `mypy src tests`.
4. Run `pytest`.
5. Run `python -m build`.
6. Fix all failures before asking for review.

## Review rule

Review the complete stack for the current milestone from bottom to top. The next milestone starts only after the current milestone stack is approved, unless explicitly overridden.
