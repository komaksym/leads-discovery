# M4 Contact Discovery & Enrichment Design

**Status:** production implementation contract; the credentialed one-company canary remains the external acceptance gate.

## Purpose

M4 consumes only M3 `accepted` companies and produces reviewable contact artifacts. It does
not change M1-M3 discovery, extraction, scoring, decisions, usage ledgers, or commands, and it
does not perform outreach.

```text
M3 companies_evaluated.jsonl
        |
        | accepted only
        v
Exa People Search (<=1 request/company, <=10 results)
        |
        v
confirm current company + rank buying proximity
        |
        v
retain <=3 distinct contacts/company
        |
        | top 2 only, rank 1/2 only
        v
Clay Routine -> Apollo fallback -> Instantly verification
        |
        v
contacts.jsonl + leads.csv
```

`uncertain` and `rejected` companies never enter an M4 provider loop.

## Product invariants

1. M4 state and usage are separate from M2/M3 state and ledgers.
2. M4 reads `companies_evaluated.jsonl` and accepts only records whose decision stage is
   complete and whose `final_decision` is exactly `accepted`.
3. M4 never evaluates more than the M3 maximum universe of 20 companies.
4. Exa receives at most one People Search request per accepted company and returns at most 10
   candidates.
5. Current employment is required before a person becomes a contact. Exa structured person
   metadata is authoritative for this check: a `workHistory` item must name the accepted
   company and have `dates.to == null`. Past-only employment does not qualify.
6. Ranking is deterministic local code. Functional proximity to the software-buying decision
   beats unrelated title prestige.
7. At most three distinct contacts are retained per company. Deduplication uses normalized
   profile URL first, otherwise normalized full name plus normalized company domain. No fuzzy
   name merge is allowed.
8. Only the first two retained contacts can be paid candidates, and only when their decision
   rank is 1 or 2. Rank 3 never triggers Clay, Apollo, or Instantly.
9. Missing email never removes a retained contact.
10. Clay is work-email-only. Apollo personal email, phone, and waterfall flags are always
    explicitly false. Instantly is verification-only.
11. Every paid dispatch is durably marked `in_flight` first. Known result and usage are
    persisted before the operation becomes complete.
12. An unknown synchronous paid-call outcome is never replayed automatically. Resumable
    asynchronous state uses only its persisted provider identifier: Clay polls the same
    `routine_run_id`; Instantly `pending` resumes with GET for the same email and never repeats
    POST.
13. Known budget exhaustion publishes the best partial contact artifacts and pauses cleanly.
14. Dry execution reads no provider credentials, constructs no live provider clients, and
    accesses no network.
15. Existing `run`, `score`, and `calibrate` behavior remains unchanged and never invokes M4.
16. All M4 output writes reuse the repository's atomic, path-contained, no-symlink primitives.
17. Automated development and tests spend $0 on providers.

## Contact model

M4 adds a separate `ContactRecord`; people fields do not belong on `CompanyRecord`.

```python
@dataclass(slots=True)
class ContactRecord:
    contact_id: str
    company_id: str
    company_name: str
    company_domain: str
    company_final_score: float | None
    full_name: str
    title: str
    decision_rank: int
    decision_reason: str
    linkedin_url: str | None = None
    profile_url: str | None = None
    current_employment_confirmed: bool = True
    work_email: str | None = None
    email_source: str | None = None
    email_verification_status: str | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    provider_attempts: list[dict[str, Any]] = field(default_factory=list)
```

Mutable fields are defensively copied. `contact_id` is a SHA-256-derived stable ID from
`company_id` plus the contact's exact dedupe key. Timestamps are intentionally omitted from
identity and sorting.

## Exa People Search

Current Exa documentation (checked 2026-08-24) specifies:

- `POST https://api.exa.ai/search`;
- header `x-api-key`;
- `category: "people"`, `type: "auto"`, `numResults: 10`;
- `contents: {"highlights": true}`;
- structured person entities under `results[].entities[].properties`;
- current employment can be inspected through `workHistory[].company.name` and
  `workHistory[].dates.to`; `to: null` usually represents the active role.

Reference: https://exa.ai/docs/reference/verticals/people-for-coding-agents

The query names the accepted company and asks for people close to purchasing operational
software: owners/executives, senior Sales/Operations/Commercial/Estimating/Inside Sales
leaders, and credible branch/regional/function managers.

A candidate qualifies only when a structured `person` entity has a nonblank name and a
current work-history row whose normalized company name exactly matches the accepted company's
normalized name (or its canonical name after the same normalization). The title comes from
that current row. A result URL is retained as provenance/profile URL.

Exa response `costDollars.total` is retained as provider-reported estimated cost when present.
No static pricing assumption is used for accounting. The independent Exa People USD ceiling
is checked against replayed known provider spend before each request; unknown prior spend
fails closed.

## Decision ranking

Normalize titles by Unicode-preserving whitespace collapse and case-folding.

Rank 1, direct decision-maker:

- owner/founder-owner;
- president;
- CEO / chief executive officer;
- COO / chief operating officer;
- managing partner;
- general manager / GM.

Rank 2, functional decision-maker:

- a relevant function (`sales`, `operations`, `commercial`, `estimating`, `inside sales`)
  combined with senior leadership (`chief`, `executive`, `VP`/`vice president`, `head`,
  `director`).

Rank 3, operational deputy:

- relevant branch/regional/sales/operations/estimating/inside-sales management, such as a
  manager or branch/regional director that did not already qualify for rank 2.

Unrelated prestige (for example, an unrelated C-level title) does not qualify merely because
it is senior. Within a rank, deterministic function relevance and normalized name break ties.

## Deduplication and paid boundary

The exact dedupe key is:

1. normalized LinkedIn/profile URL when available;
2. otherwise `normalized full name + normalized company domain`.

The first three ranked distinct contacts are retained. Paid candidates are the first two
retained contacts, filtered again to decision rank 1 or 2.

## Clay

Current Clay Public API documentation (checked 2026-08-24) uses `clay-api-key` authentication
and asynchronous routines. M4 requires:

- `CLAY_PUBLIC_API_KEY`;
- `CLAY_CONTACT_ROUTINE_ID`;
- `POST https://api.clay.com/public/v0/routines/{routine_id}/run` with 1-100 `items`;
- `GET https://api.clay.com/public/v0/routines/run/{routine_run_id}/results`.

Reference: https://developers.clay.com/ and its routines API index.

M4 batches all currently eligible paid contacts into one bounded run when possible, limited by
`clay_max_contacts` (default 10). Each item uses `contact_id` as its stable item ID and inputs:
`full_name`, `company_name`, `company_domain`, `linkedin_url`, and `profile_url`.

The configured routine contract must return a work email under `work_email`. M4 accepts only a
syntactically valid non-personal email. It never requests or persists phones or personal
emails. The returned `routine_run_id` is checkpointed immediately before any result polling.
A restart with that ID polls the same run; a start whose response was lost is `paused_unknown`
and is not replaced.

## Apollo fallback

Current Apollo People Enrichment documentation (checked 2026-08-24) uses:

- `POST https://api.apollo.io/api/v1/people/match`;
- header `x-api-key`;
- identifiers such as `name`, `domain`, `organization_name`, and `linkedin_url`.

Reference: https://docs.apollo.io/reference/people-enrichment

M4 calls Apollo only after Clay is known complete for that contact with no usable work email.
Every request explicitly sends:

```text
reveal_personal_emails=false
reveal_phone_number=false
run_waterfall_email=false
run_waterfall_phone=false
```

No webhook is sent. One credit is reserved before each attempt. If Apollo reports a usage
field such as `credits_consumed`/`credits_used`, it must be finite and nonnegative and becomes
the accounted value. When no usage field is reported, the reserved one credit remains the
conservative budget charge even when no email is returned. An email miss never implies zero
spend.

## Instantly verification

Current Instantly V2 documentation (checked 2026-08-24) uses Bearer authentication and only
these M4 endpoints:

- `POST https://api.instantly.ai/api/v2/email-verification`;
- `GET https://api.instantly.ai/api/v2/email-verification/{email}`.

References:
https://developer.instantly.ai/api-reference/schemas/email-verification and
https://developer.instantly.ai/.

M4 reads `verification_status`, never the top-level request `status`, and preserves exactly
`verified`, `invalid`, or `pending`. Provider-reported `credits_used` must be null or a finite
nonnegative number and is persisted in usage metadata. A `pending` POST is persisted before
returning; later invocations use GET only. M4 contains no client methods for Instantly lead,
list, campaign, sequence, SuperSearch, or email endpoints.

## State and artifacts

Under `data/<run_id>/` M4 owns only:

```text
contacts.jsonl
leads.csv
contact_usage_events.jsonl
contact_usage.json
contact_checkpoint.json
```

`contact_checkpoint.json` reuses `RunCheckpoint` with a separate file and operation map. M2's
`checkpoint.json`, `usage_events.jsonl`, and `usage.json` remain read-only to M4.

`contact_usage_events.jsonl` stores `UsageEvent` records for Exa People, Clay, Apollo, and
Instantly calls. `contact_usage.json` contains replayed `CostTracker` totals plus explicit M4
quota counters for Clay submitted contacts, Apollo credits (reported or conservatively
reserved), Instantly API calls and reported credits.

Provider attempt dictionaries on a contact contain only safe state/provenance such as provider,
operation, state, request/run identifier, and result classification; raw provider bodies and
credentials are never persisted there.

## Artifact publication

`contacts.jsonl` is a complete atomic snapshot, not an append-only paid ledger. Every retained
contact appears once. `leads.csv` is regenerated from the same snapshot and is the primary
human-review artifact.

CSV order is:

1. company final score descending, missing score last;
2. decision rank ascending;
3. normalized contact name;
4. contact ID.

Columns are:

```text
company_id,company_name,company_domain,company_final_score,
contact_id,full_name,title,decision_rank,decision_reason,
work_email,email_verification_status,linkedin_url,profile_url,email_source
```

Externally sourced CSV cells use the existing M3 formula-injection rule: after leading
whitespace, values beginning with `=`, `+`, `-`, or `@` receive a leading apostrophe. JSON
retains the original value.

## Configuration and budgets

`ContactEnrichmentConfig` has independent controls only:

```text
max_contacts_per_company = 3          # validated 1..3
max_paid_contacts_per_company = 2     # validated 0..2 and <= max contacts
exa_people_budget_usd                 # explicit nonnegative finite ceiling for live execution
clay_max_contacts = 10                # nonnegative integer
apollo_credit_cap = 5                 # nonnegative finite number
instantly_verification_call_cap = 5   # nonnegative integer
```

There is no aggregate budget. Budget exhaustion is a durable `paused_budget` partial result.
An unknown `in_flight` outcome is `paused_unknown` except for Clay with a persisted run ID and
Instantly with a persisted `pending` email, whose documented GET status calls are resumable.

## CLI and GitHub Actions

Add:

```text
python -m leads_discovery enrich --run-id RUN [controls] [--execute-live]
```

Without `--execute-live`, the command validates local scalar arguments and prints a dry-run
summary without reading credentials, importing live provider composition, touching run files,
or accessing the network. Live execution requires Exa, Clay, Apollo, and Instantly credentials
because the configured waterfall is explicit; a missing credential fails before dispatch.

A separate `workflow_dispatch` GitHub Actions workflow performs manual live enrichment using
repository secrets and publishes only `leads.csv` and `contacts.jsonl` to the dedicated
`generated-leads` branch. The other M4 run artifacts remain in the ephemeral runner workspace.
It has no schedule, no outreach step, and no write-back to a CRM.

## Completion gate

The integrated tree contains the production implementation and permanent independent contract
and red-team coverage. Lint, typing, full tests, build, offline, and workflow checks are the
local completion gate; the remaining external acceptance is one credentialed one-company
canary with the fixed workflow ceilings. `PLANS.md` records the integrated milestone as complete.
