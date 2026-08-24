# M4 Contact Discovery & Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit resumable M4 command that turns M3 accepted companies into bounded,
reviewable contact artifacts using Exa People Search, Clay, Apollo fallback, and Instantly
verification without any outreach.

**Architecture:** M4 is a separate file-backed pipeline. Pure contact selection parses Exa's
structured person entities and ranks current employees; thin injected-`httpx` adapters own the
four provider contracts; a resumable orchestrator owns the independent contact checkpoint and
usage ledger; the top-level CLI constructs live clients only after explicit authorization.

**Tech Stack:** Python 3.12+, existing stdlib/dataclasses/JSON/CSV/pathlib, existing `httpx`,
pytest/Ruff/mypy/setuptools build. No new production dependency.

**Spec:** `docs/superpowers/specs/2026-08-24-m4-contact-discovery-enrichment-design.md`

## Global Constraints

- Base production work only on current `main` and branch `codex/m4-production`.
- Do not inspect or modify independent M4 contract-test or red-team branches/PRs.
- Production/config/docs only; never modify `tests/**` in this workstream.
- Only M3 `accepted` companies may cause M4 provider calls; uncertain/rejected call count is zero.
- Max 20 accepted-company inputs, max one Exa People request/company, max 10 Exa candidates.
- Keep <=3 contacts/company and <=2 paid candidates/company; rank 3 never causes paid enrichment.
- No phones, personal emails, outreach, CRM, database, frontend, or autonomous SDR behavior.
- M4 owns separate checkpoint and usage artifacts and never changes M2 ledgers.
- Persist `in_flight` before paid dispatch and known result/usage before completion.
- Unknown paid outcomes fail closed; only persisted Clay run IDs and Instantly pending emails use
  documented GET resume paths.
- Dry `enrich` must not read credentials, construct live clients, access run files, or network.
- Every function has a useful docstring and mutable values are defensively copied.
- Preserve path containment, symlink rejection, atomic writes, and CSV formula neutralization.
- Development and automated validation use $0 provider spend.
- Do not mark M4 complete until combined validation and red-team review are green.

---

### Task 1: Add the contact model and deterministic selection

**Files:**
- Modify: `src/leads_discovery/models.py`
- Create: `src/leads_discovery/contacts/__init__.py`
- Create: `src/leads_discovery/contacts/selection.py`

**Interfaces:**
- Consumes: M3 `CompanyRecord`; one Exa People Search `results` list.
- Produces: `ContactRecord`, `select_contacts(company, results, limit=3)` and normalization helpers.

- [ ] **Step 1: Add `ContactRecord` as a separate persisted model**

Implement the exact fields from the M4 design, defensive copying, `to_dict()`, and `from_dict()`.
Do not add people fields to `CompanyRecord`.

- [ ] **Step 2: Parse structured Exa person entities conservatively**

For each result, locate an entity whose type is exactly `person`. Require a nonblank
`properties.name` and a `workHistory` row whose `company.name` exactly matches the accepted
company after normalization and whose `dates.to` is null. Use that row's nonblank title.
Past-only work history does not qualify.

- [ ] **Step 3: Rank by decision proximity**

Implement rank 1 direct owners/executives, rank 2 senior relevant functional leaders, and rank
3 credible relevant managers. Exclude titles outside these groups. Return a stable
`decision_reason` code/string suitable for review.

- [ ] **Step 4: Deduplicate exactly and cap the retained contacts**

Normalize profile URLs without tracking query/fragment noise. Prefer profile URL as identity;
otherwise use exact normalized name plus company domain. Never fuzzy-match names. Generate the
stable contact ID from company ID plus the dedupe key, sort deterministically, and retain at
most three.

- [ ] **Step 5: Run narrow production checks**

Run every available command without touching tests:

```text
python -m compileall -q src/leads_discovery/models.py src/leads_discovery/contacts
ruff check src/leads_discovery/models.py src/leads_discovery/contacts
mypy src/leads_discovery/models.py src/leads_discovery/contacts
```

---

### Task 2: Implement the four bounded HTTP adapters

**Files:**
- Create: `src/leads_discovery/contacts/providers.py`

**Interfaces:**
- Produces injected-client adapters and immutable result objects:
  `ExaPeopleProvider.search()`, `ClayContactProvider.start()/results()`,
  `ApolloContactProvider.enrich()`, `InstantlyVerificationProvider.create()/get()`.
- Every successful/failed attempted call carries safe `UsageEvent` accounting.

- [ ] **Step 1: Add a sanitized contact-provider error/result contract**

Use the existing M2 HTTP status taxonomy and `UsageEvent` shape, but keep M4 provider interfaces
separate from M2 discovery interfaces. Never retain request headers, credentials, or raw error
bodies in exceptions.

- [ ] **Step 2: Implement Exa People Search**

POST `https://api.exa.ai/search` with `x-api-key`, `category=people`, `type=auto`,
`numResults=10`, and highlights. Validate `results` as a list, retain at most 10 rows, and parse
finite nonnegative `costDollars.total` when present.

- [ ] **Step 3: Implement Clay routine start and status read**

POST `/public/v0/routines/{routine_id}/run` with 1-100 stable `{id, inputs}` items and
`clay-api-key`. Require a nonblank routine run ID from the response. GET only the exact persisted
run at `/public/v0/routines/run/{routine_run_id}/results`. Treat `202` or nonterminal status as
pending; terminal `200` data must be a list of item results. Do not implement Clay webhooks.

- [ ] **Step 4: Implement Apollo synchronous fallback**

POST `/api/v1/people/match` with strongest available person identifiers and explicitly false
personal-email, phone, and both waterfall flags. Never send a webhook. Validate reported credit
usage when present; malformed, NaN, infinity, or negative usage is an invalid response. Return
only a syntactically valid non-personal work email when present.

- [ ] **Step 5: Implement Instantly verification-only methods**

Use Bearer authentication. POST only `/api/v2/email-verification` with the email and GET only
`/api/v2/email-verification/{quoted_email}`. Accept exactly `verified`, `invalid`, or `pending`.
Validate `credits_used` when present. The adapter exposes no lead/list/campaign/email methods.

- [ ] **Step 6: Run narrow production checks**

```text
python -m compileall -q src/leads_discovery/contacts
ruff check src/leads_discovery/contacts
mypy src/leads_discovery/contacts
```

---

### Task 3: Build separate resumable M4 state and artifacts

**Files:**
- Create: `src/leads_discovery/pipeline/contact_enrichment.py`
- Modify: `src/leads_discovery/pipeline/__init__.py` only if export wiring is useful

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class ContactEnrichmentConfig:
    run_id: str
    data_root: Path
    max_contacts_per_company: int = 3
    max_paid_contacts_per_company: int = 2
    exa_people_budget_usd: float | None = None
    clay_max_contacts: int = 10
    apollo_credit_cap: float = 5.0
    instantly_verification_call_cap: int = 5
    execute_live: bool = False

@dataclass(frozen=True, slots=True)
class ContactEnrichmentSummary:
    run_id: str
    status: str
    accepted_company_count: int
    contact_count: int
    paid_candidate_count: int
    verified_email_count: int
    artifact_paths: tuple[Path, ...]
```

`run_contact_enrichment(config, *, exa, clay, apollo, instantly)` executes only with injected
providers; CLI owns credential/client composition.

- [ ] **Step 1: Validate M4 paths and load accepted M3 companies**

Require the same run-ID grammar and direct-child data-root containment as M3. Reject symlinked
M4 outputs and `companies_evaluated.jsonl`. Strictly load unique completed M3 records, reject
more than 20, and select only exact `accepted` decisions before any provider work.

- [ ] **Step 2: Add a dedicated contact checkpoint and usage replay**

Reuse `RunCheckpoint`, `UsageEvent`, `load_usage_events`, `append_usage_event`, `CostTracker`,
and atomic JSON writers against M4 paths only. Validate operation maps, quota metadata, and
provider-reported numeric usage before making a budget decision.

- [ ] **Step 3: Discover and publish contacts company-by-company**

For each accepted company without completed Exa People state, check only the Exa People budget,
mark `in_flight`, perform one request, record usage, select contacts, atomically republish
`contacts.jsonl`/`leads.csv`, then mark the company operation complete. A previously unknown Exa
in-flight request becomes `paused_unknown` and is not replayed.

- [ ] **Step 4: Run/resume one bounded Clay batch**

Take only the first two retained contacts/company whose rank is 1 or 2 and have no completed
Clay attempt. Respect `clay_max_contacts`. Before POST persist `in_flight`; after POST persist
`routine_run_id` immediately and change the operation to resumable pending. On later invocation
GET the same run. When complete, apply each item's `work_email` by stable contact ID, persist
contacts and usage, then complete the operation. A start without a persisted run ID pauses
unknown.

- [ ] **Step 5: Apply Apollo fallback one contact at a time**

Only Clay-complete paid candidates with no usable email qualify. Reserve one credit before each
attempt and enforce the independent replayed Apollo cap. Persist `in_flight`, call once, apply
email/miss plus reported-or-reserved credit accounting, persist artifacts/usage, then complete.
Any unknown synchronous Apollo result pauses unknown and is never replayed.

- [ ] **Step 6: Create or resume Instantly verification**

For paid candidates with work email, enforce the independent API-call cap. New emails get one
POST after `in_flight`; `pending` is persisted as resumable state. Later invocations issue GET
for the same email only. Persist returned verification status and credits before completing or
leaving pending. Never repeat POST for a persisted pending verification.

- [ ] **Step 7: Publish deterministic artifacts after every known progress point**

Atomically replace `contacts.jsonl` and `leads.csv` from the current complete contact snapshot.
Sort by company score descending, rank ascending, normalized name, contact ID. Reuse the M3 CSV
formula-neutralization rule. Rebuild `contact_usage.json` from the append-only contact ledger.

- [ ] **Step 8: Return useful partial summaries**

Known budget exhaustion returns `paused_budget` with current artifacts. Unknown in-flight work
returns `paused_unknown`. Pending Clay or Instantly asynchronous work returns `paused_pending`
with its resumable identifier persisted. Full completion clears pending state.

- [ ] **Step 9: Run narrow production checks**

```text
python -m compileall -q src/leads_discovery/pipeline/contact_enrichment.py
ruff check src/leads_discovery/pipeline/contact_enrichment.py src/leads_discovery/contacts
mypy src/leads_discovery/pipeline/contact_enrichment.py src/leads_discovery/contacts
```

---

### Task 4: Add the explicit CLI, manual workflow, and operator docs

**Files:**
- Modify: `src/leads_discovery/cli.py`
- Modify: `.env.example`
- Modify: `README.md`
- Create: `.github/workflows/generate-leads.yml`
- Modify: `PLANS.md` to add an unchecked M4 milestone only

**Interfaces:**
- Adds only `python -m leads_discovery enrich --run-id RUN ... [--execute-live]`.
- Existing `run`, `score`, and `calibrate` dispatch paths remain unchanged.

- [ ] **Step 1: Add the `enrich` parser and dry path**

Expose the independent M4 controls. Dry mode validates only arguments and returns a sanitized
JSON summary. It must not read environment variables, import provider composition, touch run
artifacts, instantiate `httpx.Client`, or access network.

- [ ] **Step 2: Compose live enrichment only behind `--execute-live`**

Inside the live function import `os`, `httpx`, provider adapters, and orchestrator. Read
`EXA_API_KEY`, `CLAY_PUBLIC_API_KEY`, `CLAY_CONTACT_ROUTINE_ID`, `APOLLO_API_KEY`, and
`INSTANTLY_API_KEY`; fail before dispatch when required credentials are missing. Construct one
caller-owned `httpx.Client` and inject all providers.

- [ ] **Step 3: Add M4 setup and artifact documentation**

Document current-provider environment variables, dry/live behavior, independent budgets,
selection/ranking, current-employment requirement, resume semantics, all five artifacts, and the
hard no-outreach/no-phone/no-personal-email boundary.

- [ ] **Step 4: Add a manual-only lead-generation workflow**

Use only `workflow_dispatch`; no schedule. Install the package, invoke `enrich --execute-live`
with secrets, and upload the five M4 files with short retention (3 days). Do not create outreach
or CRM steps.

- [ ] **Step 5: Add an unchecked M4 milestone**

Append M4 scope and contract links to `PLANS.md` but keep its checkbox unchecked. State that
completion belongs to the combined production-plus-independent-test candidate after red-team
validation.

- [ ] **Step 6: Run full production-branch gates**

```text
ruff check .
mypy src tests
pytest
python -m build
```

No live provider smoke test is authorized. If browser execution cannot run a local command,
use the draft PR's GitHub Actions result as the available remote validation signal and report
that limitation precisely.

- [ ] **Step 7: Publish the draft PR**

Push `codex/m4-production` and open a draft PR to `main`. Include actual validation results,
`$0` development provider spend, changed files, and this DAG:

```text
M3 accepted companies
        |
        v
Exa People -> current-employment check -> decision ranking/dedupe
        |
        +--> rank 3: retain only
        |
        v
rank 1/2 top two -> Clay -> Apollo fallback -> Instantly verify
        |
        v
contacts.jsonl + leads.csv
        |
        +--> contact checkpoint + separate usage ledger
```

Do not merge and do not mark M4 complete.
