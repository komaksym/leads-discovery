# M3 Evaluation, Orchestration, and Calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox
> (`- [ ]`) syntax for tracking.

**Goal:** Finish the leads-discovery application with deterministic evidence-aware scoring,
precision-first decisions, resumable full orchestration, safe derived exports, and local
manual-label calibration.

**Architecture:** M2 remains the only paid pipeline and persists cited facts. M3 is a local
derivation layer: pure scoring transforms facts into score/coverage/decision, an evaluation
runner atomically publishes views, calibration compares human labels without mutating policy,
and the top-level CLI composes the existing M2 batch with those free operations.

**Tech Stack:** Python 3.12+, standard-library dataclasses/JSON/CSV/argparse/pathlib, existing
`httpx` and `tldextract`, pytest, Ruff, mypy, setuptools build.

**Spec:** `docs/superpowers/specs/2026-08-23-m3-evaluation-calibration-design.md`

## Summary

M3 is one atomic product module and one final integration PR. Production and independent
contract tests are developed on separate branches from `codex/m3-spec`; they are combined
only after both draft PRs are ready. A red-team pass then probes the combined candidate. The
work is separated for independence, not because M3 is split into product sub-milestones.

## Global Constraints

- Read the complete M3 spec and this plan before changing files.
- Use the current remote `codex/m3-spec` branch as the shared base; do not require an exact
  commit hash or a local machine path.
- Production owns `src/**`, `README.md`, `.env.example`, and the final M3 status change in
  `PLANS.md`; the contract-test branch owns only `tests/**`.
- Production and contract-test agents do not inspect each other's branch, PR, CI, messages,
  or files before integration.
- Red team runs only after both workstreams are complete and reviews their combined result.
- No automated or agent-development run may call Exa, Apify, DeepSeek, DNS, or credentials.
- `score` and `calibrate` are permanently zero-network and zero-provider-spend operations.
- `run` calls providers only with explicit `--execute-live`; provider budgets remain
  independent and reuse M2's append-only usage ledger.
- Unknown facts never become zero. Only high-confidence hard rules reject. Every failed
  acceptance gate yields `uncertain`.
- Keep the `1..20` evaluated cap and M2's `1..100` candidate cap.
- Add no production dependency, database, frontend, contacts, outreach, CRM, agent framework,
  or LLM scoring.
- Preserve the old narrow M2 entry point and old serialized M1/M2 company payloads.
- Every function has a useful docstring and all mutable values are defensively copied.
- Missing local Ruff, mypy, build, or package-install capability is not a reason to abandon a
  change: commit, push, open a draft PR, and let GitHub CI run unavailable gates.
- No role merges its own draft PR.

---

### Task 1: Add the M3 models and deterministic scoring policy

**Files:**
- Modify: `src/leads_discovery/models.py`
- Create: `src/leads_discovery/scoring/__init__.py`
- Create: `src/leads_discovery/scoring/policy.py`

**Interfaces:**
- Consumes: existing `CompanyRecord`, `EvidenceItem`, M2 `features`,
  `feature_confidence`, and `stage_status`.
- Produces: `DecisionReason`, `ScoringPolicy`, `DEFAULT_POLICY`, `FinalDecision`,
  `evaluate_company()`, and `evaluate_companies()` exactly as frozen in the spec.

- [ ] **Step 1: Extend the canonical model additively**

Add `DecisionReason` plus defaulted `decision_reasons` and
`evaluation_policy_version` fields. Update defensive construction and serialization so both
old payloads without these keys and new nested payloads load safely.

```python
DecisionKind = Literal["review", "rejection"]

@dataclass(slots=True)
class DecisionReason:
    """Explain one review or rejection decision with retained citations."""

    code: str
    kind: DecisionKind
    explanation: str
    confidence: float | None = None
    evidence_ids: list[str] = field(default_factory=list)
```

- [ ] **Step 2: Define and validate the immutable policy**

Implement the exact seven thresholds and version from the spec. Reject booleans,
non-finite numbers, out-of-range confidence/coverage thresholds, acceptance scores outside
`0..100`, or a blank version.

```python
@dataclass(frozen=True, slots=True)
class ScoringPolicy:
    """Configure the versioned M3 decision thresholds."""

    version: str = "m3-v1"
    minimum_fact_confidence: float = 0.60
    critical_relevance_confidence: float = 0.75
    hard_rejection_confidence: float = 0.85
    acceptance_score: float = 70.0
    minimum_overall_coverage: float = 0.70
    minimum_workload_coverage: float = 0.60
    minimum_economic_coverage: float = 0.50
```

- [ ] **Step 3: Implement fact validation and signal transforms**

Use a private immutable rule catalog containing the exact category/subfeature weights from
the spec. Resolve each fact only when confidence, citations, retained evidence identity,
type, and range are valid. Keep bool distinct from int. Implement the exact boolean, branch,
employee, revenue, and manufacturer-breadth boundaries.

```python
@dataclass(frozen=True, slots=True)
class _UsableFact:
    """Hold one validated fact and its evidence-linked confidence."""

    value: FactValue
    confidence: float
    evidence_ids: tuple[str, ...]
```

Return an invalid-fact review reason instead of assigning a score for malformed fact-level
data. Raise on run-level model corruption rather than swallowing it.

- [ ] **Step 4: Compute category score, category coverage, final score, and overall coverage**

Implement the formulas verbatim from the spec. Category scores normalize over usable
subfeature weights. Final category influence is `product_weight * category_coverage`.
Use unrounded values for decisions, then persist score to two decimals and coverage to four.
When every score category is unknown, persist `final_score=None` and
`score_unavailable`.

- [ ] **Step 5: Implement hard rules and the precision-first decision gate**

Apply hard rules first. Otherwise require confirmed PVF relevance, score `>=70`, overall
coverage `>=0.70`, workload coverage `>=0.60`, economic coverage `>=0.50`, and resolved
non-positive incumbent evidence. Any failed acceptance gate produces `uncertain`.
Historical competitor evaluation is context only.

- [ ] **Step 6: Make evaluation defensive and recomputable**

`evaluate_company()` must reject incomplete extraction, return a detached record, replace
all prior M3-derived values/reasons, and preserve every M2 fact/provenance field.
`evaluate_companies()` validates `limit in 1..20`, rejects duplicate IDs, filters to completed
extractions, sorts by company ID, and returns a tuple.

- [ ] **Step 7: Run the narrow production checks**

```bash
python -m compileall -q src/leads_discovery/models.py src/leads_discovery/scoring
ruff check src/leads_discovery/models.py src/leads_discovery/scoring
mypy src/leads_discovery/models.py src/leads_discovery/scoring
pytest tests/test_package.py tests/test_extraction.py -q
```

Run every available command. If a tool is unavailable, record that fact and continue.

- [ ] **Step 8: Commit the scoring slice**

```bash
git add src/leads_discovery/models.py src/leads_discovery/scoring
git commit -m "feat(scoring): evaluate company evidence"
```

### Task 2: Publish deterministic evaluation artifacts

**Files:**
- Create: `src/leads_discovery/pipeline/evaluation.py`
- Modify: `src/leads_discovery/pipeline/state.py`
- Modify: `src/leads_discovery/pipeline/__init__.py`

**Interfaces:**
- Consumes: Task 1 evaluation functions; M2 `companies_extracted.jsonl`, `usage.json`, and
  `checkpoint.json`; existing path-safe persistence helpers.
- Produces: `EvaluationConfig`, `EvaluationSummary`, `evaluate_run()` and the seven derived
  non-calibration artifacts in the spec.

- [ ] **Step 1: Add shared atomic derived-file writers**

Add same-directory temp-file writers for complete JSONL and text/CSV files. Reuse
`_ensure_write_target()` and directory fsync. Validate all target symlinks before creating or
replacing any artifact. Never use append semantics for M3 views.

```python
def write_jsonl_atomic(path: Path, payloads: Iterable[dict[str, Any]]) -> None:
    """Atomically replace one complete JSONL artifact without following symlinks."""


def write_text_atomic(path: Path, text: str) -> None:
    """Atomically replace one UTF-8 text artifact without following symlinks."""
```

- [ ] **Step 2: Validate the local evaluation boundary**

Implement `EvaluationConfig` and exact M2 run-ID/path/cap validation before writes. Reject a
symlinked run directory or any M3 artifact target. Load the durable checkpoint and latest
extracted snapshot per company without modifying either.

- [ ] **Step 3: Evaluate the selected completed records**

Select extraction-complete records in company-ID order, cap at `max_evaluated`, and invoke
Task 1. Permit zero results only while M2 is running or paused. If an individual fact is
malformed, keep that company uncertain; if the JSONL or canonical record is structurally
corrupt, fail without replacing prior outputs.

- [ ] **Step 4: Render canonical JSONL and safe CSV views**

Write one evaluated snapshot per company. Implement the exact decision/score/coverage/name/ID
sort and columns from the spec. Missing category score is an empty cell. Join reasons with
`;`. Prefix externally sourced spreadsheet cells whose first non-whitespace character is
`=`, `+`, `-`, or `@` with an apostrophe.

```python
_DECISION_ORDER = {"accepted": 0, "uncertain": 1, "rejected": 2}
_FORMULA_PREFIXES = frozenset("=+-@")
```

- [ ] **Step 5: Write the calibration template and run summary**

Generate the ranked context plus blank `manual_label` and `manual_notes`. Report policy,
checkpoint status, counts, relative artifact paths, and existing provider usage without
inventing missing costs.

- [ ] **Step 6: Return the frozen summary**

```python
@dataclass(frozen=True, slots=True)
class EvaluationSummary:
    """Summarize one complete local M3 evaluation publication."""

    run_id: str
    policy_version: str
    evaluated_count: int
    accepted_count: int
    rejected_count: int
    uncertain_count: int
    artifact_paths: tuple[Path, ...]
```

All returned paths are detached `Path` values. Persisted paths remain relative to the run
directory.

- [ ] **Step 7: Run the narrow production checks**

```bash
python -m compileall -q src/leads_discovery/pipeline
ruff check src/leads_discovery/pipeline src/leads_discovery/scoring src/leads_discovery/models.py
mypy src/leads_discovery/pipeline src/leads_discovery/scoring src/leads_discovery/models.py
pytest tests/test_state.py tests/test_m2_batch.py -q
```

- [ ] **Step 8: Commit the evaluation slice**

```bash
git add src/leads_discovery/pipeline
git commit -m "feat(pipeline): publish evaluated company views"
```

### Task 3: Add report-only manual calibration

**Files:**
- Create: `src/leads_discovery/calibration.py`

**Interfaces:**
- Consumes: `EvaluationConfig`, evaluated JSONL, ranked CSV data, and policy version.
- Produces: `ManualLabel`, `CalibrationSummary`, and `calibrate_run()` exactly as frozen.

- [ ] **Step 1: Validate label input completely before output mutation**

Reject a missing or symlinked label file, duplicate headers, duplicate IDs, unknown IDs,
blank supplied labels, formula-like company IDs, malformed CSV, or values outside A/B/C.
Trim values and uppercase labels. Permit read-only template context columns and partial
labeling.

- [ ] **Step 2: Compute the fixed matrix and disagreement classes**

Create every A/B/C by accepted/rejected/uncertain matrix cell, including zero cells. Compute
per-label score/coverage summaries only over non-null values. Critical disagreements are
`A+rejected` and `C+accepted`; review disagreements use the exact remaining combinations from
the spec.

- [ ] **Step 3: Atomically publish calibration outputs**

Write deterministic `calibration_report.json` and `companies_calibrated.csv`. Regenerate all
ranked context from evaluated records, carrying only validated label/notes from input. Do not
modify policy, evaluation files, checkpoint, usage, or M2 artifacts.

- [ ] **Step 4: Return the frozen summary**

```python
@dataclass(frozen=True, slots=True)
class CalibrationSummary:
    """Summarize one local manual-label comparison."""

    run_id: str
    policy_version: str
    evaluated_count: int
    labeled_count: int
    unlabeled_count: int
    critical_disagreement_count: int
    report_path: Path
    joined_csv_path: Path
```

- [ ] **Step 5: Run the narrow production checks**

```bash
python -m compileall -q src/leads_discovery/calibration.py
ruff check src/leads_discovery/calibration.py
mypy src/leads_discovery/calibration.py
```

- [ ] **Step 6: Commit the calibration slice**

```bash
git add src/leads_discovery/calibration.py
git commit -m "feat(calibration): compare manual lead labels"
```

### Task 4: Compose the full CLI, documentation, and production handoff

**Files:**
- Create: `src/leads_discovery/cli.py`
- Create: `src/leads_discovery/__main__.py`
- Create: `README.md`
- Modify: `.env.example` only if the documented existing variables are missing
- Modify: `PLANS.md` only after the combined candidate passes every gate

**Interfaces:**
- Consumes: existing `M2BatchConfig`/`run_m2_batch`, Task 2 `evaluate_run`, and Task 3
  `calibrate_run`.
- Produces: the exact `run`, `score`, and `calibrate` command surface and exit codes.

- [ ] **Step 1: Build the three-command parser**

Map `run --max-evaluated` to M2 `max_extracted`. Forward existing independent provider
budgets without adding an aggregate budget. Do not put provider/budget flags on `score` or
`calibrate`.

- [ ] **Step 2: Enforce the paid execution boundary**

Without `--execute-live`, `run` returns the authorized dry-run summary and never reads
credentials or creates clients. With it, read credentials only at CLI composition time,
disable missing optional Apify, and use existing M2 provider adapters unchanged.

- [ ] **Step 3: Evaluate useful partial work after M2**

After `completed`, `paused_budget`, or `paused_unknown`, run local evaluation over every
completed extraction. Preserve pause state and return code 2 for pauses. On full success,
persist `evaluation` and `m3_pipeline` stage completion. Do not evaluate after failed
configuration/authentication state.

- [ ] **Step 4: Keep local commands isolated**

`score` and `calibrate` must not import or instantiate provider clients on their execution
paths, inspect provider environment variables, append usage events, or touch M2 ledgers.
Print one sanitized JSON summary for every command.

- [ ] **Step 5: Document the operating workflow**

Document setup, explicit live authorization, separate Exa/Apify/DeepSeek ceilings, cap and
resume behavior, all artifacts, score versus coverage, decision rules, A/B/C calibration,
zero-network local commands, and exclusions such as contacts/outreach.

- [ ] **Step 6: Run all production-branch gates available locally**

```bash
python -m compileall -q src
ruff check .
mypy src tests
pytest
python -m build
```

Do not mark M3 complete in `PLANS.md` yet. Completion belongs to the combined integration
candidate after independent tests and red-team review.

- [ ] **Step 7: Commit and publish the production branch**

```bash
git add src README.md .env.example
git commit -m "feat(m3): finish lead evaluation pipeline"
git push -u origin codex/m3-production
```

Open a draft PR from `codex/m3-production` to `codex/m3-spec`. Include the required
high-level DAG, actual validation results, deferred CI gates, and no unverified claims. Do not
merge it.

### Task 5: Build the independent M3 contract suite in parallel

**Files:**
- Create: `tests/test_scoring.py`
- Create: `tests/test_decisions.py`
- Create: `tests/test_evaluation.py`
- Create: `tests/test_outputs.py`
- Create: `tests/test_calibration.py`
- Create: `tests/test_cli.py`
- Create: `tests/m3_factories.py`
- Create or modify: `tests/conftest.py` only for shared M3 factories and a zero-network guard

**Interfaces:**
- Consumes: only the frozen API and behavior in the approved spec; it does not inspect the
  production branch.
- Produces: independent tests that may initially fail import/collection on the spec-only base
  but must be syntactically valid and become authoritative after integration.

- [ ] **Step 1: Create strict evidence-linked company factories**

Build `CompanyRecord` fixtures with real registrable domains, extraction status completed,
unique evidence IDs, and coherent confidence objects. Use a factory name other than pytest's
reserved `request`. Construct NaN/infinity corruption from raw bytes or `float()` at runtime,
not non-standard JSON literals accidentally normalized by a serializer. Put the typed builder
in `tests/m3_factories.py`:

```python
FactInput = tuple[FactValue, float]


def build_company(
    *,
    facts: Mapping[str, FactInput],
    company_id: str = "cmp_contract",
    name: str = "Contract Valve",
) -> CompanyRecord:
    """Build one extracted company whose non-null facts cite unique retained evidence."""
    evidence: list[EvidenceItem] = []
    features: dict[str, FactValue] = {}
    confidence: dict[str, object] = {}
    for index, (key, (value, score)) in enumerate(sorted(facts.items()), start=1):
        evidence_id = f"ev_{index:024d}"
        ids = [] if value is None else [evidence_id]
        if value is not None:
            evidence.append(
                EvidenceItem(
                    evidence_id=evidence_id,
                    url=f"https://contractvalve.com/evidence/{index}",
                )
            )
        features[key] = value
        confidence[key] = {"confidence": score, "evidence_ids": ids}
    return CompanyRecord(
        company_id=company_id,
        name=name,
        domain="contractvalve.com",
        normalized_domain="contractvalve.com",
        country="US",
        evidence=evidence,
        features=features,
        feature_confidence=confidence,
        stage_status={"research": "completed", "extraction": "completed"},
    )
```

- [ ] **Step 2: Test exact formulas and transform boundaries**

Cover every threshold edge, category weight sum, effective coverage weight, final rounding,
unknown/malformed/uncited/low-confidence behavior, bool-versus-int rejection, list breadth
deduplication, duplicate companies, cap validation, and defensive copies.

```python
def test_high_score_with_low_coverage_is_uncertain() -> None:
    company = build_company(
        facts={
            "pvf_relevant": (True, 0.90),
            "rfq_or_quote_workflow_evidence": (True, 0.90),
            "inside_sales_or_estimating_presence": (True, 0.90),
            "project_or_tender_business": (True, 0.90),
            "employee_count": (50, 0.90),
            "branch_count": (3, 0.90),
            "known_current_direct_competitor_customer": (False, 0.90),
            "direct_quotation_pain_evidence": (True, 0.90),
        }
    )

    evaluated = evaluate_company(company)

    assert evaluated.final_score is not None
    assert evaluated.final_score >= 70
    assert evaluated.coverage["overall"] < 0.70
    assert evaluated.final_decision == "uncertain"
    assert "low_overall_coverage" in evaluated.review_reasons
```

- [ ] **Step 3: Test every decision gate and hard rule independently**

Prove one failed acceptance gate at a time yields uncertain. Prove every hard rule needs the
exact full trigger and confidence boundary, retains citations, and outranks acceptance. Prove
historical competitor evaluation is review-only and a low score never rejects.

- [ ] **Step 4: Test derived persistence and exports adversarially**

Cover stable tie ordering, exact headers, blank missing scores, Unicode/quotes/newlines,
formula prefixes after whitespace, JSON text preservation, relative report paths, atomic
replacement, rerun idempotency, M2 input immutability, duplicate JSONL IDs, torn/corrupt
input, and run/artifact symlinks.

- [ ] **Step 5: Test calibration as a report-only operation**

Cover the complete matrix, critical/review disagreements, partial labels, score/coverage
summaries, context-column import, case normalization, and rejection of duplicate headers/IDs,
unknown IDs, blank labels, formula IDs, malformed CSV, and label symlinks. Assert no evaluated,
checkpoint, usage, or policy file changes.

- [ ] **Step 6: Test the CLI and budget boundary without providers**

Use fakes or monkeypatching at the composition boundary. Prove dry run reads no credentials,
score/calibrate load no clients or environment secrets, provider flags do not exist on local
commands, `max-evaluated` maps to M2 exactly, completed/paused/failed statuses produce exact
evaluation and exit behavior, and partial extracted companies are exported after pause.

- [ ] **Step 7: Run test-branch checks that do not require production**

```bash
python -m compileall -q tests
ruff check tests
```

Collection failure caused only by absent M3 imports is expected on the independent base and
must be reported, not treated as a reason to guess another API or abandon the branch.

- [ ] **Step 8: Commit and publish the contract-test branch**

```bash
git add tests
git commit -m "test(m3): enforce evaluation contracts"
git push -u origin codex/m3-contract-tests
```

Open a draft PR from `codex/m3-contract-tests` to `codex/m3-spec`. Report the behavior matrix,
syntax/lint results, expected isolated collection state, deferred integration gates, and zero
provider spend. Do not merge it.

### Task 6: Integrate production and tests, then prove the combined candidate

**Files:**
- Combine: current tips of `codex/m3-production` and `codex/m3-contract-tests`
- Modify: production or tests only to resolve confirmed integration defects
- Modify: `PLANS.md` only after all required checks and red-team remediation pass

**Interfaces:**
- Consumes: both completed draft PR branches based on `codex/m3-spec`.
- Produces: one reviewable combined M3 candidate; branch naming may follow the coordinator's
  current workflow and is not an immutable precondition.

- [ ] **Step 1: Combine both current branches without rewriting their independent history**

Use a merge-based integration branch or equivalent GitHub integration PR. Do not ask the user
for an exact commit hash when the current remote branches/PRs identify the work.

- [ ] **Step 2: Run narrow failures first and fix root causes in the owning branch**

Test assertion defects belong to the contract-test branch. Production behavior defects belong
to the production branch. Recombine after each remediation; do not weaken a valid invariant to
make CI green.

- [ ] **Step 3: Run the full gate**

```bash
ruff check .
mypy src tests
pytest
python -m build
```

All must pass on the combined candidate. GitHub CI is authoritative when local tools are
unavailable.

- [ ] **Step 4: Hand the actual combined candidate to red team**

Tell red team to locate and review the current combined M3 branch/PR. Do not pass a shell-local
path, immutable-base demand, placeholder hash, or unverified green claim.

### Task 7: Run a post-integration adversarial review

**Files:**
- Review: all combined M3 production and test changes against the approved spec
- Add or correct: `tests/**` only
- Never modify: `src/**`, docs, config, or plans

**Interfaces:**
- Consumes: the current combined production-plus-contract-test candidate.
- Produces: confirmed/candidate/dismissed findings, adversarial tests, full validation results,
  and a draft red-team PR or review branch; never a merge.

- [ ] **Step 1: Resolve the combined candidate from current repository state**

Prefer the coordinator's current combined branch/PR. If naming differs, inspect the current M3
PRs and use the candidate containing both completed workstreams. Never block on an absent exact
SHA. If they are not combined, make a temporary red-team branch that merges the current two
branches for review.

- [ ] **Step 2: Attack scoring and decisions**

Probe confidence values `0.5999/0.60/0.7499/0.75/0.8499/0.85`, score and coverage boundaries,
bool-as-int, non-finite data, duplicate/missing citations, unsupported types, category weight
renormalization, all hard-rule near misses, incumbent ambiguity, and stale recomputation.

- [ ] **Step 3: Attack state, files, CSV, calibration, CLI, and spend safety**

Probe traversal/symlinks, torn JSONL, duplicate IDs, atomic-failure preservation, spreadsheet
formulas with whitespace/Unicode, CSV header tricks, calibration joins, environment leakage,
local-command network isolation, pause/resume status, cap bypass, aggregate-budget invention,
and ambiguous paid-call repetition.

- [ ] **Step 4: Use mutation probes to test the tests**

Temporarily invert important production predicates or boundary comparisons in an isolated
review checkout, run the relevant tests, and restore the file without committing production
mutations. Record which mutations survived. Add focused tests for valid uncovered behavior;
never alter production code.

- [ ] **Step 5: Run all available gates and inspect CI**

```bash
ruff check .
mypy src tests
pytest
python -m build
```

Missing local tools defer only that command to CI; they do not terminate review. No live
provider smoke test is authorized.

- [ ] **Step 6: Publish the adversarial result without merging**

For each confirmed finding report severity, exact invariant, minimal reproduction, affected
file/line, and whether a committed test proves it. Separate candidate hypotheses and dismissed
hypotheses. Report remaining untested risks, committed test-only changes, validation, branch/PR,
and `$0` provider spend. A green pass requires no confirmed open defect and a green combined
gate.

### Task 8: Finalize M3 after red-team green

**Files:**
- Modify: `PLANS.md`
- Review: `README.md` and the final combined diff

**Interfaces:**
- Consumes: green combined CI and green red-team report.
- Produces: M3 completion metadata and a merge-ready final PR with the required DAG.

- [ ] **Step 1: Resolve every confirmed red-team finding**

Production remediates behavior defects; tests remediates invalid expectations. Recombine and
rerun the full gate after each batch.

- [ ] **Step 2: Mark M3 complete only now**

Change the M3 milestone checkbox in `PLANS.md` from `[ ]` to `[x]`. Keep all scope bullets and
contract links intact.

- [ ] **Step 3: Run the final gate on the exact merge candidate**

```bash
ruff check .
mypy src tests
pytest
python -m build
```

- [ ] **Step 4: Prepare the final review metadata**

Include factual validation results, risks, no breaking changes unless proven otherwise, and a
small DAG showing M2 facts flowing through M3 scoring, decisions, outputs, and calibration.
No frontend screenshot is required because this project has no frontend.

- [ ] **Step 5: Merge only after review approval and green post-merge CI**

Do not delete or overwrite live artifacts. After merge, verify the remote default branch and
its CI rather than relying only on the pre-merge branch.
