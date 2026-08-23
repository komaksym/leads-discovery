# M3 Evaluation, Orchestration, and Calibration Design

**Status:** draft implementation contract for review, Revision 1  
**Authority:** this document governs M3 when it is more specific than `PLANS.md` or the
foundational product design. The merged M2 contract remains authoritative for discovery,
research, extraction, paid-call persistence, and provider budgets.

## Purpose and boundary

M3 is the third and final implementation milestone. It is delivered and reviewed as one
atomic module, not as sub-milestones or stacked feature sections.

M2 produces canonical companies with public evidence, extracted fact values, confidence,
and evidence citations. M3 consumes those persisted facts and finishes the product:

```text
M2 companies_extracted.jsonl
        |
        v
validate cited facts
        |
        v
coverage + deterministic scoring
        |
        v
hard rejection + conservative decision policy
        |
        v
evaluated JSONL + ranked/rejected/uncertain CSV
        |
        v
manual A/B/C labels -> local calibration report
```

M3 adds no discovery source, extraction model, database, frontend, contact enrichment,
outreach, CRM integration, or autonomous agent. Scoring and calibration are local and add
zero Exa, Apify, or DeepSeek spend. The full `run` command may invoke the existing M2 live
batch, subject to M2's independent provider budgets and explicit live-execution controls.

## Product invariants

| ID | Requirement |
| --- | --- |
| M3-INV-01 | Scoring and decisions are deterministic local code. An LLM never assigns a score or decision. |
| M3-INV-02 | Unknown is not false. Missing, malformed, unsupported, uncited, or insufficiently confident facts contribute neither positive nor negative points. |
| M3-INV-03 | Score and coverage remain separate. A high score from sparse evidence cannot pass the acceptance gate. |
| M3-INV-04 | Every selected company whose extraction stage is complete receives exactly one of `accepted`, `rejected`, or `uncertain`. |
| M3-INV-05 | Only a strong hard-rejection rule produces `rejected`. A low score alone produces `uncertain`. |
| M3-INV-06 | Acceptance is precision-first: every acceptance gate must pass; otherwise the company is `uncertain`. |
| M3-INV-07 | M3 never mutates or truncates M2 raw, deduplicated, research, extraction, or usage ledgers. It writes derived artifacts separately. |
| M3-INV-08 | Local recomputation makes no network call, reads no provider credential, and emits no usage event. |
| M3-INV-09 | At most 20 extracted companies are evaluated per run. No command silently expands that cap. |
| M3-INV-10 | A provider pause does not discard free work: all already completed extractions are evaluated and exported before the full runner returns the original paused status. |
| M3-INV-11 | Exa, Apify, and DeepSeek budgets remain independent. M3 introduces no aggregate budget and cannot reset spend on resume. |
| M3-INV-12 | Derived artifacts are atomically replaced, deterministic for identical inputs and policy, and protected against symlink/path escape. |
| M3-INV-13 | Calibration reports disagreements but never edits the policy or evaluated records automatically. |
| M3-INV-14 | Automated tests use fakes/fixtures only and make no live provider, DNS, credential, or billable call. |
| M3-INV-15 | Existing M1/M2 serialized company records remain loadable after additive M3 model fields are introduced. |

## Score versus coverage

These values answer different questions:

```text
score     = how positive are the usable facts we have?
coverage  = how much of the configured evidence weight is usable?
```

Each category has subfeatures whose local weights total `100`. If usable facts cover
subfeatures worth `60`, category coverage is `0.60`. The category score is normalized only
over those usable weights, so unknowns are not converted to zero. Overall coverage is the
category-weighted fraction of usable evidence.

For category `c`:

```text
category_coverage[c]
  = usable_subfeature_weight[c] / configured_subfeature_weight[c]

category_score[c]
  = sum(usable_weight * signal_score) / usable_subfeature_weight[c]
```

The final score weights a category by both its product weight and its coverage. This stops a
single known subfeature from receiving the influence of a fully researched category:

```text
effective_category_weight[c]
  = category_product_weight[c] * category_coverage[c]

final_score
  = sum(category_score[c] * effective_category_weight[c])
    / sum(effective_category_weight[c])

overall_coverage
  = sum(category_product_weight[c] * category_coverage[c])
    / sum(category_product_weight[c])
```

Scores are represented on `0..100`; coverage and confidence are represented on `0..1`.
Decisions use unrounded values. Persistence and CSV display round scores to two decimal
places and coverage to four decimal places.

If no scored category has usable evidence, `final_score` is `None`; the company cannot be
accepted and receives `score_unavailable`. It is still eligible for a strong hard rejection.

Example: a company may score `82` on the facts that are known while covering only `0.55` of
the configured evidence. It is `uncertain`, not accepted.

## Usable-fact contract

M3 reads each fact from `CompanyRecord.features` and its matching object from
`CompanyRecord.feature_confidence`. A non-null fact is usable only when all conditions hold:

1. the confidence is a finite number in `0..1` and is at least `0.60`;
2. `evidence_ids` is a duplicate-free list of strings;
3. at least one evidence ID is present and every ID exists in the company's retained
   `EvidenceItem` collection;
4. the value has a supported key-specific type and range.

An M2-standard unknown is exactly `value=None`, confidence `0`, and no evidence IDs. Any
malformed or unsupported fact is treated as unknown and adds a stable
`invalid_fact:<fact_key>` review reason. It does not abort evaluation of other companies.
Booleans are never accepted as numbers. NaN, infinity, negative counts, arbitrary numeric
strings, and ambiguous free-form categories are unsupported.

The supported local transforms are:

| Transform | Accepted value | Signal score |
| --- | --- | --- |
| positive boolean | `bool` | `True=100`, `False=0` |
| inverted boolean | `bool` | `False=100`, `True=0` |
| workload branch count | positive integer | `1=25`, `2..5=60`, `6..15=85`, `16+=100` |
| economic branch count | positive integer | `1=40`, `2..15=100`, `16..30=70`, `31+=50` |
| employee count | positive integer | `<10=20`, `10..19=60`, `20..150=100`, `151..500=70`, `501+=50` |
| reliable revenue USD | positive finite number | `<1m=20`, `1m..<5m=50`, `5m..100m=100`, `>100m..500m=70`, `>500m=50` |
| manufacturer breadth | nonnegative integer, list of strings, or `none/narrow/moderate/broad` | count `0=0`, `1..4=25`, `5..9=50`, `10..19=75`, `20+=100`; categories `0/25/60/100` |

For manufacturer breadth, a list is scored by the number of distinct nonblank case-folded
values. Only the four exact case-insensitive categorical words above are accepted as strings.

## Default scoring policy

The category product weights are fixed in one inspectable `DEFAULT_POLICY` object:

```text
workload / pain likelihood   40
economic fit                 25
low incumbent exposure       25
direct pain evidence         10
```

Subfeature weights total `100` inside each category:

| Category | Fact | Weight | Transform |
| --- | --- | ---: | --- |
| workload | `rfq_or_quote_workflow_evidence` | 25 | positive boolean |
| workload | `inside_sales_or_estimating_presence` | 15 | positive boolean |
| workload | `project_or_tender_business` | 15 | positive boolean |
| workload | `bom_or_line_item_complexity` | 15 | positive boolean |
| workload | `manufacturer_count_or_breadth` | 10 | manufacturer breadth |
| workload | `branch_count` | 10 | workload branch count |
| workload | `relevant_hiring` | 5 | positive boolean |
| workload | `industrial_or_process_customer_focus` | 5 | positive boolean |
| economic | `employee_count` | 35 | employee count |
| economic | `branch_count` | 25 | economic branch count |
| economic | `multi_location_signal` | 20 | positive boolean |
| economic | `regional_independent_signal` | 15 | positive boolean |
| economic | `revenue_if_reliably_available` | 5 | reliable revenue USD |
| incumbent | `known_current_direct_competitor_customer` | 60 | inverted boolean |
| incumbent | `known_quote_automation_or_order_automation_relationship` | 40 | inverted boolean |
| direct pain | `direct_quotation_pain_evidence` | 40 | positive boolean |
| direct pain | `manual_workflow_evidence` | 35 | positive boolean |
| direct pain | `explicit_process_bottleneck_evidence` | 25 | positive boolean |

`pvf_relevant` is a critical decision gate, not a scored proxy. `pvf_product_breadth` remains
inspectable context because its M2 value shape is not sufficiently constrained for reliable
initial scoring. `known_competitor_evaluation_history` also remains review context: a known
historical evaluation adds `competitor_history_review` but never lowers the score or triggers
rejection.

The policy is immutable at runtime and has version `m3-v1`. All weights, transforms, and
thresholds live together; magic threshold copies elsewhere are forbidden. Every evaluated
artifact and calibration report identifies the policy version.

## Hard rejection policy

The hard-rejection confidence threshold is `0.85`. A fact-based rejection cites the union of
the triggering facts' evidence IDs in deterministic order.

| Code | Exact trigger |
| --- | --- |
| `confirmed_not_pvf_relevant` | usable `pvf_relevant is False` with confidence `>=0.85` |
| `confirmed_outside_us_canada` | nonblank canonical country outside `US/CA` and a retained discovery record reports the same country code |
| `confirmed_inactive_or_dead` | canonical status is exactly `inactive` or `dead` |
| `confirmed_current_direct_competitor_customer` | usable `known_current_direct_competitor_customer is True` with confidence `>=0.85` |
| `confirmed_too_small_for_meaningful_quote_workload` | all four facts are usable at confidence `>=0.85`: `employee_count <10`, `branch_count ==1`, `inside_sales_or_estimating_presence is False`, and `rfq_or_quote_workflow_evidence is False` |

No missing employee, branch, revenue, workload, geography, status, or incumbent fact can fire
a rejection. Multiple hard rules may be retained, but the final decision remains one
`rejected` value.

## Decision policy

Evaluate hard rejection first. If any hard rule fires, set `final_decision="rejected"`.

Otherwise, `accepted` requires every gate below:

| Gate | Initial value |
| --- | ---: |
| usable `pvf_relevant is True` | confidence `>=0.75` |
| final score | `>=70` |
| overall coverage | `>=0.70` |
| workload coverage | `>=0.60` |
| economic coverage | `>=0.50` |
| incumbent resolution | at least one incumbent fact is usable, and neither usable incumbent fact is `True` |

If no hard rule fires and any acceptance gate fails, set `final_decision="uncertain"`. This is
intentional: score `72` with overall coverage `0.55` is uncertain. A well-covered low score is
also uncertain until manual calibration provides evidence for a soft-rejection rule.

Stable review reason codes identify every failed acceptance gate:

```text
pvf_relevance_unresolved
score_below_acceptance
score_unavailable
low_overall_coverage
low_workload_coverage
low_economic_coverage
incumbent_exposure_unresolved
incumbent_exposure_ambiguous
competitor_history_review
invalid_fact:<fact_key>
```

Stable rejection reason codes are the hard-rule codes above. Evaluation replaces prior M3
reasons instead of accumulating stale results across recomputation.

## Additive persisted model

Add the following backward-compatible model:

```python
DecisionKind = Literal["review", "rejection"]

@dataclass(slots=True)
class DecisionReason:
    code: str
    kind: DecisionKind
    explanation: str
    confidence: float | None = None
    evidence_ids: list[str] = field(default_factory=list)
```

Add these defaulted fields to `CompanyRecord` without removing or renaming existing fields:

```python
decision_reasons: list[DecisionReason] = field(default_factory=list)
evaluation_policy_version: str | None = None
```

`review_reasons` and `rejection_reasons` remain lists of stable codes for simple consumers.
`decision_reasons` supplies human-readable explanations and retained evidence citations.
Country/status structural rules may have an empty `evidence_ids` list and explain the
canonical/discovery provenance used. All constructors and `to_dict`/`from_dict` operations
must remain defensive, and old payloads missing the new fields must round-trip successfully.

Evaluation updates only:

```text
coverage
score_components
final_score
final_decision
review_reasons
rejection_reasons
decision_reasons
evaluation_policy_version
stage_status["scoring"]
stage_status["decision"]
updated_at
```

It preserves identity, discovery provenance, evidence, extracted features/confidence,
research/extraction status, and M2 artifacts.

## Frozen public evaluation API

Implement `src/leads_discovery/scoring/policy.py` and re-export these names from
`leads_discovery.scoring`:

```python
FinalDecision = Literal["accepted", "rejected", "uncertain"]

@dataclass(frozen=True, slots=True)
class ScoringPolicy:
    version: str = "m3-v1"
    minimum_fact_confidence: float = 0.60
    critical_relevance_confidence: float = 0.75
    hard_rejection_confidence: float = 0.85
    acceptance_score: float = 70.0
    minimum_overall_coverage: float = 0.70
    minimum_workload_coverage: float = 0.60
    minimum_economic_coverage: float = 0.50

DEFAULT_POLICY: Final[ScoringPolicy]

def evaluate_company(
    company: CompanyRecord,
    policy: ScoringPolicy = DEFAULT_POLICY,
) -> CompanyRecord: ...

def evaluate_companies(
    companies: Iterable[CompanyRecord],
    *,
    limit: int = 20,
    policy: ScoringPolicy = DEFAULT_POLICY,
) -> tuple[CompanyRecord, ...]: ...
```

`ScoringPolicy` validates finite thresholds and legal ranges at construction. Policy callers
may lower or raise thresholds for an explicit experiment, but the feature catalog and
transforms remain the versioned `m3-v1` catalog. `evaluate_company` returns a detached copy
and never mutates its argument. It requires extraction status `completed`; otherwise it
raises `ValueError` before evaluation. `evaluate_companies` accepts `limit` only in `1..20`,
rejects duplicate company IDs, selects extraction-complete records in ascending company ID
order, and returns results in that same stable order.

`score_components` uses exactly these keys when the corresponding category has usable facts:

```text
workload
economic_fit
low_incumbent_exposure
direct_pain
```

`coverage` always uses exactly:

```text
workload
economic_fit
low_incumbent_exposure
direct_pain
overall
```

## Derived artifact contract

M3 reads the latest snapshot per company from `companies_extracted.jsonl`. It writes these
additional run-local artifacts:

```text
data/<run_id>/
  companies_evaluated.jsonl
  companies_ranked.csv
  companies_rejected.csv
  companies_uncertain.csv
  calibration_template.csv
  calibration_report.json       # only after calibrate
  companies_calibrated.csv      # only after calibrate
  run_summary.json
```

`companies_evaluated.jsonl` contains exactly one complete canonical record per evaluated
company, sorted by company ID. It is atomically replaced, never appended, so recomputation
cannot create stale duplicate snapshots.

CSV ranking order is:

1. decision order `accepted`, `uncertain`, `rejected`;
2. final score descending;
3. overall coverage descending;
4. normalized name, then company ID ascending.

Every CSV uses UTF-8, a header, `\n` line endings, and RFC 4180 quoting. Externally sourced
text whose first non-whitespace character is `=`, `+`, `-`, or `@` is prefixed with an
apostrophe to prevent spreadsheet formula execution. JSON retains the original text.

`companies_ranked.csv` contains all evaluated companies. The rejected and uncertain views
filter that same ordered data. Exact columns are:

```text
company_id,name,domain,country,policy_version,
workload_score,workload_coverage,
economic_fit_score,economic_fit_coverage,
low_incumbent_exposure_score,low_incumbent_exposure_coverage,
direct_pain_score,direct_pain_coverage,
overall_coverage,final_score,final_decision,
review_reasons,rejection_reasons
```

Missing category scores serialize as an empty CSV cell. Reason lists use `;` between stable
codes. `calibration_template.csv` adds blank `manual_label` and `manual_notes` columns to the
ranked view.

`run_summary.json` records policy version, M2 checkpoint status, evaluated/accepted/rejected/
uncertain counts, artifact paths relative to the run directory, and the existing `usage.json`
provider totals. It must label all costs using the exact/estimated/null semantics already
provided by `CostTracker`; it never fabricates a combined exact spend from incomplete events.

## Local evaluation runner

Implement `src/leads_discovery/pipeline/evaluation.py` with this public API:

```python
@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    run_id: str
    data_root: Path
    max_evaluated: int = 20

@dataclass(frozen=True, slots=True)
class EvaluationSummary:
    run_id: str
    policy_version: str
    evaluated_count: int
    accepted_count: int
    rejected_count: int
    uncertain_count: int
    artifact_paths: tuple[Path, ...]

def evaluate_run(
    config: EvaluationConfig,
    *,
    policy: ScoringPolicy = DEFAULT_POLICY,
) -> EvaluationSummary: ...
```

Validation reuses the M2 run-ID grammar and requires the run directory to remain directly
beneath the resolved data root. Run directories and all M3 artifact targets must not be
symlinks. Configuration is validated before any write.

`evaluate_run` loads the durable checkpoint and only latest extraction-complete snapshots,
enforces the `1..20` cap, evaluates them, and atomically writes every non-calibration derived
artifact. An empty set is a valid partial result while M2 is running or paused, but is an
error when the durable checkpoint reports `completed`. A failed company fact becomes
`uncertain`; one malformed fact cannot erase successfully evaluated peers.

## Full CLI and orchestration

Add `src/leads_discovery/cli.py` and `src/leads_discovery/__main__.py` so the supported user
surface is:

```text
python -m leads_discovery run --run-id RUN --deepseek-budget-usd USD [M2 options] --execute-live
python -m leads_discovery score --run-id RUN [--data-root PATH] [--max-evaluated 20]
python -m leads_discovery calibrate --run-id RUN --labels PATH [--data-root PATH]
```

`run` accepts and forwards M2's `max-candidates`, `include-apify`, `apify-budget-usd`,
`deepseek-budget-usd`, `exa-budget-usd`, data root, and explicit `execute-live` behavior. It
maps `max-evaluated` directly to M2's `max_extracted`, so there is only one `1..20` cap.
Credentials are read only for `run --execute-live`. Missing optional Apify credentials disable
Apify exactly as in M2. No combined budget is accepted.

After M2 returns, `run` invokes local evaluation for every completed extraction even when the
checkpoint is `paused_budget` or `paused_unknown`. It preserves the paid-stage pause status,
pending company/stage, and reason. If M2 completed and evaluation succeeds, it marks
`provider_state.stages["evaluation"]` and `["m3_pipeline"]` completed and leaves status
`completed`. Re-running is safe: M2 resumes/skips paid work and M3 atomically recomputes free
derived artifacts.

`score` requires existing extracted artifacts, does not instantiate HTTP clients or provider
adapters, and does not read environment credentials. `calibrate` has the same zero-network
boundary. The old narrow `python -m leads_discovery.pipeline.m2_batch` entry remains supported.

CLI exit codes are:

```text
0  completed local/full operation or authorized dry run
1  invalid input, malformed state, or failed operation
2  durable paused_budget or paused_unknown full run
```

Every command prints one sanitized JSON summary. It never prints credentials, raw provider
responses, complete evidence text, or unsafe exception chaining.

M3 does not alter M2's paid-call retry semantics. Retryable provider errors retain pending
state for explicit re-invocation; ambiguous in-flight Exa/DeepSeek work remains
`paused_unknown` rather than being duplicated automatically.

## Calibration contract

Implement `src/leads_discovery/calibration.py` with:

```python
ManualLabel = Literal["A", "B", "C"]

@dataclass(frozen=True, slots=True)
class CalibrationSummary:
    run_id: str
    policy_version: str
    evaluated_count: int
    labeled_count: int
    unlabeled_count: int
    critical_disagreement_count: int
    report_path: Path
    joined_csv_path: Path

def calibrate_run(
    config: EvaluationConfig,
    *,
    labels_path: Path,
) -> CalibrationSummary: ...
```

The label CSV requires `company_id,manual_label` and may include `manual_notes`. It may retain
the other read-only context columns from `calibration_template.csv`; those columns are ignored
on import and regenerated from evaluated data on export. Values are trimmed and labels are
uppercased before exact validation against `A/B/C`. Duplicate header names, duplicate IDs,
unknown company IDs, blank supplied labels, formula-like company IDs, and malformed CSV fail
before output mutation. Partial labeling is allowed and reported.

`calibration_report.json` contains:

- policy version and evaluated/labeled/unlabeled counts;
- a complete `A/B/C` by `accepted/rejected/uncertain` count matrix;
- score and coverage summary values per manual label when present;
- critical disagreement IDs (`manual A + rejected`, `manual C + accepted`);
- review disagreement IDs (`manual A + uncertain`, `manual B + accepted/rejected`,
  `manual C + uncertain`).

`companies_calibrated.csv` is the ranked view joined with sanitized manual label/notes.
Calibration never changes `DEFAULT_POLICY`, decisions, scores, evidence, checkpoints, or usage.
A human reviews the report and proposes a separate policy revision if thresholds or weights
should change.

## Error, safety, and idempotency behavior

- Validate run IDs, caps, policy values, label schemas, input existence, and target paths before
  writing anything.
- Reject symlinked run directories, M3 outputs, or labels files.
- Never follow output symlinks, expose absolute artifact paths in persisted reports, or allow a
  run ID to escape `data_root`.
- Write each derived file to a same-directory temporary file, fsync it, atomically replace the
  target, and fsync the parent where supported.
- Reject non-object JSONL rows, duplicate company IDs, non-finite persisted numbers, malformed
  canonical records, and conflicting policy versions with clear sanitized errors.
- Preserve prior complete derived artifacts if recomputation fails before replacement.
- Do not catch `BaseException`; do not silently convert run-level corruption into an uncertain
  lead.

## Required implementation surface

The expected production changes are:

```text
src/leads_discovery/models.py
src/leads_discovery/scoring/__init__.py
src/leads_discovery/scoring/policy.py
src/leads_discovery/pipeline/evaluation.py
src/leads_discovery/calibration.py
src/leads_discovery/cli.py
src/leads_discovery/__main__.py
src/leads_discovery/pipeline/state.py        # only shared atomic writers if needed
README.md
.env.example                                 # only if current variables are incomplete
PLANS.md                                     # mark M3 complete only after all gates pass
```

No new production dependency is expected. Follow existing dataclass, defensive-copy,
path-safety, persistence, and strict typing conventions. Every function has a useful
docstring. Unrelated refactoring is out of scope.

## Verification contract

Independent tests must prove at least:

1. unknown, malformed, low-confidence, uncited, and unsupported facts never become zero;
2. exact subfeature/category formulas, boundary transforms, effective weights, rounding, and
   deterministic ordering;
3. score and coverage diverge correctly, including score `>=70` with coverage `<0.70` becoming
   uncertain;
4. every acceptance gate individually forces uncertain when it fails;
5. each hard rule requires its complete high-confidence trigger and retains citations;
6. historical competitor evaluation is review-only;
7. low score alone never rejects;
8. recomputation replaces stale M3 values without mutating M2 facts or its append-only files;
9. old M1/M2 model payloads load and new nested reason objects defensively round-trip;
10. evaluation caps, duplicate IDs, empty/partial runs, resume, and paid-pause preservation;
11. exact JSONL/CSV schemas, stable sort/ties, blank missing scores, formula neutralization,
    Unicode, commas, quotes, and newlines;
12. calibration matrix/disagreement rules, partial labels, and malformed/duplicate/unknown
    label rejection;
13. `score` and `calibrate` never read credentials, instantiate provider clients, touch usage,
    or make network/DNS calls;
14. full orchestration evaluates completed companies after budget/unknown pauses and returns
    the documented exit status;
15. run/path/artifact/label symlink and traversal defenses;
16. constructor defensive copies and policy validation reject booleans, NaN, and infinity;
17. existing M1/M2 tests remain green.

Tests may use temporary directories, representative persisted fixtures, injected clocks where
timestamps matter, and subprocess CLI checks. Normal tests must not require credentials or
internet access.

Before completion run, in order:

```text
ruff check .
mypy src tests
pytest
python -m build
```

Missing local developer tools are not a reason to abandon or withhold a browser-agent change:
the agent still commits, pushes, opens a draft PR, and reports which gates are delegated to
GitHub CI. Final integration and merge, however, require all four gates to pass on the combined
production-plus-test candidate and a green adversarial review.

## Completion criteria

M3 is complete only when:

1. the full CLI runs or resumes M2 and derives M3 outputs without duplicating paid work;
2. up to 20 completed extractions receive deterministic coverage, scores, and exactly one
   decision;
3. sparse or materially ambiguous candidates become uncertain, including the approved
   score-72/coverage-55 example;
4. rejection requires strong inspectable evidence and accepted leads pass every precision gate;
5. evaluated/ranked/rejected/uncertain outputs and usage/run summaries are inspectable and
   deterministic;
6. scoring and calibration work offline for zero provider spend;
7. manual A/B/C labels produce a useful disagreement report without automatic policy mutation;
8. documentation explains setup, independent budgets, live authorization, resume, outputs,
   coverage versus score, and calibration;
9. the combined candidate passes lint, typecheck, tests, build, and post-integration red team;
10. `PLANS.md` marks M3 complete only after those checks are green.
