# M2 Discovery and Deduplication — Agent-Executable Design Contract

Status: awaiting product-owner review | Revision: 2 | Baseline: `616003f` (merged M1)

Implementation unit: one atomic M2 pull request. The implementation plan is generated
only after this document is approved.

M2 MUST be implemented, validated, submitted, and reviewed as one change. Headings in
this document are navigation aids, not sub-milestones, stack layers, partial deliveries,
or separate approval gates.

## Execution rules

Normative words (`MUST`, `MUST NOT`, `SHOULD`, `MAY`) are requirements. Behavioral
requirement IDs are stable traceability keys; tests and the later implementation plan
MUST cite them.

Authority, highest first:

1. Direct user instructions and repository `AGENTS.md` rules.
2. This M2 contract for product behavior, boundaries, interfaces, and acceptance.
3. Merged M1 code for existing public types and persistence compatibility.
4. Provider documentation for wire-format details only.
5. Nearby repository patterns for details this contract intentionally leaves local.

If two authorities conflict, or provider drift makes a requirement impossible, the
implementer MUST stop and report the exact conflict. It MUST NOT invent a compromise,
silently broaden scope, raise a budget, weaken an invariant, or change a public M1 field.

This contract has no unresolved product or architecture questions. Implementation
details not constrained here are local choices: choose the smallest design consistent
with M1, DRY, YAGNI, testability, and the file responsibilities below.

## Outcome, boundary, and system flow

M2 extends M1's state and cost primitives with bounded U.S./Canada company discovery
and conservative identity resolution:

```text
build_discovery_requests
        │
        ├── ExaDiscoveryProvider.search(request)
        └── ApifyDiscoveryProvider.search(request)  # optional, hard-capped
        │
        ▼
DiscoveryBatch(records + usage events)
        │
        ▼
deduplicate(all raw records)  # pure; zero network
        │
        ├── CompanyRecord[] with complete discovery provenance
        └── unresolved DiscoveryRecord[]
```

M2 is successful when a caller can generate one deterministic bounded request plan,
execute Exa with or without Apify, account for every attempted provider call, and turn
all returned raw records into deterministic canonical companies or an explicit
unresolved collection without losing provenance.

The 100-row default is a safe calibration batch, not a promise of 100 unique or
qualified accounts; later calibrated runs may revise the catalog/cap through this
change-control process.

M2 MUST NOT add:

- M3 web research, evidence collection, or fact extraction;
- M4 features, scoring, rejection policy, or final decisions;
- M5 configuration loading, CLI/runner, checkpoints, persistence orchestration, CSV
  views, or resume/retry policy;
- people/contact discovery, Clay, Apollo, Instantly, email, phone, or outreach;
- Mexico-targeted discovery;
- fuzzy, semantic, embedding, or LLM identity matching;
- a database, frontend, dashboard, async framework, agent framework, or provider SDK;
- automatic live-provider tests or unapproved paid calls.

## Baseline and file contract

M1 already provides `CompanyRecord`, `UsageEvent`, `CostTracker`, `RunCheckpoint`,
append-safe JSONL, atomic JSON, and stage-resume helpers. M2 MUST reuse those shapes.
Existing M1 serialized keys, defaults, semantics, and imports MUST remain compatible.

Production dependencies added to `[project].dependencies`:

```toml
dependencies = [
  "httpx>=0.27,<1",
  "tldextract>=5.3,<6",
]
```

No other production dependency is authorized. `.env.example` already contains
`EXA_API_KEY` and `APIFY_TOKEN`, so M2 does not modify or load it.

| Path | Change | Single responsibility | Public output |
| --- | --- | --- | --- |
| `pyproject.toml` | modify | Declare the two runtime dependencies | installable package |
| `src/leads_discovery/models.py` | modify | Own JSON-safe discovery data contracts | request/record/batch/result models |
| `src/leads_discovery/discovery/__init__.py` | create | Export the supported discovery API only | providers, protocol, error, planner |
| `src/leads_discovery/discovery/base.py` | create | Provider protocol, sanitized error, shared record-ID helper | `DiscoveryProvider`, `DiscoveryProviderError` |
| `src/leads_discovery/discovery/queries.py` | create | Pure query catalog and bounded allocation | `build_discovery_requests` |
| `src/leads_discovery/discovery/exa.py` | create | Exa HTTP request/response translation | `ExaDiscoveryProvider` |
| `src/leads_discovery/discovery/apify.py` | create | One capped Apify Actor lifecycle | `ApifyDiscoveryProvider` |
| `src/leads_discovery/dedup.py` | create | Pure normalization, identity resolution, canonical merge | `deduplicate`, normalization helpers |
| `tests/test_queries.py` | create | Planner contract tests | QRY evidence |
| `tests/test_exa_discovery.py` | create | Exa adapter contract tests | EXA/ERR evidence |
| `tests/test_apify_discovery.py` | create | Apify lifecycle contract tests | APY/ERR evidence |
| `tests/test_deduplication.py` | create | Normalization/merge/property tests | ID/CAN evidence |
| `PLANS.md` | modify at completion | Mark M2 complete without creating stack layers | milestone status |

Every new or changed function and class MUST have a useful docstring. Presentation,
provider translation, and identity logic MUST remain separated according to this table.
No production file outside this manifest may change unless an unavoidable repository
constraint is documented in the commit and does not expand M2.

## Public data and error contracts

Add these types to `models.py`. `query` is optional because provider provenance MUST
remain unknown when Apify omits `searchString`; the target or first request query MUST
NOT be substituted.

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

`MOD-001`: All four models MUST implement `to_dict()` and `from_dict()` and round-trip
nested objects without loss. Serialization MUST contain only JSON-compatible
primitives. `from_dict()` MUST copy input collections rather than retain caller-owned
mutable lists or dictionaries.

`MOD-002`: Existing M1 model serialization MUST remain byte-shape compatible. New
model deserialization MUST reject a missing required field, an unknown field, an
invalid literal, or a non-JSON-compatible `raw_metadata` value with `TypeError` or
`ValueError`.

Field meaning is exact:

| Field | Contract |
| --- | --- |
| `request_id` | Versioned stable catalog identity, not a random run ID |
| `target_country_code` | Intended request geography; never evidence of returned-company location |
| `query` | Exact provider-reported query when known; `None` means unknown |
| `provider_result_id` | Provider's stable entity/place/result ID when supplied |
| `source_url` | Page that produced the record; provenance only, never automatically identity |
| `website_url` | Raw provider candidate for a corporate site; validation happens in dedup |
| location fields | Provider-reported values, normalized only as explicitly required below |
| `raw_metadata` | Complete JSON result row needed to replay/debug parsing; no credentials |
| `retrieved_at` | Adapter-generated timezone-aware ISO-8601 timestamp in UTC for this successful response |

`MOD-003`: The provider API is synchronous and has these exact public call signatures:

```text
DiscoveryProvider.search(
    self, request: DiscoveryRequest
) -> DiscoveryBatch

ExaDiscoveryProvider.__init__(
    self, api_key: str, client: httpx.Client
) -> None

ApifyDiscoveryProvider.__init__(
    self,
    token: str,
    client: httpx.Client,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None
```

`MOD-004`: `DiscoveryProviderError` MUST subclass `RuntimeError` and has this exact
constructor and public fields:

```text
DiscoveryProviderError.__init__(
    self,
    *,
    provider: ProviderName,
    kind: ErrorKind,
    request_id: str,
    retryable: bool,
    status_code: int | None,
    usage_event: UsageEvent,
    safe_message: str,
) -> None
```

The error exposes the seven keyword values above as read-only attributes; `str(error)`
returns `safe_message`. `repr(error)` may add only the other six safe fields. Adapters
MUST select `safe_message` from static adapter-owned wording plus safe identifiers; they
MUST NOT interpolate provider bodies, exception text, headers, credentials, or URLs.

`MOD-005`: Blank credentials raise `ValueError` before any HTTP call. Callers own and
close the injected client. Adapters MUST NOT read environment variables, own global
clients, log request/response bodies, or retry. Every `DiscoveryProviderError` MUST
carry one safe `UsageEvent`, including `request_count=0` for validation failures and all
attempted HTTP calls for provider/lifecycle failures. Its string, repr, and exception
chain MUST expose only the listed safe values, never credentials, auth headers, request
bodies, full responses, or environment values.

## Global invariants

| ID | Normative requirement |
| --- | --- |
| INV-001 | The normal default request plan MUST request no more than 100 raw rows. |
| INV-002 | The Apify plan MUST have a provider-side aggregate ceiling of at most `$1.00`. |
| INV-003 | No request builder path may target Mexico; unexpected outside-scope results are preserved, not hidden. |
| INV-004 | Every provider row MUST appear exactly once in a canonical company's `discovery_records` or in `unresolved_records`. |
| INV-005 | Missing provider data MUST remain unknown; query targets and neighboring records MUST NOT fabricate facts. |
| INV-006 | Deduplication MUST be deterministic and MUST perform zero I/O, DNS, HTTP, environment, clock, or random calls. |
| INV-007 | Different valid corporate domains MUST never merge through a weaker key. |
| INV-008 | `source_url` MUST never become an identity key merely because `website_url` is absent. |
| INV-009 | Provider usage MUST be recoverable on success and failure; reported dollar usage remains estimated. |
| INV-010 | Exa MUST work when Apify is disabled or no Apify adapter/token exists. |
| INV-011 | Normal automated tests MUST be hermetic and MUST make zero live provider or public-suffix requests. |
| INV-012 | M2 MUST leave `features`, confidence, coverage, scores, decisions, evidence, and rejection reasons untouched/defaulted. |
| INV-013 | Every emitted `CompanyRecord.company_id` MUST be unique within one deduplication result, including duplicate raw provider rows. |

## Deterministic request planning

Public signature:

```python
def build_discovery_requests(
    *,
    include_apify: bool,
    max_candidates: int = 100,
    apify_budget_usd: float = 1.0,
) -> tuple[DiscoveryRequest, ...]:
    """Build the complete deterministic M2 discovery request plan."""
```

| ID | Normative requirement |
| --- | --- |
| QRY-001 | Require `type(include_apify) is bool`; require `type(max_candidates) is int` and `1 <= max_candidates <= 100`; require `apify_budget_usd` to be an `int`/`float` but not `bool`, finite, and in `0.0..1.0`. Reject every other value with `TypeError` or `ValueError`, and store accepted budget values as `float`. |
| QRY-002 | Catalog order is all U.S. Exa families, all Canada Exa families, U.S. Apify, Canada Apify. |
| QRY-003 | A request with zero allocated rows MUST be omitted. Returned requests MUST be a tuple. |
| QRY-004 | IDs are `<provider>:<lower-country>:<family>:v1`; changing query text/order requires a catalog version bump. |
| QRY-005 | If Apify is disabled, budget is zero, or its computed share is zero, allocate every row to Exa. |
| QRY-006 | Otherwise Apify receives `min(30, floor(max_candidates * 0.30))` rows and Exa receives the remainder. |
| QRY-007 | Allocate an integer total over an ordered request list using `q, r = divmod(total, count)`; entries `0..r-1` receive `q+1` and the rest `q`. |
| QRY-008 | Exa requests contain exactly one query and set per-query and total maxima to the same allocation. |
| QRY-009 | Each active Apify country request contains all three Maps terms, sets total to its country allocation, and per-query to `ceil(total / 3)`. |
| QRY-010 | Split the Apify budget equally across active Apify requests; the sum of their `max_cost_usd` MUST NOT exceed the input budget. |
| QRY-011 | The sum of every request's `max_results_total` MUST equal `max_candidates`. |

Exa families and exact text:

| Family | Template (`{country}` is `the United States` or `Canada`) |
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

Required examples:

| Inputs | Exact allocation |
| --- | --- |
| `include_apify=False, max=100, budget=1` | ten Exa requests × 10; no Apify |
| `include_apify=True, max=100, budget=1` | ten Exa × 7; U.S. Apify 15/$0.50; Canada Apify 15/$0.50 |
| `include_apify=True, max=100, budget=0` | ten Exa × 10; no Apify |
| `include_apify=True, max=1, budget=1` | U.S. `core-pvf` Exa × 1; no Apify |
| `include_apify=True, max=4, budget=1` | first three Exa requests × 1; U.S. Apify × 1/$1.00 |

## Shared provider behavior

| ID | Normative requirement |
| --- | --- |
| PRV-001 | `search` MUST reject a request for the wrong provider before HTTP with `DiscoveryProviderError(kind="invalid_request")` and a zero-request usage event. |
| PRV-002 | It MUST reject an invalid target country; blank IDs/families/query strings; totals outside `1..100`; per-query limits outside `1..max_results_total`; Exa cardinality other than one or non-`None` cost; and Apify cardinality other than three or cost outside `(0, 1]`, the same way before HTTP. |
| PRV-003 | It MUST issue only the calls required for that one request and MUST return at most `max_results_total` records. |
| PRV-004 | It MUST generate one `datetime.now(UTC).isoformat()` batch timestamp after the final successful response and apply it to every parsed row. |
| PRV-005 | It MUST preserve optional/missing row fields as `None`; mapped strings remain verbatim when they contain a non-whitespace character and otherwise become `None`. Malformed required envelopes are `invalid_response`, not empty success. |
| PRV-006 | Raw rows MUST remain JSON-compatible and unmodified in `raw_metadata`. |
| PRV-007 | Record ordering MUST match provider response ordering; dedup owns canonical sorting. |
| PRV-008 | Adapters MUST NOT retry. A later M5 orchestrator owns retry, pause, resume, and optional-provider continuation. |
| PRV-009 | Every parsed record MUST receive the deterministic `raw_` ID defined immediately below. |

Record IDs use:

```python
digest = sha256(identity_bytes).hexdigest()[:24]
record_id = f"raw_{digest}"
```

When `provider_result_id` is non-`None`, derive bytes with actual NUL separators:

```python
identity_bytes = (
    f"{record.provider}\0{record.request_id}\0{record.provider_result_id}"
).encode("utf-8")
```

Otherwise UTF-8 encode `json.dumps(identity, sort_keys=True, separators=(",", ":"),
ensure_ascii=False, allow_nan=False)`, where `identity` has exactly these keys and
values from the record:

```python
{
    "provider": record.provider,
    "request_id": record.request_id,
    "target_country_code": record.target_country_code,
    "query": record.query,
    "name": record.name,
    "source_url": record.source_url,
    "website_url": record.website_url,
    "city": record.city,
    "region": record.region,
    "postal_code": record.postal_code,
    "country_code": record.country_code,
    "title": record.title,
    "snippet": record.snippet,
    "raw_metadata": record.raw_metadata,
}
```

Record/retrieval time, HTTP headers, call counts, rankings outside the provider row,
and credentials MUST NOT enter the hash.

## Exa adapter contract

Endpoint: `POST https://api.exa.ai/search`; authentication: `x-api-key` header;
operation: `company_search`.

The request JSON MUST equal this Python mapping after substituting request values:

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

| ID | Normative requirement |
| --- | --- |
| EXA-001 | Do not send deep search, text, summary, output schema, date/crawl filters, `excludeDomains`, deprecated options, or unspecified fields. |
| EXA-002 | A successful envelope MUST contain `results` as a list; an empty list is valid. Every item MUST be an object. |
| EXA-003 | If `entities` is present it MUST be a list of objects. Select the first item whose `type == "company"`; unrelated entities are ignored. Present `properties`/`headquarters` containers MUST be objects. |
| EXA-004 | `provider_result_id` is the first nonempty string from company entity `id`, then top-level result `id`, else `None`. |
| EXA-005 | `name` is entity `properties.name`, else top-level `title`; `title` is top-level `title`; `query` is the sent request query. Apply PRV-005 to every value. |
| EXA-006 | Map entity `properties.headquarters.city`, `postalCode`, and `country` to city/postal/country. Exa supplies no reliable region here, so `region=None`. |
| EXA-007 | `source_url` and raw `website_url` both receive top-level `url` when it is a string; dedup decides whether it is corporate. |
| EXA-008 | Missing `highlights` yields `snippet=None`; otherwise it MUST be a list of strings. Join in order with `"\n"`, then retain the first 2,000 characters; full highlights remain raw. |
| EXA-009 | Workforce, financial, traffic, schema-version, and other entity data remain only in `raw_metadata`; M2 does not promote them to company facts. |
| EXA-010 | Return one success `UsageEvent` with provider `exa`, operation `company_search`, `request_count=1`, estimated cost from a finite nonnegative integer/float (not boolean) at `costDollars.total` or `None`, and exact cost `None`. |
| EXA-011 | Usage metadata has exactly `request_id`, `query_family`, `target_country_code`, `provider_request_id`, and `result_count`; request ID is top-level string `requestId` or `None`, and result count is the returned record count. |

## Apify adapter contract

Actor: `compass/crawler-google-places` via Apify API v2; authentication: Bearer header,
never a URL query token; operation: `google_maps_search`.

Lifecycle:

```text
POST /v2/acts/compass~crawler-google-places/runs
  ?waitForFinish=60
  &maxItems=<request.max_results_total>
  &maxTotalChargeUsd=<request.max_cost_usd>
  ├── SUCCEEDED ────────────────────────────────────────────────┐
  ├── READY/RUNNING                                             │
  │     └── GET /v2/actor-runs/<run_id>                          │
  │           ?waitForFinish=<poll_wait_seconds>                 │
  │           ├── READY/RUNNING → repeat same GET               │
  │           ├── SUCCEEDED ────────────────────────────────────┤
  │           └── FAILED/TIMED-OUT/ABORTED → safe error         │
  └── FAILED/TIMED-OUT/ABORTED → safe error                     │
                                                                ▼
        GET /v2/datasets/<defaultDatasetId>/items
          ?clean=true&limit=<max_results_total>
```

`APY-012`: Start a five-minute monotonic deadline immediately before the POST. After
the initial 60-second server wait, poll only the returned run ID after 2, 4, 8, and then
10 seconds for every remaining poll, clipping a sleep to the remaining time. Before a
call, fail locally when no time remains; otherwise pass the positive remaining duration
as that POST/poll call's `httpx` timeout. For polls, also set `poll_wait_seconds` to
`min(60, max(1, ceil(remaining_seconds)))`. Never start, resurrect, reboot, abort, or
silently replace another Actor run. On local deadline, raise retryable `transient`;
provider-side caps remain the spend guard.

Actor input MUST be exactly the request/search controls plus explicit free/default
controls below; paid filters and unspecified enrichments are omitted:

```python
{
    "searchStringsArray": list(request.queries),
    "countryCode": request.target_country_code.lower(),
    "language": "en",
    "maxCrawledPlacesPerSearch": request.max_results_per_query,
    "website": "allPlaces",
    "skipClosedPlaces": False,
    "scrapePlaceDetailPage": False,
    "includeWebResults": False,
    "scrapeDirectories": False,
    "scrapeContacts": False,
    "scrapeSocialMediaProfiles": {
        "facebooks": False,
        "instagrams": False,
        "youtubes": False,
        "tiktoks": False,
        "twitters": False,
    },
    "maximumLeadsEnrichmentRecords": 0,
    "verifyLeadsEnrichmentEmails": False,
    "maxReviews": 0,
    "scrapeReviewsPersonalData": False,
    "maxImages": 0,
    "enableCompetitorAnalysis": False,
    "maxCompetitorsToAnalyze": 0,
}
```

| ID | Normative requirement |
| --- | --- |
| APY-001 | One request starts at most one Actor run and MUST have a positive `max_cost_usd` no greater than $1. |
| APY-002 | Accepted nonterminal statuses are `READY` and `RUNNING`; success is `SUCCEEDED`; `FAILED`, `TIMED-OUT`, and `ABORTED` are permanent terminal failures. Unknown statuses are `invalid_response`. |
| APY-003 | Every start/poll body MUST be an object containing a `data` object. `data.id` MUST be the same nonempty string run ID on every response; success MUST provide nonempty string `data.defaultDatasetId`. |
| APY-004 | Dataset output MUST be a JSON list of objects. Preserve closed-place status in raw metadata; never filter it in M2. |
| APY-005 | Map ID from the first nonempty string at `placeId`, then `cid`; map both name and title from `title`, source from Maps `url`, and website candidate from `website`. |
| APY-006 | Map `city`, `state` to region, `postalCode`, `countryCode`, and `searchString` to query. Snippet is the first nonempty string from `description`, `category`, then `categoryName`, truncated to 2,000 characters. Missing `searchString` produces `query=None`. |
| APY-007 | Uppercase a provider-reported two-letter country code; never fill country from request target. |
| APY-008 | Slice returned records to `max_results_total` even though `maxItems` is also sent. |
| APY-009 | Return one success usage event whose request count equals POST + polls + dataset GET and whose estimated cost is the latest finite nonnegative integer/float (not boolean) `data.usageTotalUsd` seen; exact cost is `None`. |
| APY-010 | Usage metadata has exactly `request_id`, `query_family`, `target_country_code`, `run_id`, `terminal_status`, and `result_count`; values use the validated run, final status, and returned record count. |
| APY-011 | If Apify rejects `maxTotalChargeUsd` below its current `minimalMaxTotalChargeUsd`, surface `invalid_request`; never raise the supplied cap. |

## Failure classification and accounting

| ID | Condition | Kind | Retryable |
| --- | --- | --- | --- |
| ERR-001 | HTTP 401 or 403 | `authentication` | no |
| ERR-002 | HTTP 402 or explicit credit/budget exhaustion | `budget_exhausted` | no |
| ERR-003 | HTTP 400 or 422, including rejected cap/input | `invalid_request` | no |
| ERR-004 | HTTP 408 or 429 | `rate_limited` | yes |
| ERR-005 | HTTP 5xx | `transient` | yes |
| ERR-006 | connect/read/write/pool timeout or transport failure | `transient` | yes |
| ERR-007 | invalid JSON, missing required envelope, impossible type/status | `invalid_response` | no |
| ERR-008 | other HTTP 4xx or Apify failed/aborted/timed-out terminal run | `permanent` | no |

`ERR-009`: On failure, the error's `UsageEvent` MUST use the provider operation, count
every attempted HTTP call, capture any safely available estimated usage, leave exact
cost unknown, and include only metadata permitted by the corresponding success contract
plus `failure_kind`.

`ERR-010`: Raising from a provider exception/body is prohibited when the chained
exception can expose unsafe data; adapters MUST sanitize and raise from `None`.

## Website, name, and location normalization

`normalize_website_domain(url: str | None) -> str | None` is pure and executes these
requirements in ID order:

| ID | Normative requirement |
| --- | --- |
| WEB-001 | Strip surrounding whitespace and parse strictly. Require `http` or `https`, a hostname, no userinfo, and no malformed port. |
| WEB-002 | Lowercase the host, remove one terminal dot, and encode Unicode labels with IDNA. |
| WEB-003 | Reject localhost, `.local`, IP literals, malformed labels, and hosts with an unknown public suffix. |
| WEB-004 | Extract through exactly one module-level offline extractor configured as shown below. |
| WEB-005 | Return `top_domain_under_public_suffix`; never return a subdomain, bare suffix, or empty value. |
| WEB-006 | Return `None` when that registrable domain equals or is below a denied domain in the exact list below. |

The `WEB-004` extractor is:

```python
tldextract.TLDExtract(
    cache_dir=None,
    suffix_list_urls=(),
    fallback_to_snapshot=True,
    include_psl_private_domains=True,
)
```

The `WEB-006` denied registrable domains are:

```text
linkedin.com     facebook.com      instagram.com     x.com
twitter.com      youtube.com       google.com        yelp.com
yellowpages.com  yellowpages.ca    mapquest.com      crunchbase.com
bloomberg.com    zoominfo.com      dnb.com           pitchbook.com
opencorporates.com
```

`WEB-007`: The extractor MUST make zero network calls. Private-suffix support MUST keep
`acme.wixsite.com` distinct from `beta.wixsite.com`.

`NAM-001`: `normalize_company_name(name: str | None) -> str | None` MUST apply NFKC,
casefold, replace ASCII `&` with ` and `, replace every Unicode punctuation code point
with ASCII space, collapse all whitespace runs to one ASCII space, trim, and repeatedly
remove only trailing legal tokens:

```text
inc incorporated llc ltd limited corp corporation co company
lp llp plc ulc ltee ltée
```

`NAM-002`: Suffix comparison happens after punctuation collapse and removes whole
tokens only. It MUST NOT remove internal/legal-looking tokens, trade words, geographic
words, or perform fuzzy/semantic matching. Empty output becomes `None`.

`LOC-001`: Cities use NFKC, casefold, and the exact punctuation/whitespace replacement
from `NAM-001` without ampersand expansion or suffix removal; empty output is `None`.

`LOC-002`: Regions use a complete case-insensitive U.S. state/DC and Canadian
province/territory name-to-two-letter map. Already valid two-letter codes uppercase;
unknown nonempty values use the same normalized city text and are not inferred.

`LOC-003`: Provider-reported country aliases normalize case-insensitively to ISO
alpha-2 where recognized, including U.S./USA/United States → `US` and Canada/CA → `CA`.
Unknown nonempty country values are NFKC-normalized, uppercased, trimmed, and
whitespace-collapsed but not inferred.

`LOC-004`: Postal codes use NFKC, uppercase, surrounding-whitespace trim, and internal
whitespace collapse; punctuation is preserved and empty output is `None`.

`LOC-005`: A fallback identity key exists only when normalized name, city, region, and
country are all nonempty:

```text
<name>|<city>|<region>|<country>
```

Postal code and request target MUST NOT participate in fallback identity.

## Deduplication algorithm

Public signature:

```python
def deduplicate(records: Iterable[DiscoveryRecord]) -> DeduplicationResult:
    """Resolve raw records into deterministic companies without I/O."""
```

`DED-001`: Materialize and sort input by `(record_id, canonical_record_json)`, where
canonical JSON uses sorted keys, compact separators, and Unicode characters unescaped.
Before grouping, require each `retrieved_at` to parse as a timezone-aware ISO-8601
instant; otherwise raise `ValueError`. Convert valid instants to UTC for comparison and
output.

Then apply exactly:

| ID | Case | Required result |
| --- | --- | --- |
| DED-002 | Valid corporate domain | Group by exact normalized domain. |
| DED-003 | Two different valid domains share fallback | Keep separate; domain authority wins. |
| DED-004 | Domainless record's full fallback matches one domain group | Attach to that domain group. |
| DED-005 | Domainless record's full fallback matches multiple domain groups | Keep that raw record as its own singleton with `AMBIGUOUS_IDENTITY`. Do not merge it with equally ambiguous rows. |
| DED-006 | Domainless full fallback matches no domain group | Merge with other domainless records having that exact full fallback. |
| DED-007 | Domainless named record lacks a full fallback | Keep as its own singleton with `INSUFFICIENT_IDENTITY`. |
| DED-008 | Valid-domain group has no usable name | Use normalized domain as provisional display name and add `INSUFFICIENT_IDENTITY`. |
| DED-009 | Neither usable name nor valid domain | Put in `unresolved_records`; create no company. |

`DED-010`: No other merge rule exists. In particular, matching provider IDs across
providers, source URLs, postal codes, partial locations, substrings, token overlap,
edit distance, phonetics, embeddings, or LLM judgment MUST NOT merge records.

`DED-011`: Assign each output group an explicit kind when the decision table creates it:
`domain` for `DED-002`/`DED-004`/`DED-008`, `fallback` for `DED-006`, and `singleton`
for `DED-005`/`DED-007`. Derive identity from the group kind exactly as follows:

```python
if group_kind == "domain":
    key = f"domain:{domain}"
elif group_kind == "fallback":
    key = f"fallback:{fallback_key}"
else:
    record_id = records[0].record_id
    ordinal = singleton_ordinal_for_record_id
    key = f"record:{record_id}"
    if ordinal > 0:
        key = f"{key}:duplicate:{ordinal}"

company_id = f"cmp_{sha256(key.encode('utf-8')).hexdigest()[:24]}"
```

The required domain/fallback values MUST be non-`None` for their group kinds. The last
branch is valid only for a one-record singleton; `records[0]` is therefore unambiguous.
In particular, an ambiguous `DED-005` record MUST use the singleton branch even though
it has a full fallback key. `singleton_ordinal_for_record_id` is the zero-based position
among singleton groups sharing that raw ID, ordered by canonical record JSON; identical
JSON ties occupy consecutive ordinals. Thus a normal unique raw ID retains the stable
`record:<record_id>` key while duplicate rows receive distinct IDs without merging.

Canonical selection is deterministic:

| ID | `CompanyRecord` field | Required value |
| --- | --- | --- |
| CAN-001 | `company_id` | Exact `DED-011` hash rule |
| CAN-002 | `normalized_domain` / `domain` | Group's normalized domain, else `None` |
| CAN-003 | `normalized_name` | Most frequent nonempty normalized name; ties prefer a name observed from Exa, then lexicographic normalized value |
| CAN-004 | `name` | Among stripped raw names mapping to the selected normalized name: Exa before Apify, then shortest value, lexicographic casefold value, then lexicographic raw value; return that stripped value, or domain if no name |
| CAN-005 | `country` | The only distinct normalized reported country; `None` if none or conflicting |
| CAN-006 | `locations_if_known` | Unique strings built as `", ".join` over nonempty normalized city, region, postal code, and country in that order; sort by lexicographic casefold value then raw value |
| CAN-007 | `status` | `active` |
| CAN-008 | `discovery_sources` / `discovery_queries` | Sorted unique provider values / sorted unique non-`None` exact query values |
| CAN-009 | `discovery_records` | Every full `DiscoveryRecord.to_dict()`, sorted by `(record_id, canonical JSON)` |
| CAN-010 | `review_reasons` / `stage_status` | Sorted unique codes below / `{"deduplication": "completed"}` |
| CAN-011 | `created_at` / `updated_at` | Earliest / latest source instant, rendered exactly by `instant.astimezone(UTC).isoformat()` |
| CAN-012 | evidence/features/confidence/coverage/scores/decisions/rejections | Existing M1 defaults, untouched |
| CAN-013 | result company order | Sort `companies` by `company_id` ascending |

Review codes:

| ID | Code | Trigger |
| --- | --- | --- |
| REV-001 | `AMBIGUOUS_IDENTITY` | Domainless full fallback maps to multiple domain groups |
| REV-002 | `INSUFFICIENT_IDENTITY` | Domainless incomplete singleton or domain-only provisional name |
| REV-003 | `CONFLICTING_COUNTRY` | Group contains more than one distinct reported country |
| REV-004 | `OUTSIDE_GEOGRAPHY` | Any group record reports a country other than `US` or `CA` |

`DED-012`: `unresolved_records` MUST use the same deterministic raw ordering. Country
conflict or outside geography is review metadata, never destructive filtering. No M2
output receives a final decision.

`DED-013`: Permuting the same multiset of input records MUST produce byte-equivalent
`DeduplicationResult.to_dict()` output.

## Acceptance evidence contract

Implementation follows TDD: each behavioral test is written and observed failing before
the minimal production behavior is added. Tests MUST cite covered requirement IDs in
their docstring or a nearby comment. Test names below are required or may be split only
when the mapping remains obvious.

| Evidence | Contract IDs | Required oracle |
| --- | --- | --- |
| `test_queries.py::test_default_exa_plan` | INV-001, INV-010; QRY-002..005, QRY-007..009, QRY-011 | Exact order, IDs, text, ten × 10, tuple type, and no Apify dependency |
| `test_queries.py::test_default_exa_apify_plan` | INV-001..003; QRY-002..011 | Exact 70/30 allocation, U.S./Canada order, three-term Maps requests, and $0.50 caps |
| `test_queries.py::test_planner_boundaries` | INV-001..002; QRY-001, QRY-003, QRY-005..011 | Max 1/max 4 examples, omitted zero allocations, budget zero, and every rejected range |
| `test_queries.py::test_query_catalog_has_no_mexico` | INV-003; QRY-002, QRY-004 | No request target or query contains Mexico; exact catalog versions remain stable |
| `test_exa_discovery.py::test_exa_rejects_invalid_requests_before_http` | MOD-003, MOD-005; PRV-001..002, PRV-008 | Wrong provider and every invalid shape make zero calls; blank key and client ownership are enforced |
| `test_exa_discovery.py::test_exa_payload_and_parse` | INV-005, INV-008; PRV-003..007, PRV-009; EXA-001..009 | `httpx.MockTransport` sees exact JSON/header; entity fallback, timestamp, location, snippet, raw row, ordering, cap, and ID mappings are exact |
| `test_exa_discovery.py::test_exa_usage` | INV-009; EXA-010..011 | One call, allowlisted metadata, and estimated-versus-exact cost semantics |
| `test_exa_discovery.py::test_exa_errors_are_sanitized` | MOD-004..005; ERR-001..010 | Every HTTP/transport/envelope class, retryability, call count, safe fields, and secret/body/chain absence |
| `test_apify_discovery.py::test_apify_rejects_invalid_requests_before_http` | MOD-003, MOD-005; PRV-001..002, PRV-008; APY-001 | Wrong provider, invalid cardinality/limits/cap, and blank token make zero calls |
| `test_apify_discovery.py::test_apify_start_payload_is_capped_and_minimal` | INV-002; PRV-003; APY-001 | Exact Actor, Bearer auth, URL params, body, cap, and all explicit no-enrichment values |
| `test_apify_discovery.py::test_apify_running_to_success` | INV-005, INV-009; PRV-003..007, PRV-009; APY-002..010, APY-012 | Fake clock/sleep proves 2/4/8/10 polling, bounded server wait/timeout, same run, envelope and row mappings, timestamp/order/cap/ID, and request/cost accounting |
| `test_apify_discovery.py::test_apify_terminal_and_deadline_failures` | MOD-004..005; APY-002..003, APY-009..010, APY-012; ERR-005..010 | No replacement/abort, exact terminal/deadline kinds, clipped final wait, safe errors, and usage accounting |
| `test_apify_discovery.py::test_apify_rejected_cap_is_not_raised` | APY-011; ERR-003, ERR-009..010 | Provider minimum error remains `invalid_request`; no second POST or cap increase occurs |
| `test_deduplication.py::test_model_round_trips` | MOD-001..002 | Every new model round-trips nested data, copies mutable input, rejects bad input, and leaves M1 serialization unchanged |
| `test_deduplication.py::test_domain_normalization` | INV-006, INV-008, INV-011; WEB-001..007 | Scheme, IDNA, PSL/private suffix, IP, localhost, userinfo, port, unknown suffix, deny list, and offline extraction table |
| `test_deduplication.py::test_name_and_location_normalization` | INV-005..006; NAM-001..002; LOC-001..005 | NFKC, ampersand, suffix boundary, unknown preservation, full U.S./Canada maps, aliases, postal formatting, and exact fallback completeness |
| `test_deduplication.py::test_identity_decision_table` | INV-004..008, INV-012..013; DED-002..010, DED-012; REV-001..002 | Every merge-table row plus forbidden source/fuzzy/partial/different-domain merges, unresolved ordering, duplicate singleton IDs, and no final decision |
| `test_deduplication.py::test_canonical_merge_preserves_every_record` | INV-004..005, INV-012..013; DED-001, DED-011..012; CAN-001..013; REV-003..004 | Timestamp rejection/UTC normalization, exact group-kind ID and field selection, unique IDs/company ordering, country review, untouched M1 defaults, and raw-row conservation |
| `test_deduplication.py::test_dedup_is_permutation_invariant` | INV-006; DED-013 | Every permutation of a bounded fixture produces byte-equal `to_dict()` output and performs no I/O |
| existing M1 tests | MOD-002; INV-012 | Public and persisted M1 behavior remains unchanged |

Hermetic tests MUST use `httpx.MockTransport`, injected clock/sleep, and the bundled
`tldextract` snapshot. They MUST NOT inspect real environment credentials or call DNS,
Exa, Apify, or public suffix endpoints.

After user approval—and never in CI—perform:

1. One Exa smoke call with `numResults=1`.
2. One Apify smoke only if the Actor accepts `maxItems=1` with
   `maxTotalChargeUsd <= $0.10`; if its minimum is higher, do not run and report that
   the contract tests are the available evidence.

Do not print credentials, headers, full raw responses, or commit smoke output. A smoke
failure does not authorize a payload/budget change; diagnose and update this contract
before implementation changes.

Full local gate, in this order:

```bash
ruff check .
mypy src tests
pytest
python -m build
```

The M2 PR body MUST contain the system DAG from this contract and report each gate's
actual result. No frontend exists, so no screenshot is required.

## Completion and change control

M2 is complete only when:

- every manifest file is implemented together in one M2 PR;
- every requirement maps to passing automated evidence above;
- all four full-repository gates pass;
- the default request plan cannot exceed 100 rows or $1 Apify spend;
- Exa operates without Apify;
- every provider row satisfies the conservation invariant;
- repeated/permuted local inputs produce byte-equivalent canonical output;
- no normal test performs network access;
- no unresolved placeholder, generic error instruction, generic test instruction, or
  unstated follow-up remains;
- `PLANS.md` marks M2 complete only after the implementation is actually complete.

Query/payload/identity behavior changes require a spec revision, changed requirement ID
or catalog version where applicable, and tests updated in the same atomic PR. Provider
documentation may clarify wire syntax but cannot override product scope, provenance,
identity conservatism, cost ceilings, or safety.

Provider wire references retained by this contract:

- https://exa.ai/docs/reference/verticals/company-for-coding-agents
- https://exa.ai/docs/reference/error-codes
- https://docs.apify.com/api/v2/actors-runs-post
- https://docs.apify.com/api/v2/actor-run-get
- https://apify.com/compass/crawler-google-places/input-schema

After product-owner approval of this file, create a separate Superpowers implementation
plan. Its tasks are internal TDD execution order only: they MUST culminate in this one
atomic M2 implementation and MUST NOT become separate product sections, PR layers, or
approval gates.
