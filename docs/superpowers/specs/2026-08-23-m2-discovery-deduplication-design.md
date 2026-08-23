# M2 Discovery and Deduplication Specification

M2 is one atomic implementation, one pull request, and one validation/review gate. It
must not be split into sub-milestones, stack layers, separately approved sections, or
partial deliveries. The headings below organize this single contract; they are not
implementation slices.

## Objective

Extend the implemented M1 state/cost foundation with the complete company-discovery
and identity-resolution layer:

```text
deterministic U.S./Canada request plan
       ├── Exa company search
       └── optional Apify Google Maps search (hard-capped)
                         ↓
               typed raw records + usage
                         ↓
             conservative local deduplication
                         ↓
          existing CompanyRecord + full provenance
```

M2 ends at deduplicated companies. It does not add research/extraction (M3), scoring
(M4), pipeline/CLI/resume/output orchestration (M5), people enrichment, outreach, a
database, or Mexico discovery.

## Required change

Use the existing `CompanyRecord`, `UsageEvent`, `CostTracker`, and persistence shapes.
Add only `httpx>=0.27,<1` and `tldextract>=5.3,<6`; use direct HTTP, dataclasses, and an
injectable `httpx.Client`, not provider SDKs or a framework.

Implement and test these files together:

```text
Modify  pyproject.toml
Modify  src/leads_discovery/models.py
Create  src/leads_discovery/discovery/{__init__,base,queries,exa,apify}.py
Create  src/leads_discovery/dedup.py
Create  tests/test_{queries,exa_discovery,apify_discovery,deduplication}.py
Modify  PLANS.md
```

Every function/class needs a useful docstring. Preserve public M1 fields and persisted
data compatibility.

Add these typed contracts (with `to_dict`/`from_dict` consistent with M1):

```python
ProviderName = Literal["exa", "apify"]
CountryCode = Literal["US", "CA"]


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
    query: str
    provider_result_id: str | None
    name: str | None
    source_url: str | None       # provenance page; never automatically identity
    website_url: str | None      # candidate corporate website
    city: str | None
    region: str | None
    postal_code: str | None
    country_code: str | None     # provider-reported; never inferred from query target
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

`DiscoveryProvider` is a synchronous protocol with
`search(request: DiscoveryRequest) -> DiscoveryBatch`. Constructors receive a nonempty
credential and an injected client; adapters do not read `.env`, own/close the client,
log secrets/bodies, or retry. They reject requests for another provider and invalid
query cardinality/limits.

## Exact behavior

`build_discovery_requests(include_apify, max_candidates=100,
apify_budget_usd=1.0)` is pure and stable. Reject nonpositive candidate caps and
negative budgets. Request IDs are versioned catalog IDs such as
`exa:us:core-pvf:v1`; changing query text requires a version bump.

Generate five Exa queries for each country (`US` then `CA`) in this family order:

| ID | Template (`{country}` = `the United States` or `Canada`) |
| --- | --- |
| `core-pvf` | `Independent and regional distributors of pipe, valves, and fittings (PVF) serving industrial customers in {country}` |
| `process-flow` | `Regional process piping, industrial valve, actuation, and flow-control distributors in {country}` |
| `project-rfq` | `Industrial distributors in {country} that quote RFQs, BOMs, takeoffs, or project packages for pipe, valves, and fittings` |
| `line-card` | `Independent distributors in {country} with multi-manufacturer line cards for industrial valves, pipe, fittings, or flow control` |
| `project-markets` | `Regional PVF suppliers in {country} serving process plants, contractors, waterworks, energy, chemical, or industrial projects` |

If Apify is disabled or its budget is zero, allocate all candidate slots across the ten
Exa requests with stable quotient/remainder allocation. Otherwise reserve
`min(30, floor(max_candidates * 0.30))` slots for Apify and give the remainder to Exa.
Split Apify slots across one request per country; each contains these queries:

```text
pipe valve fitting supplier
industrial valve supplier
industrial pipe and flow control supplier
```

For each active Apify request, set `max_results_total` to its country allocation,
`max_results_per_query=ceil(country allocation / 3)`, and split
`apify_budget_usd` equally across active requests. Omit zero-allocation requests. The
sum of all `max_results_total` values must never exceed `max_candidates`. The default
Exa+Apify plan is 70 rows (7 × 10) plus 30 rows (15 × 2), with a `$0.50` server cap on
each Apify run. No generated request targets Mexico.

**Exa:** POST `https://api.exa.ai/search` using the `x-api-key` header and exactly the supported
company-search fields: `query`, `category="company"`, `type="auto"`, `numResults`,
`userLocation`, and `contents={"highlights": true}`. Do not use deep search,
summaries, full text, output schemas, date filters, `excludeDomains`, or deprecated
parameters. Parse the first company entity when present: entity/result ID, entity name
with title fallback, headquarters, top-level result URL, highlights capped at 2,000
characters, and the complete bounded result row. The result URL is always
`source_url`; it is also a website candidate only if it passes the corporate-site rule
below. Preserve workforce/financial/traffic entity data only in `raw_metadata`; M3
decides facts. Emit one `UsageEvent(operation="company_search", request_count=1)` with
safe request/query/result metadata and `estimated_cost_usd=costDollars.total` when
present; exact cost stays unknown.

**Apify:** implement the real but optional
`compass/crawler-google-places` Actor using API v2. Start one asynchronous run per
country with Bearer auth and API caps `maxTotalChargeUsd=request.max_cost_usd`,
`maxItems=request.max_results_total`, and `waitForFinish=60`. Actor input contains the
request search strings, lower-case `countryCode`, English language, and
`maxCrawledPlacesPerSearch`. It sets `website="allPlaces"`, `skipClosedPlaces=false`,
`scrapePlaceDetailPage=false`, `includeWebResults=false`, `scrapeDirectories=false`,
`scrapeContacts=false`, every `scrapeSocialMediaProfiles` flag to false,
`maximumLeadsEnrichmentRecords=0`, `verifyLeadsEnrichmentEmails=false`, `maxReviews=0`,
`scrapeReviewsPersonalData=false`, `maxImages=0`, `enableCompetitorAnalysis=false`, and
`maxCompetitorsToAnalyze=0`; omit paid filters and every other enrichment. Preserve
closed-place status for later policy. Poll the same run to a monotonic five-minute
deadline (2/4/8/10-second intervals), then fetch its clean default dataset. Never start
a replacement run automatically. Return at most
`max_results_total` rows, mapping `placeId`/`cid`, `title`, Maps `url` as source,
`website` as candidate website, structured location, `searchString`, description or
category, and full row metadata. Emit one `UsageEvent(operation="google_maps_search")`
for the lifecycle; count all HTTP calls and store safe run/status/result metadata.
Treat authenticated `usageTotalUsd` as estimated, not exact. If Apify rejects a cap
below the Actor's current `minimalMaxTotalChargeUsd`, fail before a run starts as an
invalid request; do not silently raise the cap.

**Failures:** expose one sanitized `DiscoveryProviderError` with provider, kind,
retryable, status, and safe request ID. Kinds are `authentication`,
`budget_exhausted`, `rate_limited`, `invalid_request`, `invalid_response`, `transient`,
and `permanent`. Exa `401/402/400-or-422/429/5xx` map respectively to auth, budget,
invalid, retryable rate limit, and retryable transient; transport timeouts are
transient. Map Apify HTTP failures equivalently and terminal failed/timed-out/aborted
runs without starting another Actor. Never include credentials, headers, request
bodies, full response bodies, or environment values in errors.

**Identity:** `source_url` is provenance, not a key. Normalize only a candidate
`website_url` that is HTTP(S), non-IP, a syntactically public DNS hostname, and not a
known social/directory/search domain (at minimum LinkedIn, Facebook, Instagram,
X/Twitter, YouTube, Google/Maps,
Yelp, Yellow Pages, MapQuest, Crunchbase, Bloomberg, ZoomInfo, D&B, PitchBook, and
OpenCorporates). Use strict URL parsing and one module-level extractor:

```python
tldextract.TLDExtract(
    cache_dir=None,
    suffix_list_urls=(),
    fallback_to_snapshot=True,
    include_psl_private_domains=True,
)
```

Return its `top_domain_under_public_suffix` after lower-case/IDNA normalization. The
bundled PSL prevents surprise network access; private-suffix support keeps
`acme.wixsite.com` distinct from `beta.wixsite.com`. Invalid hosts, IPs, localhost,
and unknown suffixes produce no domain.

Normalize names with NFKC, `casefold`, `& -> and`, punctuation/whitespace collapse,
and removal of trailing legal suffixes only (`inc`, `incorporated`, `llc`, `ltd`,
`limited`, `corp`, `corporation`, `co`, `company`, `lp`, `llp`, `plc`, `ulc`, `ltee`,
`ltée`). Never remove meaningful trade words or use fuzzy/semantic/LLM matching.
Normalize cities similarly, states/provinces to two-letter codes, and provider-reported
country names/aliases to ISO alpha-2 where recognized. Never fill a missing reported
country from `target_country_code`. A fallback key requires exact normalized name +
city + region + country; country alone is insufficient.

Deduplicate in sorted `record_id` order:

1. Group records by exact normalized corporate domain.
2. Never merge two different valid domains through name/location.
3. Attach a domainless record to a domain group only when its exact fallback key
   matches exactly one domain group.
4. If it matches multiple domain groups, keep it as an ambiguous singleton.
5. Otherwise merge only domainless records sharing the exact full fallback key.
6. Keep a domainless named record without a full fallback key as its own review
   singleton. A valid domain is sufficient identity even when location is sparse.
7. Put records with neither name nor valid domain in `unresolved_records`.

Company IDs are deterministic SHA-256 prefixes over `domain:<domain>`,
`fallback:<name>|<city>|<region>|<country>`, or `record:<record_id>`. Raw record IDs
hash provider + request + provider result ID, falling back to stable identity fields or
canonical raw JSON after excluding retrieval time and other volatile request metadata.
Canonical value selection and output ordering must be deterministic.
Use the existing `CompanyRecord`: preserve every full raw record, provider, and exact
query; union/sort locations; derive timestamps from source timestamps; leave
discovery data out of features/scores/decisions. Every emitted company has
`stage_status["deduplication"]="completed"`; identity uncertainty belongs in exact
review codes, not progress state. Use `AMBIGUOUS_IDENTITY` for a domainless record that
matches multiple domain groups and `INSUFFICIENT_IDENTITY` for a domainless incomplete
fallback or a domain-only provisional name. Conflicting reported countries remain raw,
make canonical country unknown, and add `CONFLICTING_COUNTRY`. No source record is
deleted.

Current provider references verified for this design:

- https://exa.ai/docs/reference/verticals/company-for-coding-agents
- https://exa.ai/docs/reference/error-codes
- https://docs.apify.com/api/v2/actors-runs-post
- https://apify.com/compass/crawler-google-places/input-schema

## Verification and completion

Tests must prove the exact query catalog/order/caps/geography; Exa and Apify payload,
parsing, usage, timeout/budget/error behavior with `httpx.MockTransport`; no enabled
Apify enrichments; URL/PSL/IDNA/private-domain normalization; domain and exact fallback
merges; non-merges for different domains, ambiguous matches, fuzzy names, or incomplete
locations; complete provenance; unresolved preservation; and identical canonical
output after shuffled inputs. Normal tests never call providers.

Before the implementation PR is ready, run one approval-gated live Exa smoke request
with one result. Run one Apify smoke only after confirming its accepted minimum cap,
with all enrichments disabled and `maxTotalChargeUsd <= $0.10`. Never run live provider
tests in CI or print credentials/raw payloads.

M2 is done only when the whole change works together, Exa works without Apify, the
default plan cannot request more than 100 rows or charge Apify more than `$1`, every
raw record remains recoverable, deduplication makes zero network calls, and
`ruff check .`, `mypy src tests`, `pytest`, and `python -m build` all pass. Submit one
M2 PR with the DAG above; there are no M2 stack layers.
