# PVF Company Discovery & Ranking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the smallest complete, resumable Python pipeline that discovers, researches, extracts, deterministically scores, and classifies approximately 20 North American PVF distributors without spending people-enrichment credits.

**Architecture:** Use staged JSON/JSONL/CSV files as durable state. Provider-specific logic lives behind small adapters; expensive work checkpoints immediately so runs can resume without repaying for completed work. Scoring is pure local code over persisted structured facts and keeps evidence coverage separate from score.

**Tech Stack:** Python 3.12+, standard library, `httpx`, `pydantic`, `python-dotenv`, `tldextract`, `ruff`, `mypy`, `pytest`, `build`.

**Spec:** `docs/superpowers/specs/2026-08-22-pvf-company-discovery-ranking-design.md`

## Global Constraints

- Unknown is not false; missing evidence must remain explicit.
- Exa is the primary discovery/research provider; Apify is optional.
- DeepSeek may extract facts but never assigns final scores or decisions.
- Clay, Apollo, and Instantly are not called in this milestone.
- Default evaluation cap is 20 companies; default discovery cap is 100 candidates.
- Provider budget exhaustion must preserve completed state and allow resume.
- No database, frontend, dashboard, LangChain, or agent framework.
- Every function and class must have a useful docstring.
- No secrets or environment values may be committed.

---

### Task 1: Core state and persistence

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `src/leads_discovery/__init__.py`
- Create: `src/leads_discovery/models.py`
- Create: `src/leads_discovery/pipeline/state.py`
- Create: `src/leads_discovery/pipeline/costs.py`
- Create: `tests/test_state.py`
- Create: `tests/test_costs.py`

**Interfaces:**
- Produces: canonical `CompanyRecord`, `EvidenceItem`, `UsageEvent`, and `RunCheckpoint` models.
- Produces: append-safe JSONL helpers and atomic JSON checkpoint writes.
- Produces: `CostTracker` for per-provider request/token/cost accumulation.

- [ ] Write failing tests proving JSONL round-trip, atomic checkpoint replacement, resume-safe lookup by company/stage, and cost aggregation.
- [ ] Run `pytest tests/test_state.py tests/test_costs.py -q` and verify the new tests fail.
- [ ] Implement the minimal models/state/cost code needed to pass.
- [ ] Run the narrow tests again.
- [ ] Run `ruff check .`, `mypy src tests`, `pytest`, and `python -m build`.
- [ ] Commit and stop for review.

### Task 2: Discovery and deduplication

**Files:**
- Create: `src/leads_discovery/discovery/base.py`
- Create: `src/leads_discovery/discovery/queries.py`
- Create: `src/leads_discovery/discovery/exa.py`
- Create: `src/leads_discovery/discovery/apify.py`
- Create: `src/leads_discovery/dedup.py`
- Create: `tests/test_queries.py`
- Create: `tests/test_dedup.py`
- Create: `tests/test_discovery_adapters.py`

**Interfaces:**
- Consumes: canonical models and cost tracker from Task 1.
- Produces: deterministic query list and `DiscoveryProvider.search(query, limit)` contract.
- Produces: `deduplicate(records) -> list[CompanyRecord]` using normalized registrable domain, then conservative name+location fallback.

- [ ] Write tests for query family coverage and stable generation.
- [ ] Write tests for domain normalization, provenance merge, and non-merging of ambiguous similar names.
- [ ] Implement Exa HTTP adapter and optional Apify adapter with no-op skip when token is absent.
- [ ] Validate only discovery/dedup tests, then full lint/type/test/build.
- [ ] Commit and stop for review.

### Task 3: Research and DeepSeek extraction

**Files:**
- Create: `src/leads_discovery/research/evidence.py`
- Create: `src/leads_discovery/research/extract.py`
- Create: `tests/test_evidence.py`
- Create: `tests/test_extract.py`

**Interfaces:**
- Consumes: canonical companies, Exa adapter, cost tracker.
- Produces: bounded evidence bundles grouped around relevance, workload, economic fit, incumbent exposure, and direct pain.
- Produces: typed extracted features where every field is `{value, confidence, evidence_ids}` and unsupported facts stay `null`.

- [ ] Write tests for evidence deduplication/bounding and unknown-preserving extraction parsing.
- [ ] Implement Exa evidence queries and evidence bundle construction.
- [ ] Implement DeepSeek structured JSON extraction with schema validation.
- [ ] Validate narrow tests, then full lint/type/test/build.
- [ ] Commit and stop for review.

### Task 4: Deterministic scoring and decisions

**Files:**
- Create: `src/leads_discovery/scoring/features.py`
- Create: `src/leads_discovery/scoring/policy.py`
- Create: `tests/test_scoring.py`
- Create: `tests/test_decisions.py`

**Interfaces:**
- Consumes: persisted extracted feature objects.
- Produces: category scores, category coverage, overall coverage, final score, and machine-readable decision/review/rejection reasons.

- [ ] Write tests showing unknown features do not become zero and known subfeatures renormalize within a category.
- [ ] Write tests for soft size priors, confirmed competitor hard rejection, historical competitor evaluation non-rejection, and uncertain low-coverage cases.
- [ ] Implement pure deterministic scoring with weights 40/25/25/10.
- [ ] Implement accepted/rejected/uncertain policy with configurable thresholds.
- [ ] Validate narrow tests, then full lint/type/test/build.
- [ ] Commit and stop for review.

### Task 5: End-to-end runner and calibration outputs

**Files:**
- Create: `src/leads_discovery/config.py`
- Create: `src/leads_discovery/pipeline/runner.py`
- Create: `src/leads_discovery/cli.py`
- Create: `src/leads_discovery/__main__.py`
- Create: `tests/test_runner.py`
- Create: `tests/test_budget_resume.py`
- Create: `README.md`
- Modify: `PLANS.md`

**Interfaces:**
- Consumes: all previous stage interfaces.
- Produces: `run` and local-only `score` CLI commands, run directories, CSV/JSON outputs, checkpoint state, and usage report.

- [ ] Write a fake-provider end-to-end test for discovery → dedup → research → extraction → scoring → output views.
- [ ] Write a budget-exhaustion test proving state persists before pause and resume skips completed paid work.
- [ ] Implement configuration loading without ever logging secrets.
- [ ] Implement runner with a default 100-candidate discovery cap and 20-company evaluation cap.
- [ ] Implement ranked/rejected/uncertain CSV views and usage report.
- [ ] Document setup, `.env`, commands, output semantics, and calibration workflow.
- [ ] Run full lint, typecheck, tests, and build.
- [ ] Update `PLANS.md` milestone status.
- [ ] Open PR with a high-level DAG diagram of the completed local scope; no frontend screenshot is needed because this project has no frontend.

## Self-review

- Spec coverage: all milestone requirements map to Tasks 1–5.
- Missing-value handling is explicitly tested in Task 4.
- Budget pause/resume and no-repeat paid work are explicitly tested in Task 5.
- Provider optionality is covered in Task 2.
- Provenance survives dedup/research through canonical records defined in Task 1.
- No contact-enrichment provider is included in any task.
