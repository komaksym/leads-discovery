# PVF Company Discovery & Ranking — Milestone 1 Design

> **Foundational product design:** The product thesis remains relevant, but milestone
> boundaries are superseded by `PLANS.md`. M2 implementation authority lives in
> `docs/superpowers/specs/2026-08-23-m2-discovery-deduplication-design.md`.

## Goal

Build and validate a cheap, mostly automated Python pipeline that discovers, researches, evaluates, and deterministically ranks North American PVF distributors for startup validation.

This milestone ends at company-level evaluation and ranking. It does not perform people/contact enrichment.

The calibration run should fully evaluate approximately 20 distinct deduplicated U.S./Canadian companies, while discovery may produce a substantially larger raw candidate pool.

## Product thesis

The target account is not merely obscure. The desired intersection is:

```text
meaningful quotation/RFQ workload
+
enough economic value to pay
+
relatively low incumbent exposure
```

Because distributors rarely state that quoting is painful, the system should infer likely pain from observable workload and business structure: branches, inside-sales/estimating staff, RFQ/BOM/project workflows, catalog complexity, manufacturers represented, industrial/process customers, tender/project business, and relevant hiring.

Core invariants:

- Unknown is not false.
- Missing evidence is never silently converted to a negative feature.
- Hard rejection requires reasonably strong evidence.
- Roughly 2–15 locations and 20–150 employees are ranking priors, not hard inclusion rules.
- A company that previously evaluated and rejected a competitor is not automatically rejected.
- Rejected and uncertain companies remain in canonical storage with provenance.

## Architecture decision

Use a simple staged file pipeline with small provider adapters.

Do not use a database, frontend, dashboard, LangChain, or an agent framework for this milestone.

```text
query generation
    ↓
discovery (Exa primary, Apify optional)
    ↓
raw candidate pool
    ↓
deduplication
    ↓
canonical company records
    ↓
web research / evidence collection
    ↓
DeepSeek structured fact extraction
    ↓
deterministic scoring + decision policy
    ↓
accepted / rejected / uncertain views
    ↓
CSV/JSON outputs + usage/cost report
```

Each expensive stage persists completed work so a later run can resume without repeating successful API calls.

## Repository structure

Initial structure:

```text
src/leads_discovery/
  discovery/
    base.py
    exa.py
    apify.py
    queries.py
  research/
    evidence.py
    extract.py
  scoring/
    features.py
    policy.py
  pipeline/
    runner.py
    state.py
    costs.py
  models.py
  cli.py

tests/
  test_deduplication.py
  test_scoring.py
  test_decisions.py
  test_resume.py
  test_costs.py

.env.example
pyproject.toml
README.md
```

The exact file split may be simplified during implementation if a file would otherwise contain trivial boilerplate, but the logical boundaries must remain clear.

## Provider strategy

### Exa

Primary provider for company discovery and web/evidence search.

The pipeline must not assume Exa is the only possible discovery source. Exa-specific response parsing stays behind an adapter boundary.

### Apify

Optional complementary discovery provider.

If `APIFY_TOKEN` is absent, Apify is skipped without failing the run.

If Apify credits are exhausted, its own provider path stops cleanly. Equivalent Exa-based discovery may continue because Apify is optional.

### DeepSeek

Used only for structured extraction from collected evidence.

The LLM does not assign final lead scores or final decisions. It returns schema-constrained facts, evidence-linked fields, confidence, and explicit unknowns.

### Clay / Apollo / Instantly

Not used in this milestone. No people/contact enrichment credits are spent.

## Configuration

Use environment variables loaded from `.env` locally and normal environment variables in other environments.

Expected configuration includes:

```text
EXA_API_KEY
DEEPSEEK_API_KEY
APIFY_TOKEN              # optional

MAX_EVALUATED_COMPANIES=20
MAX_DISCOVERY_CANDIDATES=100
EXA_BUDGET_USD            # optional local run ceiling; unset means no extra local ceiling
DEEPSEEK_BUDGET_USD       # optional local run ceiling; unset means no extra local ceiling
APIFY_BUDGET_USD          # optional local run ceiling; unset means no extra local ceiling
```

Provider budget ceilings are run-level safety limits in addition to provider-side credit exhaustion. The default calibration discovers at most 100 raw candidates and fully evaluates at most 20 deduplicated companies unless the user explicitly overrides those limits.

No credentials are committed.

## Run model and persistence

Each run gets a stable `run_id` and directory:

```text
data/<run_id>/
  companies_raw.jsonl
  companies_deduped.jsonl
  companies_evaluated.jsonl
  companies_ranked.csv
  companies_rejected.csv
  companies_uncertain.csv
  usage.json
  checkpoint.json
```

### Canonical record

Every deduplicated company gets one canonical company record containing at least:

```text
company_id
name
normalized_name
domain
normalized_domain
country
locations_if_known
status

discovery_sources[]
discovery_queries[]
discovery_records[]

evidence[]
features
feature_confidence
coverage

score_components
final_score
final_decision
review_reasons[]
rejection_reasons[]

stage_status
created_at
updated_at
```

`final_decision` is one of:

```text
accepted
rejected
uncertain
```

No record is destructively removed because of its decision.

### Checkpoint semantics

A company/stage result is persisted immediately after successful completion of an expensive operation.

The runner should be idempotent at the practical milestone level:

- already completed discovery results are not re-requested unless explicitly forced;
- already researched companies are not researched again by default;
- already extracted companies are not sent to DeepSeek again by default;
- scoring can always be recomputed locally from persisted extracted features;
- interrupted runs resume from unfinished work.

`checkpoint.json` records run-level stage progress and provider pause reason. Canonical company records additionally carry per-stage status so recovery does not depend only on one mutable cursor.

## Budget exhaustion and pause behavior

There are two distinct cases.

### Optional provider exhaustion

If an optional provider such as Apify hits its budget/credits:

1. Persist the provider error and usage state.
2. Mark that provider unavailable for the remainder of the run.
3. Continue only if equivalent pipeline work can proceed through available providers.

### Required provider exhaustion

If the currently required work cannot proceed because Exa or DeepSeek has reached its configured budget, account credits, or an explicit provider quota:

1. Finish writing all already completed result records.
2. Persist usage/cost accounting.
3. Persist the exact pending stage/company.
4. Mark the run `paused_budget` with a machine-readable reason.
5. Exit cleanly with a documented pause exit status distinct from success.
6. On the next run with budget restored, resume without repeating completed paid work.

Transient provider/network errors are not automatically treated as budget exhaustion; they follow bounded retry behavior and, if unresolved, leave the affected company/stage pending or failed with provenance.

## Discovery

Discovery is automated and query-driven.

### Query generation

Programmatically generate a compact set of complementary queries rather than repeatedly searching only for `PVF distributor`.

Query families should cover concepts such as:

- PVF / pipe-valve-fitting distributors;
- industrial pipe/valve/fitting suppliers;
- process piping distributors;
- valve/actuation distributors;
- industrial flow-control suppliers;
- regional industrial supply companies with PVF lines;
- manufacturer/distributor relationships;
- project/quotation/RFQ language;
- geographic variants across U.S. and Canada.

The generated query string is stored with every discovery record.

The goal is breadth with controlled cost, not exhaustive search-engine crawling.

### Raw candidate model

A raw discovery record should preserve:

```text
provider
query
provider_result_id if available
name
domain/url if available
location/country if available
snippet/title if available
raw metadata needed for debugging
```

Discovery should over-generate relative to the 20-company evaluation target so deduplication and rejection do not starve calibration.

## Deduplication

Deduplicate before expensive research wherever practical.

Primary key:

```text
normalized registrable domain
```

Fallback when no reliable domain exists:

```text
normalized company name + normalized location/country
```

Normalization should remove common URL noise (`www`, paths, tracking params) and company suffix noise where safe for fallback matching.

Duplicates are merged into one canonical company while preserving all discovery provenance, queries, source URLs, and provider records.

Deduplication must avoid aggressive fuzzy matching that could merge distinct distributors with similar names.

## Research and evidence collection

For each canonical candidate selected for evaluation, collect public evidence for five groups:

1. PVF relevance
2. probable RFQ/quotation workload
3. economic fit
4. incumbent exposure
5. direct pain evidence when available

Evidence search should prefer source diversity and high-signal pages over indiscriminate crawling.

Likely sources include:

- company website / about / locations / products / industries;
- line cards and supplier PDFs;
- manufacturer dealer/distributor pages;
- job postings;
- association/member pages;
- public directory/company pages;
- pages mentioning RFQ, quotation, estimating, projects, BOMs, inside sales, tenders, or ERP/e-commerce automation.

Each evidence item retains:

```text
url
title/snippet/text excerpt
source type/provider
retrieved_at
```

The research layer collects evidence; it does not directly score companies.

## Structured extraction

DeepSeek receives a bounded evidence bundle for one company and returns structured facts.

Important extracted values should be represented as objects with:

```text
value: <typed value or null>
confidence: <0..1 or categorical equivalent>
evidence_ids: [...]
```

The schema should include facts such as:

### Relevance

```text
pvf_relevant
pvf_product_breadth
industrial_or_process_customer_focus
```

### Workload / pain proxies

```text
branch_count
inside_sales_or_estimating_presence
rfq_or_quote_workflow_evidence
project_or_tender_business
bom_or_line_item_complexity
manufacturer_count_or_breadth
relevant_hiring
```

### Economic fit

```text
employee_count
revenue_if_reliably_available
regional_independent_signal
multi_location_signal
```

### Incumbent exposure

```text
known_current_direct_competitor_customer
known_competitor_evaluation_history
known_quote_automation_or_order_automation_relationship
```

### Direct pain

```text
direct_quotation_pain_evidence
manual_workflow_evidence
explicit_process_bottleneck_evidence
```

If evidence does not support a value, DeepSeek must return `null`/unknown rather than infer a negative.

Every material fact must point back to supporting evidence IDs where support exists.

## Deterministic scoring

Scoring is pure local code and fully recomputable from extracted records.

Starting category weights:

```text
workload / pain likelihood   40%
economic fit                 25%
low incumbent exposure       25%
direct pain evidence         10%
```

These weights live in one inspectable configuration/policy structure.

### Missing-value policy

Unknown features do not receive zero by default.

For each score category:

1. Score only known subfeatures.
2. Normalize over the available configured subfeature weight within that category.
3. Separately compute category coverage as `known_weight / total_weight`.
4. Compute overall evidence coverage from category/subfeature coverage.
5. Preserve both score and coverage; do not hide low evidence behind a high normalized score.

Example:

```text
Known workload signals:
  RFQ workflow       positive
  project business   positive
Unknown:
  branch count
  inside sales count

→ workload score is calculated from the known workload signals only
→ workload coverage remains low/moderate
→ decision policy may classify the company uncertain because evidence is incomplete
```

This prevents `unknown = negative` while still allowing ranking among partially known companies.

### Size priors

Roughly 2–15 locations and 20–150 employees contribute positively when known, but are soft priors.

Outside these ranges does not automatically imply rejection or a monotonic penalty. The economic-fit score should favor plausible purchasing power and quotation workload rather than raw company size.

### Incumbent exposure

Confirmed current use/customer relationship with a direct competitor is a strong negative and may trigger hard rejection.

No evidence of incumbent exposure is **unknown**, not automatically proof of low exposure. Low-incumbent score therefore needs positive/credible absence-oriented evidence or otherwise carries lower coverage/confidence.

A historical evaluation/rejection of a competitor may be neutral or positive context and must not trigger hard rejection by itself.

## Hard rejection policy

Hard rejection is rule-based and requires sufficiently strong evidence.

Allowed initial hard-rejection reasons:

```text
confirmed_not_pvf_relevant
confirmed_outside_us_canada
confirmed_inactive_or_dead
confirmed_current_direct_competitor_customer
confirmed_too_small_for_meaningful_quote_workload
```

The last rule should require strong evidence, not a single weak size estimate.

Missing employee count, branch count, revenue, or incumbent data never triggers hard rejection.

Every hard rejection stores:

```text
reason code
evidence IDs
confidence
human-readable explanation
```

## Decision policy

Every fully evaluated company receives exactly one decision.

### Rejected

Any strong hard-rejection rule fires.

### Accepted

No hard rejection, sufficiently high final score, and sufficient evidence coverage/confidence to trust the ranking.

### Uncertain

No hard rejection, but one or more of the following applies:

- evidence coverage is too low;
- critical PVF relevance/economic/workload evidence remains unresolved;
- incumbent exposure remains materially ambiguous;
- score lies in a review band where evidence is insufficient for acceptance.

Initial score/coverage thresholds are calibration parameters, not business truth. They should live in the policy config and be easy to change after manual A/B/C labeling.

## Selection of the 20-company calibration set

Discovery may yield many candidates. After deduplication, the runner selects up to the configured evaluation cap (`20` by default).

Selection should avoid wasting research budget on obviously invalid records that can be cheaply ruled out from strong discovery evidence, while not converting weak discovery snippets into destructive hard rejections.

The calibration target is approximately 20 companies that reach structured extraction and deterministic scoring. If budget exhaustion occurs first, the run pauses with completed companies retained.

The pipeline must not automatically continue to hundreds of companies after reaching the cap.

## Outputs

### `companies_raw.jsonl`

All raw discovery records with provider/query provenance.

### `companies_deduped.jsonl`

Canonical merged company records before expensive research.

### `companies_evaluated.jsonl`

Canonical evaluated records including evidence, extracted structured facts, confidence, coverage, score components, decisions, and reasons.

### `companies_ranked.csv`

Human-inspection view with one row per evaluated company, including at least:

```text
company_id
name
domain
country
workload_score
workload_coverage
economic_fit_score
economic_fit_coverage
low_incumbent_score
low_incumbent_coverage
direct_pain_score
direct_pain_coverage
overall_coverage
final_score
final_decision
review/rejection reason summary
```

Accepted and uncertain companies can appear in the ranked view; rejected companies retain scores where meaningful but are explicitly marked rejected.

### `companies_rejected.csv`

Convenience view of rejected records only.

### `companies_uncertain.csv`

Convenience view of uncertain records only.

### `usage.json`

Per-provider usage and approximate cost, including where available:

```text
request count
search/result units
tokens in/out
estimated cost
configured run budget
provider-reported or inferred exhaustion state
```

## Cost accounting

Each provider adapter emits usage events into a shared run-level cost tracker.

The cost tracker should work even when exact provider billing units are unavailable: exact known values are stored when provided; estimates are clearly marked as estimates.

The run summary reports total estimated spend and useful pipeline outputs, enabling later comparison of useful leads per dollar and founder-hour.

No Clay, Apollo, or Instantly usage is recorded in this milestone because they are not called.

## CLI

Keep the interface small.

Expected commands/flags can be implemented as one CLI with stages, for example:

```text
python -m leads_discovery run --run-id calibration-001 --max-evaluated 20
python -m leads_discovery score --run-id calibration-001
```

Useful behaviors:

- start a new run;
- resume a prior run by `run_id`;
- recompute scoring locally without paid API calls;
- optionally disable Apify;
- optionally stop after a stage for debugging/calibration.

Avoid a large command surface before calibration proves it useful.

## Error handling

Provider errors are classified into at least:

```text
budget/quota exhausted
rate limited
transient/network
invalid/auth/configuration
permanent provider response error
```

Retries are bounded and only used for retryable failures.

No retry loop should accidentally burn credits indefinitely.

Per-company failures remain represented in state and do not erase completed companies.

## Testing strategy

Unit tests should focus on the business invariants and expensive-call safety rather than mocking every implementation detail.

Required checks:

1. Domain normalization and provenance-preserving deduplication.
2. Fallback deduplication does not aggressively merge distinct companies.
3. Missing features remain unknown and do not score as zero.
4. Category scoring renormalizes over known feature weights while lowering coverage.
5. Hard rejection requires explicit strong evidence.
6. Historical competitor evaluation does not cause automatic rejection.
7. Current direct-competitor customer evidence does cause rejection when confidence threshold is met.
8. Every fully evaluated record becomes accepted/rejected/uncertain.
9. Scoring recomputation requires no provider calls.
10. Resume skips already completed paid work.
11. Required-provider budget exhaustion persists state and returns a clean paused run.
12. Optional Apify exhaustion does not stop equivalent Exa work.
13. Usage/cost aggregation remains consistent after resume.

Provider integration code should be thin enough to test parsing with representative saved payloads/mocks. Real API smoke tests are optional/manual and must not run in the normal unit-test suite.

## Calibration workflow

The intended first run is:

```text
1. Configure Exa + DeepSeek credentials.
2. Optionally configure Apify.
3. Run discovery and over-generate candidates.
4. Deduplicate before research.
5. Fully evaluate approximately 20 companies.
6. Export ranked/evaluated/rejected/uncertain views and usage report.
7. Founder manually labels each evaluated company:
      A = definitely contact
      B = maybe
      C = don't contact
8. Compare manual labels against score, decision, and evidence coverage.
9. Adjust deterministic scoring weights/thresholds.
10. Only after calibration, consider scaling discovery/evaluation and adding people enrichment.
```

Manual A/B/C labels are not required to be part of automated ranking in this milestone; outputs merely need to be easy to label externally or by adding a simple column later.

## Explicit non-goals

Do not implement in Milestone 1:

- people/contact enrichment;
- Clay/Apollo/Instantly workflows;
- phone enrichment;
- outreach;
- CRM synchronization;
- dashboards/frontends;
- a persistent database;
- autonomous agents;
- learned/LLM-generated final scores;
- scaling automatically to hundreds of evaluated companies.

## Done when

Milestone 1 is complete when:

1. The staged pipeline runs discovery → deduplication → research → structured extraction → deterministic scoring → decision outputs.
2. Approximately 20 distinct deduplicated real U.S./Canadian companies have reached scoring, unless a genuine provider budget pause occurs first.
3. Every evaluated company retains evidence/provenance sufficient to inspect how its features were extracted.
4. Missing information remains explicit unknown data and never silently becomes negative evidence.
5. Rejected and uncertain records remain canonical and diagnosable.
6. Scoring is locally recomputable without rediscovery or new LLM calls.
7. Cost/usage reporting covers all providers used in the run.
8. Budget exhaustion checkpoints all completed work and supports resume without repeating completed paid work.
9. Relevant automated tests pass.
10. The run stops at the calibration cap and does not spend people-enrichment credits.
