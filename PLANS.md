# Leads Discovery Plan

## Status

- [x] M1 — core state and persistence
- [x] M2 — candidate discovery, research, extraction, replay safety, and cost tracking
- [x] M3 — evaluation, ranking, outputs, and calibration
- [ ] PR #27 cleanup repair — simplify internals without changing public artifact behavior

## PR #27 repair plan

1. **Restore contract boundaries**
   - Preserve the existing M2/M3 artifact set and schemas.
   - Keep historical implementation/spec documents outside the cleanup diff.
   - Fix the current lint/test inconsistencies before judging deeper refactors.
2. **Keep the useful architectural cleanup**
   - No runtime signature inspection, exact-type dispatch, private researcher calls, or fake-specific production branches.
   - Keep explicit discovery/research/extraction boundaries and one live M2 composition path.
   - Keep direct defensive copying instead of serialization round-trips used only for cloning.
3. **Make paid-call semantics explicit**
   - Require a finite positive Exa per-request reservation for live execution.
   - Admit an Exa request only when `known_spend + reservation <= configured_budget`.
   - Preserve durable in-flight state and ambiguous-outcome replay barriers.
4. **Reduce test/internal API ceremony**
   - Test CLI behavior at the CLI boundary instead of patching provider implementation details.
   - Keep one strong regression for spend, replay, ranking/dedup, corruption, filesystem, privacy, and zero-network invariants.
   - Give genuinely shared evaluation/calibration formatting logic a shared owner; otherwise keep it local.
5. **Validation**
   - Run focused changed tests first.
   - Run `ruff check src --select C901` and compare with `main`.
   - Run `ruff check .`, `mypy src tests`, `pytest`, and `python -m build`.
   - Inspect final diff/LOC, then repeat the complete validation gate on the same final head.

## Non-negotiable invariants

- Exact/conservative company deduplication and deterministic ranking.
- Explicit provider budgets and reservation before paid dispatch.
- No silent replay of ambiguous paid operations.
- Bounded provider calls and responses.
- Atomic writes, path containment, and symlink rejection.
- Append-only authoritative usage accounting plus the existing derived usage artifact.
- Zero live provider calls in tests.
- No public rich-contact publication without an explicit human approval boundary.
