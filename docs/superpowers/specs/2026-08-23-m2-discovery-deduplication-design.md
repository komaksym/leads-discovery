# M2 Discovery and Deduplication Design Contract

Status: awaiting product-owner review | Revision: 3 | Baseline: `616003f` (merged M1)

M2 is one atomic implementation, one pull request, and one validation/review gate. The
headings below make this single contract readable; they are not sub-milestones, stack
layers, partial deliveries, or separate approval points. A file-by-file implementation
plan will be written only after this contract is approved.

## Outcome and scope

M2 adds bounded company discovery and conservative identity resolution to the M1 state
and cost foundation:

```text
deterministic U.S./Canada request plan
        ├── Exa company discovery
        └── optional Apify Google Maps discovery (hard-capped)
                         │
                         ▼
          typed raw records + provider usage
                         │
                         ▼
              pure local deduplication
                 ├── CompanyRecord[]
                 └── unresolved raw records
```

The discovery catalog is designed to find independent and regional PVF distributors,
including companies visible through RFQ/BOM language, project-market positioning, and
manufacturer line cards—not only the obvious accounts found in mainstream sales
databases. M2 collects candidates and provenance; it does not decide whether a company
is qualified. The default 100-row plan is a safe calibration batch, not a promise of
100 unique or accepted accounts.

M2 ends at canonical companies plus unresolved raw records. It does not add:

- M3 web research, evidence gathering, or structured fact extraction;
- M4 scoring, rejection policy, or accepted/rejected/uncertain decisions;
- M5 configuration loading, CLI orchestration, persistence workflows, retry/resume,
  output views, or end-to-end calibration;
- people/contact discovery, enrichment waterfalls, email, phone, or outreach;
- Mexico-targeted discovery;
- fuzzy, semantic, embedding, or LLM identity matching;
- a database, frontend, provider SDK, async system, or agent framework;
- automatic live-provider tests or unapproved paid calls.

The critical invariants are:

| ID | Requirement |
| --- | --- |
| INV-01 | The default plan requests at most 100 raw rows and places an aggregate provider-side ceiling of at most `$1.00` on Apify. |
| INV-02 | No generated request targets Mexico. Returned outside-scope rows are preserved and marked for review rather than hidden. |
| INV-03 | Exa works when Apify is disabled or unavailable. |
| INV-04 | Every attempted provider call is recoverable through usage accounting on success or failure; reported dollar usage remains estimated. |
| INV-05 | Missing provider data remains unknown. Query targets and neighboring records never fabricate returned-company facts. |
| INV-06 | Every raw provider row appears exactly once in a canonical company's provenance or in `unresolved_records`. |
| INV-07 | Valid corporate domains outrank weaker identity keys; two different valid domains never merge through name/location. |
| INV-08 | `source_url` is provenance, not identity, and never substitutes for a missing corporate `website_url`. |
| INV-09 | Deduplication is deterministic, permutation-invariant, and performs no I/O, DNS, HTTP, environment, clock, or random calls. |
| INV-10 | M2 leaves M1 evidence, features, confidence, coverage, scores, decisions, and rejection fields at their existing defaults. |

## Architecture and contracts

M2 reuses `CompanyRecord`, `UsageEvent`, `CostTracker`, persistence shapes, and stage
semantics from M1. Existing M1 public imports and serialized keys must remain
compatible. Add only these runtime dependencies:

```toml
dependencies = [
  "httpx>=0.27,<1",
  "tldextract>=5.3,<6",
]
```

The implementation remains separated by responsibility:

| Path | Responsibility |
| --- | --- |
| `src/leads_discovery/models.py` | JSON-safe discovery request, raw record, batch, and result contracts |
| `src/leads_discovery/discovery/base.py` | synchronous provider protocol, sanitized provider error, shared raw-ID behavior |
| `src/leads_discovery/discovery/queries.py` | pure deterministic request catalog and allocation |
| `src/leads_discovery/discovery/exa.py` | Exa HTTP translation only |
| `src/leads_discovery/discovery/apify.py` | one bounded Apify Actor lifecycle only |
| `src/leads_discovery/discovery/__init__.py` | supported discovery exports |
| `src/leads_discovery/dedup.py` | pure normalization, identity grouping, and canonical merge |
| `tests/test_{queries,exa_discovery,apify_discovery,deduplication}.py` | behavioral contract tests |
| `pyproject.toml`, `PLANS.md` | dependencies and M2 completion status |

No production file outside this manifest should change unless an unavoidable repository
constraint is documented without expanding M2. Every new or changed function and class
requires a useful docstring.

Add these public data contracts to `models.py`:

```python
ProviderName = Literal["exa", "apify"]
CountryCode = Literal["US", "CA"]
ErrorKind = Literal[
    "authentication",
    "budget_exhausted",
    "rate_limited",
    "invalid_request",
    "invalid_response",
    "transient",
    "permanent",
]


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    request_id: str
    provider: ProviderName
    query_family: str
    target_country_code: CountryCode
    queries: tuple[str, ...]
    max_results_per_query: int
    max_results_total: int
    max_cost_usd: float | None = None


@dataclass(slots=True)
class DiscoveryRecord:
    record_id: str
    provider: ProviderName
    request_id: str
    target_country_code: CountryCode
    query: str | None
    provider_result_id: str | None
    name: str | None
    source_url: str | None
    website_url: str | None
    city: str | None
    region: str | None
    postal_code: str | None
    country_code: str | None
    title: str | None
    snippet: str | None
    raw_metadata: dict[str, Any]
    retrieved_at: str


@dataclass(slots=True)
class DiscoveryBatch:
    request: DiscoveryRequest
    records: list[DiscoveryRecord]
    usage_events: list[UsageEvent]


@dataclass(slots=True)
class DeduplicationResult:
    companies: list[CompanyRecord]
    unresolved_records: list[DiscoveryRecord]
```

All four models provide `to_dict()` and `from_dict()`, round-trip nested data without
loss, emit JSON-compatible primitives, and do not retain caller-owned mutable
collections. `query` is optional because Apify may omit `searchString`; the adapter
must not substitute the request target or another query. `target_country_code` records
intent only and is never evidence of the returned company's country.

`DiscoveryProvider.search(request) -> DiscoveryBatch` is a synchronous protocol.
Provider constructors receive a nonempty credential and an injected `httpx.Client`;
Apify also receives injectable monotonic-clock and sleep functions for hermetic tests.
Callers own the client. Adapters do not read environment variables, create global
clients, close injected clients, log bodies/secrets, or retry.

`DiscoveryProviderError` is a sanitized `RuntimeError` carrying `provider`, `kind`,
`request_id`, `retryable`, `status_code`, and one `usage_event`. Validation failures
have zero attempted requests; provider failures count every attempted HTTP call. Error
text and exception chains must never expose credentials, authentication headers,
request bodies, full response bodies, or environment values.

## Deterministic request planning

```python
def build_discovery_requests(
    *,
    include_apify: bool,
    max_candidates: int = 100,
    apify_budget_usd: float = 1.0,
) -> tuple[DiscoveryRequest, ...]:
    """Build the complete bounded M2 discovery plan."""
```

| ID | Requirement |
| --- | --- |
| QRY-01 | Accept `max_candidates` only in `1..100` and `apify_budget_usd` only in `0..1`; invalid values fail before provider work. |
| QRY-02 | Catalog order is all U.S. Exa families, all Canada Exa families, U.S. Apify, then Canada Apify. Request IDs use `<provider>:<lower-country>:<family>:v1`; query text/order changes require a version bump. |
| QRY-03 | With Apify disabled, a zero budget, or a computed zero share, allocate every row to Exa. Otherwise Apify receives `min(30, floor(max_candidates * 0.30))` rows and Exa receives the remainder. |
| QRY-04 | Allocate each provider total over its ordered requests with stable quotient/remainder allocation and omit zero-allocation requests. The sum of request totals equals `max_candidates`. |
| QRY-05 | Each Exa request contains one query. Each active Apify country request contains all three Maps terms, uses `ceil(country_total / 3)` per term, and receives an equal share of the supplied Apify budget. |

The five Exa families run for `the United States` and then `Canada`:

| Family | Exact query template |
| --- | --- |
| `core-pvf` | `Independent and regional distributors of pipe, valves, and fittings (PVF) serving industrial customers in {country}` |
| `process-flow` | `Regional process piping, industrial valve, actuation, and flow-control distributors in {country}` |
| `project-rfq` | `Industrial distributors in {country} that quote RFQs, BOMs, takeoffs, or project packages for pipe, valves, and fittings` |
| `line-card` | `Independent distributors in {country} with multi-manufacturer line cards for industrial valves, pipe, fittings, or flow control` |
| `project-markets` | `Regional PVF suppliers in {country} serving process plants, contractors, waterworks, energy, chemical, or industrial projects` |

Apify family `maps-pvf` contains, in order:

```text
pipe valve fitting supplier
industrial valve supplier
industrial pipe and flow control supplier
```

The default Exa-only plan is ten requests × 10 rows. The default combined plan is ten
Exa requests × 7 rows plus U.S. and Canada Apify requests × 15 rows, capped at `$0.50`
per Actor run. Small candidate totals use the same allocation rules rather than special
cases.

## Provider behavior

Common provider behavior:

| ID | Requirement |
| --- | --- |
| PRV-01 | Reject a request for the wrong provider, invalid geography/cardinality, blank queries, totals outside `1..100`, or an invalid/missing Apify cap before HTTP. |
| PRV-02 | Return at most `max_results_total` records in provider order. Preserve the complete JSON result row in `raw_metadata`; optional missing values remain `None`. |
| PRV-03 | Give every successful batch one UTC retrieval timestamp and every row `raw_` plus the first 24 SHA-256 hex characters. Hash provider + request + provider result ID when available; otherwise hash stable parsed/raw identity while excluding retrieval time and credentials. |
| PRV-04 | Emit one aggregate `UsageEvent` per provider operation. It counts all attempted HTTP calls, stores only safe request/run/result metadata, records authenticated dollar usage as estimated, and leaves exact cost unknown. |
| PRV-05 | Adapters make no automatic retries. M5 owns retry, optional-provider continuation, pause, and resume policy. |
| PRV-06 | Malformed required envelopes, invalid JSON, or impossible response types/statuses fail as `invalid_response`; an empty valid result list is successful. |
| PRV-07 | Provider API drift never authorizes broader scope, enabled enrichment, a raised result/spend cap, or fabricated fallback data. |

Exa uses `POST https://api.exa.ai/search` with the `x-api-key` header and exactly this
request body:

```python
{
    "query": request.queries[0],
    "category": "company",
    "type": "auto",
    "numResults": request.max_results_total,
    "userLocation": request.target_country_code,
    "contents": {"highlights": True},
}
```

Do not enable deep search, summaries, full text, schemas, crawl/date filters,
`excludeDomains`, or deprecated/unspecified options. Parse the first company entity
when available: entity/result ID, entity name with title fallback, headquarters,
top-level URL, and ordered highlights capped at 2,000 characters. Map headquarters
city/postal/country and leave region unknown. The sent query is known provenance. The
result URL is both `source_url` and a raw website candidate;
deduplication decides whether it is a valid corporate domain. Keep workforce,
financial, traffic, and other entity details only in `raw_metadata`. Exa usage uses
operation `company_search`, one request, safe request/query/result metadata, and
nonnegative `costDollars.total` when supplied.

Apify uses Actor `compass/crawler-google-places` through API v2 with Bearer auth. One
`DiscoveryRequest` starts at most one Actor run with:

```text
waitForFinish=60
maxItems=request.max_results_total
maxTotalChargeUsd=request.max_cost_usd
```

Actor input contains the three search strings, lower-case country code, English
language, `maxCrawledPlacesPerSearch`, `website="allPlaces"`, and
`skipClosedPlaces=false`. These enrichment controls are exact; omit other paid
filters/enrichments:

| Actor input | Required value |
| --- | --- |
| `scrapePlaceDetailPage`, `includeWebResults`, `scrapeDirectories`, `scrapeContacts` | `false` |
| every `scrapeSocialMediaProfiles` flag (`facebooks`, `instagrams`, `youtubes`, `tiktoks`, `twitters`) | `false` |
| `maximumLeadsEnrichmentRecords`, `maxReviews`, `maxImages`, `maxCompetitorsToAnalyze` | `0` |
| `verifyLeadsEnrichmentEmails`, `scrapeReviewsPersonalData`, `enableCompetitorAnalysis` | `false` |

`READY` and `RUNNING` are nonterminal, and `SUCCEEDED` is success. Poll only the
returned run to a monotonic five-minute deadline with bounded backoff, then fetch its
clean default dataset. A local deadline returns retryable `transient` and leaves the
capped remote run untouched. Never start a replacement run or raise/evade the supplied
cap. `FAILED`, `TIMED-OUT`, and `ABORTED` are terminal errors; unknown states are
invalid responses. If Apify rejects a cap below the Actor's current minimum, return
`invalid_request` without increasing it.

Map `placeId`/`cid`, title, Maps URL, website, structured location, `searchString`, and
description/category into `DiscoveryRecord`; preserve closed status and the full row.
Missing `searchString` stays `None`, and request geography never fills missing returned
country. Apify usage uses operation `google_maps_search`, counts start + polls + dataset
fetch, retains safe run/status/result metadata, and treats authenticated
`usageTotalUsd` as estimated.

Failure classification is shared:

| Condition | Kind | Retryable |
| --- | --- | --- |
| HTTP 401/403 | `authentication` | no |
| HTTP 402 or explicit credit exhaustion | `budget_exhausted` | no |
| HTTP 400/422, including rejected input/cap | `invalid_request` | no |
| HTTP 408/429 | `rate_limited` | yes |
| HTTP 5xx or transport/timeout failure | `transient` | yes |
| malformed JSON/envelope/type/status | `invalid_response` | no |
| other HTTP 4xx or terminal failed/aborted/timed-out Actor | `permanent` | no |

Every provider failure carries its safe usage event. Exceptions must be raised without
chaining unsafe provider bodies or transport messages that may contain secrets.

## Identity resolution and canonical output

`normalize_website_domain()` accepts only HTTP(S) URLs with a valid public hostname.
It rejects userinfo, malformed ports/labels, localhost, `.local`, IP literals, and
unknown public suffixes; lowercases and IDNA-normalizes the host; and returns the
registrable domain. Use one offline extractor so tests and production never fetch the
public suffix list:

```python
tldextract.TLDExtract(
    cache_dir=None,
    suffix_list_urls=(),
    fallback_to_snapshot=True,
    include_psl_private_domains=True,
)
```

Private-suffix support keeps tenants such as `acme.wixsite.com` and
`beta.wixsite.com` distinct. Reject these social, directory, and search domains as
corporate identity:

```text
linkedin.com facebook.com instagram.com x.com twitter.com youtube.com google.com
yelp.com yellowpages.com yellowpages.ca mapquest.com crunchbase.com bloomberg.com
zoominfo.com dnb.com pitchbook.com opencorporates.com
```

`normalize_company_name()` applies NFKC, casefolding, `& -> and`, punctuation and
whitespace collapse, then repeatedly removes trailing legal suffix tokens only:

```text
inc incorporated llc ltd limited corp corporation co company
lp llp plc ulc ltee ltée
```

It never removes internal/trade/geographic words or performs fuzzy matching. Cities use
the same Unicode/punctuation normalization. U.S. states/DC and Canadian
provinces/territories normalize to two-letter codes. Recognized provider-reported
country aliases normalize to ISO alpha-2; unknown values remain explicit, and missing
country is never inferred from the request target. A fallback key exists only when
normalized name, city, region, and country are all present:

```text
<name>|<city>|<region>|<country>
```

Postal code and request target are not fallback identity.

`deduplicate(records) -> DeduplicationResult` applies this complete merge policy:

| ID | Input case | Result |
| --- | --- | --- |
| DED-01 | Record has a valid corporate domain | Group by exact registrable domain. |
| DED-02 | Different valid domains share a fallback key | Keep the domain groups separate. |
| DED-03 | Domainless full fallback matches exactly one domain group | Attach it to that group. |
| DED-04 | Domainless full fallback matches multiple domain groups | Emit an independent review singleton with `AMBIGUOUS_IDENTITY`. |
| DED-05 | Domainless full fallback matches no domain group | Merge only with domainless records sharing that exact fallback. |
| DED-06 | Domainless named record lacks a complete fallback | Emit an independent review singleton with `INSUFFICIENT_IDENTITY`. |
| DED-07 | Valid-domain group has no usable name | Use the domain as provisional name and add `INSUFFICIENT_IDENTITY`. |
| DED-08 | Record has neither usable name nor valid domain | Put it in `unresolved_records`; create no company. |

No other merge rule exists. Provider IDs across providers, source URLs, postal codes,
partial locations, substrings, token overlap, edit distance, phonetics, embeddings, or
LLM judgment do not merge companies.

Company IDs are `cmp_` plus the first 24 SHA-256 hex characters over the authoritative domain,
fallback, or singleton raw identity. They must be deterministic and unique within the
result, including duplicate raw rows. Canonical values follow deterministic rules:

| `CompanyRecord` data | Canonical rule |
| --- | --- |
| normalized name/name/domain | Choose the most frequent normalized name; ties prefer one observed from Exa, then lexicographic value. Select its stripped raw name by Exa, shortest, casefold, then raw-string order. Set both domain fields to the normalized group domain. |
| country | Use the single normalized reported country; conflicting countries produce `None` and `CONFLICTING_COUNTRY`. |
| locations | Store sorted unique normalized location strings built only from reported components. |
| discovery provenance | Union/sort providers and non-`None` exact queries; retain every full raw record in deterministic order. |
| review | Add sorted unique identity codes and `OUTSIDE_GEOGRAPHY` when any reported country is outside `US`/`CA`. |
| pipeline state | Set `status="active"` and `stage_status={"deduplication": "completed"}`; leave M1 evidence/features/scores/decisions/rejections untouched. |
| timestamps/order | Require timezone-aware source timestamps; invalid values fail deduplication. Use earliest/latest instants in UTC, then sort companies by ID and unresolved rows by deterministic raw order. |

Permuting the same multiset of records must produce byte-equivalent serialized output.
Country conflicts and outside geography are review metadata, never destructive filters.
No M2 output receives a final lead decision.

## Verification and completion

The implementation plan will choose exact test names and TDD task order. Automated
evidence must cover:

| Area | Required proof |
| --- | --- |
| planning | exact catalog/order/IDs, boundary validation, 100-row ceiling, 70/30 default allocation, budget-zero behavior, and no Mexico targets |
| Exa | exact HTTP payload/auth, response mappings, raw preservation, caps, usage, malformed responses, and sanitized failure classes |
| Apify | exact cap/minimal input, every disabled enrichment, one-run polling/deadline, no replacement/cap increase, dataset mappings, usage, and terminal failures |
| normalization | URL/IDNA/public/private suffix behavior, denied domains, company suffix boundaries, U.S./Canada regions, country non-inference, and offline execution |
| deduplication | every row of the merge table, forbidden weaker merges, review codes, unique stable IDs, raw-record conservation, canonical fields, and permutation invariance |
| compatibility/safety | nested model round-trips, unchanged M1 behavior/defaults, no credential leakage, and no network access in normal tests |

Tests use `httpx.MockTransport`, injected clock/sleep, and the bundled public-suffix
snapshot. Normal tests and CI never call Exa, Apify, DNS, or public-suffix endpoints.
Any live smoke request requires explicit user approval, real credentials, and a minimal
result/spend cap; it is not acceptance evidence by itself.

Run the full repository gate:

```bash
ruff check .
mypy src tests
pytest
python -m build
```

M2 is complete only when the whole manifest is implemented in one PR, all required
behavioral evidence and repository gates pass, Exa operates without Apify, spend/result
ceilings cannot be bypassed, every raw row satisfies the conservation invariant, and
`PLANS.md` marks M2 complete. The PR body includes the system DAG above and actual gate
results. There is no frontend, so no screenshot is required.

## Approval and change control

Provider documentation may clarify wire syntax but cannot override geography,
provenance, identity conservatism, disabled enrichments, or result/spend ceilings.
Changes to query text/order, payload scope, cost limits, public models, or identity
behavior require this contract and the matching tests to change together.

Wire references retained from the approved design direction:

- https://exa.ai/docs/reference/verticals/company-for-coding-agents
- https://exa.ai/docs/reference/error-codes
- https://docs.apify.com/api/v2/actors-runs-post
- https://docs.apify.com/api/v2/actor-run-get
- https://apify.com/compass/crawler-google-places/input-schema

After product-owner approval, write the separate Superpowers implementation plan. Its
tasks are internal TDD execution order only; they culminate in this one atomic M2 pull
request and do not become product sections, stack layers, or separate approval gates.
