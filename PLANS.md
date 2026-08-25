# Leads Discovery Implementation Plan

## Summary

Build the complete PVF company-discovery pipeline in reviewable milestones. M1-M3 establish company discovery, evaluation, and calibration. M4 adds an explicit artifact-only contact discovery and work-email verification stage after M3. Every milestone must pass lint, typecheck, tests, and build/package checks before moving on.

## Milestones

- [x] **M1 — Core state and persistence**
  - Python package/config skeleton
  - canonical company/evidence/run models
  - JSONL/JSON persistence
  - checkpoint/resume primitives
  - usage/cost ledger primitives
  - focused tests
- [x] **M2 — Candidate intelligence batch**
  - query generation
  - Exa discovery adapter
  - optional live Apify Google Maps adapter
  - domain/name+location deduplication with provenance merge
  - Exa evidence collection
  - bounded evidence bundles
  - DeepSeek schema-constrained extraction
  - explicit unknown/confidence/evidence links
  - narrow checkpointed/resumable batch runner
  - real discovery-to-extraction acceptance batch for up to 20 companies
- [ ] **M3 — Evaluation and calibration**
  - workload/economic/incumbent/direct-pain scoring
  - coverage-aware missing-value policy
  - hard rejection rules
  - accepted/rejected/uncertain classification
  - full CLI and end-to-end orchestration
  - provider budget pause/resume
  - CSV/JSON output views
  - ~20 evaluated-company cap
  - usage/cost report
  - manual-label calibration workflow
  - documentation and final validation
- [ ] **M4 — Contact discovery and enrichment**
  - consume only M3 accepted companies
  - Exa People Search with current-employment validation
  - deterministic buying-decision proximity ranking and exact deduplication
  - retain at most three contacts per company
  - Clay work-email Routine for only the top two rank-1/2 contacts
  - Apollo work-email fallback with phones/personal/waterfall disabled
  - Instantly verification only, with pending GET resume
  - separate M4 checkpoint and usage ledgers
  - deterministic `contacts.jsonl` and review-first `leads.csv`
  - dry-by-default explicit `enrich` CLI and manual-only GitHub Action
  - no outreach, CRM, phones, personal email, database, or frontend
  - completion remains gated on combined independent tests and red-team validation

M4 contract documents:

- `docs/superpowers/specs/2026-08-24-m4-contact-discovery-enrichment-design.md`
- `docs/superpowers/plans/2026-08-24-m4-contact-discovery-enrichment.md`

## Production-readiness implementation

Summary: keep the existing staged/file architecture, but make the live path fail closed around money, ambiguous provider outcomes, persisted state, and public output publication. The production runtime is a standard GitHub-hosted runner; no local machine or new infrastructure is introduced.

Milestones for this branch:

1. Harden the existing persistence/accounting boundary with bounded incremental replay, resource limits, and Linux-safe no-symlink atomic writes.
2. Make paid provider dispatches reserve worst-case budget and durable operation identity/state before dispatch; ambiguous potentially-billed outcomes pause instead of replaying.
3. Split Apify start/persist/poll, add explicit provider timeouts, bounded DeepSeek retry handling, and conservative validation of decision-affecting negative facts.
4. Add a manual-only GitHub-hosted production workflow that enforces a one-company canary, tiny non-bypassable spend/call/storage ceilings, and publishes only `leads.csv` plus `contacts.jsonl` to `generated-leads` with `GITHUB_TOKEN`.
5. Add focused regression tests, run repository lint/type/test/build/offline/workflow checks, inspect CI, and leave the real credentialed one-company canary as the final external acceptance gate.

### Final blocker-fix slice

Summary: fix only the three confirmed readiness defects without redesigning passing replay, budget, persistence, workflow, or publication behavior.

1. Require a hard-negative citation to connect the negation to the target concept within the same bounded clause/proximity window; otherwise downgrade the negative fact to unknown.
2. Read provider bodies through HTTPX streaming responses, reject oversized declared lengths before consumption, and stop incremental body reads immediately when the configured byte ceiling is crossed.
3. Make Exa request timeout semantics explicit at the adapter request boundary while preserving injected clients and MockTransport tests.
4. Add focused production regression tests for negative-evidence relation, true streamed byte enforcement, bounded JSON success, and Exa timeout behavior; then run the full existing validation gate.

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
query generation, Exa discovery, optional live Apify discovery, deduplication, Exa
research, DeepSeek extraction, and the narrow live batch are implemented and reviewed
together as one atomic pull request:

```text
main
└── M2 candidate intelligence batch
```

The implementation contract is
`docs/superpowers/specs/2026-08-23-m2-discovery-deduplication-design.md`. M2 must not be
split into sub-milestones, stack layers, or separately approved implementation sections.
M3 consumes persisted M2 facts and finishes all original scoring, decision, full-runner,
output, and calibration scope; no original M1-M5 capability is dropped.

M3 is also delivered as one atomic implementation because it is the final integrated module:

```text
main
└── M3 evaluation and calibration
```

Its implementation contract is
`docs/superpowers/specs/2026-08-23-m3-evaluation-calibration-design.md`. Internal files retain
clear scoring, orchestration, export, and calibration boundaries, but M3 is not split into
separately approved sub-milestones or stacked product PRs.

M4 is delivered as one isolated production PR because selection, provider resume semantics,
separate M4 state, CLI authorization, and artifacts form one contract boundary:

```text
main
└── M4 contact discovery and enrichment
```

The production branch remains incomplete until its independent contract-test candidate and
red-team review validate the combined behavior. The production PR must not mark M4 complete.

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
