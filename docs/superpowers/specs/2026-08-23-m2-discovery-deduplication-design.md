# M2 Candidate Intelligence Batch Design Contract

Status: approved for implementation | Revision: 5 | Baseline: `616003f` (merged M1)

M2 is one atomic implementation, PR, live batch, and review gate. Headings are navigation,
not sub-milestones, stack layers, or partial deliveries.

## Outcome and scope

M2 turns bounded public-provider discovery into persisted, evidence-linked company facts on
the M1 state and cost foundation:

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
                         │
                         ▼
       deterministic selection of at most 20 companies
                         │
                         ▼
          bounded Exa evidence research per company
                         │
                         ▼
       DeepSeek schema-constrained fact extraction
                         │
                         ▼
       persisted evidence + typed facts + checkpoint
```

The catalog targets independent/regional PVF distributors visible through RFQ/BOM
language, project markets, and manufacturer line cards—not only obvious sales-database
accounts. M2 collects candidates and provenance but does not qualify them. The default
100 rows are a bounded discovery batch, not 100 promised unique or accepted accounts.

M2 ends at canonical companies with discovery provenance, public evidence, and typed
extracted facts. It also retains unresolved raw records. It does not add:

- M3 deterministic scoring, rejection policy, or accepted/rejected/uncertain decisions;
- the final M3 command surface, ranked/rejected/uncertain CSV views, manual-label
  calibration, or production-scale orchestration;
- people/contact discovery, enrichment waterfalls, email, phone, or outreach;
- Mexico-targeted discovery;
- fuzzy, semantic, embedding, or LLM identity matching;
- a database, frontend, provider SDK, async system, or agent framework;
- automatic live-provider tests or unapproved paid calls.

The critical invariants are:

| ID | Requirement |
| --- | --- |
| INV-01 | The default plan requests at most 100 raw rows and places a `$0.25` aggregate provider-side ceiling on Apify. `$1.00` is an explicit absolute maximum, never the default. |
| INV-02 | No generated request targets Mexico. Returned outside-scope rows are preserved and marked for review rather than hidden. |
| INV-03 | Exa works when Apify is disabled or unavailable. |
| INV-04 | Every attempted provider call is recoverable through persisted usage accounting on success or failure; reported dollar usage remains estimated. |
| INV-05 | Missing provider data remains unknown. Query targets and neighboring records never fabricate returned-company facts. |
| INV-06 | Every raw provider row appears exactly once in a canonical company's provenance or in `unresolved_records`. |
| INV-07 | Valid corporate domains outrank weaker identity keys; two different valid domains never merge through name/location. |
| INV-08 | `source_url` is provenance, not identity, and never substitutes for a missing corporate `website_url`. |
| INV-09 | Deduplication is deterministic, permutation-invariant, and performs no I/O, DNS, HTTP, environment, clock, or random calls. |
| INV-10 | M2 populates evidence, features, and feature confidence only; coverage, scores, decisions, and rejection fields remain at M1 defaults. |
| INV-11 | Every non-null extracted fact cites retained evidence IDs; unsupported facts remain explicit unknowns and never become negative evidence. |
| INV-12 | Provider budgets are independent: Apify retains its `$0.25` default/`$1.00` maximum, DeepSeek requires an explicit per-run cap, and Exa is bounded by request/result limits plus any explicit local ceiling. |
| INV-13 | The target is at most 20 extracted companies. A provider budget wins over that target and produces a durable `paused_budget` checkpoint. |
| INV-14 | Before each paid call the runner persists intent; after success it persists usage and output before the next paid call. Unknown in-flight outcomes are never automatically repeated. |
| INV-15 | M2 acceptance includes a real, explicitly authorized discovery-to-extraction batch that completes at least one company; automated tests and CI make zero provider calls. |

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
| `src/leads_discovery/models.py` | JSON-safe discovery, evidence, extraction, and result contracts |
| `src/leads_discovery/discovery/base.py` | synchronous provider protocol, sanitized provider error, shared raw-ID behavior |
| `src/leads_discovery/discovery/queries.py` | pure deterministic request catalog and allocation |
| `src/leads_discovery/discovery/exa.py` | Exa HTTP translation only |
| `src/leads_discovery/discovery/apify.py` | one bounded Apify Actor lifecycle only |
| `src/leads_discovery/discovery/__init__.py` | supported discovery exports |
| `src/leads_discovery/dedup.py` | pure normalization, identity grouping, and canonical merge |
| `src/leads_discovery/research/evidence.py` | deterministic Exa research requests and bounded evidence bundles |
| `src/leads_discovery/research/extract.py` | one schema-constrained DeepSeek extraction per company |
| `src/leads_discovery/research/__init__.py` | supported research exports |
| `src/leads_discovery/pipeline/m2_batch.py` | narrow resumable M2 batch command and injected orchestration |
| `src/leads_discovery/pipeline/state.py`, `costs.py` | persisted JSONL events and replayable run budgets |
| `tests/test_{queries,exa_discovery,apify_discovery,deduplication}.py` | discovery and identity contract tests |
| `tests/test_{evidence,extraction,m2_batch}.py` | research, extraction, budget, and resume contract tests |
| `pyproject.toml`, `.gitignore`, `PLANS.md` | dependencies, private live artifacts, and M2 completion status |

Do not change production files outside this manifest unless a documented repository
constraint requires it. Every new or changed function/class requires a useful docstring.

Add these public data contracts to `models.py`:

```python
ProviderName = Literal["exa", "apify", "deepseek"]
DiscoveryProviderName = Literal["exa", "apify"]
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
    provider: DiscoveryProviderName
    query_family: str
    target_country_code: CountryCode
    queries: tuple[str, ...]
    max_results_per_query: int
    max_results_total: int
    max_cost_usd: float | None = None


@dataclass(slots=True)
class DiscoveryRecord:
    record_id: str
    provider: DiscoveryProviderName
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


FactValue = bool | int | float | str | list[str] | None


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    request_id: str
    company_id: str
    query_family: str
    query: str
    max_results: int


@dataclass(slots=True)
class EvidenceBundle:
    company_id: str
    items: list[EvidenceItem]
    raw_records: list[dict[str, Any]]
    usage_events: list[UsageEvent]


@dataclass(slots=True)
class ExtractedFact:
    value: FactValue
    confidence: float
    evidence_ids: list[str]


@dataclass(slots=True)
class ExtractionResult:
    company_id: str
    model: str
    facts: dict[str, ExtractedFact]
    usage_event: UsageEvent
```

All new models and the reused `EvidenceItem` round-trip nested JSON-safe data through
`to_dict()`/`from_dict()` and do not retain caller-owned mutable collections. `query` is
optional because Apify may omit `searchString`; never substitute another query.
`target_country_code` records intent, not the returned company's country.

`DiscoveryProvider.search(request) -> DiscoveryBatch` is a synchronous protocol.
Provider constructors receive a nonempty credential and an injected `httpx.Client`;
Apify also receives injectable monotonic-clock, sleep, and optional run-start callback
functions for hermetic persistence tests.
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
    apify_budget_usd: float = 0.25,
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
Exa requests × 7 rows plus U.S. and Canada Apify requests × 15 rows, capped at `$0.125`
per Actor run (`$0.25` aggregate). Small candidate totals use the same allocation rules
rather than special cases. A caller may explicitly supply a larger budget up to the
`$1.00` absolute maximum; adapters never increase it.

## Provider behavior

Common provider behavior:

| ID | Requirement |
| --- | --- |
| PRV-01 | Reject a request for the wrong provider, invalid geography/cardinality, blank queries, totals outside `1..100`, or an invalid/missing Apify cap before HTTP. |
| PRV-02 | Return at most `max_results_total` records in provider order. Preserve the complete JSON result row in `raw_metadata`; optional missing values remain `None`. |
| PRV-03 | Give every successful batch one UTC retrieval timestamp and every row `raw_` plus the first 24 SHA-256 hex characters. Hash provider + request + provider result ID when available; otherwise hash stable parsed/raw identity while excluding retrieval time and credentials. |
| PRV-04 | Emit one aggregate `UsageEvent` per provider operation. It counts all attempted HTTP calls, stores only safe request/run/result metadata, records authenticated dollar usage as estimated, and leaves exact cost unknown. |
| PRV-05 | Adapters make no automatic retries. The M2 runner owns only persisted optional-provider continuation, pause, and explicit resume; broader retry policy remains M3 scope. |
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

After validating the start response, invoke the optional run-start callback with the safe
run ID before the first poll. `resume(request, run_id)` performs no start POST and polls/fetches
only that existing run. These are the only live-run additions beyond the shared search
protocol.

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
| pipeline state | At deduplication, set `status="active"` and `stage_status={"deduplication": "completed"}`; later M2 stages may populate evidence/features, while scores/decisions/rejections remain untouched. |
| timestamps/order | Require timezone-aware source timestamps; invalid values fail deduplication. Use earliest/latest instants in UTC, then sort companies by ID and unresolved rows by deterministic raw order. |

Permuting the same multiset of records must produce byte-equivalent serialized output.
Country conflicts and outside geography are review metadata, never destructive filters.
No M2 output receives a final lead decision.

## Candidate selection and Exa research

`select_research_companies(companies, limit=20)` is pure and accepts `limit` only in
`1..20`. It never changes company status or creates a lead decision. Companies reported
outside `US`/`CA` are retained but not selected for paid research. Remaining companies
sort by valid corporate domain first, known `US`/`CA` country before unknown country,
descending distinct discovery-provider count, descending discovery-record count, then
`company_id`. Selection takes the first `limit`; this is spend prioritization, not a score.

For each selected company, build these three Exa requests in order. `{name}` is the stripped
canonical name and `{domain}` is the corporate domain or an empty string:

| Family | Exact query template |
| --- | --- |
| `company-profile` | `"{name}" {domain} pipe valves fittings products industries locations line card` |
| `quotation-workload` | `"{name}" {domain} RFQ quote quotation BOM estimating project tender inside sales` |
| `economic-incumbent-pain` | `"{name}" {domain} employees branches revenue automation ERP ecommerce quote software competitor manual workflow` |

Each request uses `POST https://api.exa.ai/search`, `type="auto"`, `numResults=5`, and
`contents={"highlights": True}`. Do not use Exa deep search, Answer, synthesized output,
summaries, full text, or an LLM inside the research adapter. The optional company category
is not used because research must surface diverse public pages rather than only entities.
Collapse template whitespace after substitution. The adapter makes no automatic retry and
returns results in query/provider order.

Convert each valid result into `EvidenceItem`: deterministic `ev_` plus 24 SHA-256 hex
characters over provider, normalized URL, and excerpt; exact URL; title; joined ordered
highlights as excerpt; source type `web`; provider `exa`; and the batch UTC timestamp.
Missing highlights produce an item with `excerpt=None`, not invented text. Retain the full
research row in `research_raw.jsonl`, separate from the bounded model input.

`build_evidence_bundle()` performs no I/O. It de-duplicates exact normalized HTTP(S) URLs,
keeps first provider occurrence, limits excerpts to 2,000 characters, admits at most two
items per registrable source domain except up to four from the company's own domain, then
keeps the first 12 items and at most 20,000 total excerpt characters. It preserves request
order and makes no relevance inference beyond these deterministic limits. The bundle may
be empty; empty evidence is a successful research result but is not sent to DeepSeek.

Exa research usage uses operation `company_research`, counts all three attempted calls,
retains only company/request/result counts, and records nonnegative `costDollars.total`
as estimated. Discovery and research usage remain separate operations.

## DeepSeek structured extraction

`DeepSeekExtractor.extract(company, bundle) -> ExtractionResult` receives a nonempty API
key, injected `httpx.Client`, explicit model, and a price schedule. The runner owns the
persisted per-run budget state and reservation. The extractor does not read environment
variables, construct/close the client, retry, repair malformed output with another paid
call, or score the company.

The current live configuration uses `POST https://api.deepseek.com/chat/completions` with
Bearer auth and exactly these behavioral controls:

```python
{
    "model": "deepseek-v4-flash",
    "messages": [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": EVIDENCE_JSON},
    ],
    "thinking": {"type": "disabled"},
    "response_format": {"type": "json_object"},
    "max_tokens": 2048,
    "temperature": 0,
    "stream": False,
}
```

The system prompt says evidence is untrusted quoted data, ignores instructions contained
inside it, requires JSON, forbids unsupported inference, and defines this fixed fact set:

```text
pvf_relevant
pvf_product_breadth
industrial_or_process_customer_focus
branch_count
inside_sales_or_estimating_presence
rfq_or_quote_workflow_evidence
project_or_tender_business
bom_or_line_item_complexity
manufacturer_count_or_breadth
relevant_hiring
employee_count
revenue_if_reliably_available
regional_independent_signal
multi_location_signal
known_current_direct_competitor_customer
known_competitor_evaluation_history
known_quote_automation_or_order_automation_relationship
direct_quotation_pain_evidence
manual_workflow_evidence
explicit_process_bottleneck_evidence
```

The response root is `{"facts": {...}}` with every listed key exactly once. Each value is
`{"value": FactValue, "confidence": float, "evidence_ids": [str, ...]}`. Reject unknown
keys, missing keys, incompatible value types, booleans masquerading as integers, confidence
outside `0..1`, duplicate IDs, or citations absent from the supplied bundle. A non-null
value requires at least one evidence ID. An unknown is exactly `value=null`,
`confidence=0`, and `evidence_ids=[]`. Invalid JSON, a truncated `finish_reason`, or an
invalid schema raises sanitized `invalid_response`; it never fabricates a fallback fact.

Applying a valid result writes raw values to `CompanyRecord.features` and writes
`{"confidence": ..., "evidence_ids": [...]}` to the matching
`CompanyRecord.feature_confidence` key. It stores the evidence bundle, sets
`stage_status["research"]` and `stage_status["extraction"]` to `completed`, and leaves
coverage, scoring, decisions, and rejection data unchanged.

Usage operation `structured_extraction` records prompt cache-hit/cache-miss, completion,
and total tokens from the authenticated response. Estimated cost uses the configured
price schedule. Revision 5's live default for `deepseek-v4-flash`, verified against the
official pricing page on 2026-08-23, is `$0.0028`/million cache-hit input tokens,
`$0.14`/million cache-miss input tokens, and `$0.28`/million output tokens. Prices are
explicit configuration so later credit or price changes require no code change.

Before a call, reserve a conservative maximum using all prompt characters as possible
input tokens at the cache-miss rate plus `max_tokens` at the output rate. Do not start the
call when persisted actual spend plus reservations would exceed the explicit per-run
DeepSeek ceiling. Reconcile the reservation to authenticated usage afterward. The current
acceptance run authorizes at most `$1.00`; automated tests authorize zero. A later live run
may explicitly authorize a different ceiling without changing production code.

## Resumable live M2 batch

`python -m leads_discovery.pipeline.m2_batch` is a narrow M2 command, not the final M3
CLI. It accepts a required `--run-id`, an explicit `--deepseek-budget-usd`, discovery and
extraction caps, and optional Apify enablement/budget. Paid execution additionally requires
`--execute-live`. The command boundary alone reads `EXA_API_KEY`, `DEEPSEEK_API_KEY`, and
optional `APIFY_TOKEN`; adapters and domain functions never read the environment.

Defaults and validation are:

| Control | Default / rule |
| --- | --- |
| discovery candidates | `100`, valid `1..100` |
| extracted companies | `20`, valid `1..20` |
| Apify | optional; disabled without explicit enablement and token |
| Apify budget | `$0.25` aggregate default; explicit `0..1`; zero disables it |
| DeepSeek budget | no paid default; positive explicit value required for live extraction |
| Exa local dollar ceiling | optional; request/result ceilings always apply |

There is no combined provider budget. The account's `$5` Apify credit does not authorize
spending `$5` in one run, and the current `$1` DeepSeek balance does not become a permanent
hard-coded script limit. Each provider is stopped independently. Optional Apify exhaustion
is persisted and Exa continues. Required Exa or DeepSeek exhaustion produces
`paused_budget`; completed companies remain usable. Budget wins over the 20-company target.

Each run writes only beneath `data/<run-id>/`:

```text
companies_raw.jsonl
companies_deduped.jsonl
research_raw.jsonl
companies_extracted.jsonl
usage_events.jsonl
usage.json
checkpoint.json
```

Run IDs accept only `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` and cannot escape the data root.
Live artifacts and credentials are gitignored and never committed. `usage.json` is rebuilt
from the append-only usage ledger. The runner replays that ledger on resume, so restarting
cannot reset a budget.

Before any paid HTTP call, atomically record provider, safe operation ID, company/request,
and state `in_flight`. After the response, append and fsync usage, persist the complete raw
result, persist the company snapshot when applicable, then atomically mark the operation
`completed` before another paid call. A process death with an `in_flight` Exa or DeepSeek
operation yields `paused_unknown`; resume does not repeat it without an explicit operator
override. For Apify, persist the returned Actor run ID immediately and resume polling that
same run; never start a replacement automatically.

Provider errors retain completed work. Authentication/invalid configuration exits failed;
budget exhaustion pauses; retryable errors remain pending with sanitized metadata. M2 does
not add automatic cross-run retry policy. The default live batch attempts extraction in
selection order until 20 complete, evidence is empty, or a required budget/provider pauses.
At least one real company must finish discovery, research, and extraction for M2 live
acceptance; a zero-output call sequence is diagnostic evidence, not acceptance.

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
| selection/research | exact priority/order/query catalog, 20-company cap, raw preservation, URL/source bounds, empty evidence, and Exa research usage |
| extraction | exact DeepSeek payload/model, prompt-injection boundary, complete schema, unknown/citation validation, token cost, reservation, and sanitized failures |
| batch/resume | fake-provider end-to-end flow, path validation, per-call persistence order, usage replay, independent budgets, optional Apify, `paused_budget`, and unknown in-flight calls |
| compatibility/safety | nested model round-trips, unchanged M1 behavior/defaults, zero network in automated tests, no credential leakage, and no committed live artifacts |

Tests use `httpx.MockTransport`, injected clock/sleep, deterministic fake providers, and
the bundled public-suffix snapshot. Normal tests and CI never call Exa, Apify, DeepSeek,
DNS, or public-suffix endpoints.

Before the real batch, approval-gated wire smokes use real credentials: Exa one result,
DeepSeek one minimal bounded extraction, and optional Apify one item with
`maxTotalChargeUsd <= $0.05`. If the Actor rejects that cap as below its current minimum,
skip Apify rather than increase it. Smokes are diagnostics, not acceptance by themselves.

The product owner has authorized one real M2 discovery-to-extraction acceptance batch.
It uses the same production command and persisted state as later runs, an explicit
DeepSeek ceiling no greater than the current `$1.00` balance, the normal `$0.25` Apify
default when Apify is enabled, and the fixed request/result caps above. Record only
sanitized counts, estimated provider costs, final checkpoint status, and artifact paths
in the PR; do not commit live rows.

Run the full repository gate:

```bash
ruff check .
mypy src tests
pytest
python -m build
```

M2 is complete only when the whole manifest is implemented in one PR, all automated
behavioral evidence and repository gates pass, Exa operates without Apify, independent
spend/result ceilings survive resume, every discovery row satisfies conservation, every
non-null fact cites retained evidence, and the authorized live batch completes at least one
company end to end. `PLANS.md` then marks M2 complete. The PR body includes the system DAG
above plus actual offline and sanitized live results. There is no frontend, so no
screenshot is required.

## Approval and change control

Provider documentation may clarify wire syntax but cannot override geography,
provenance, identity conservatism, disabled enrichments, or result/spend ceilings.
Changes to query text/order, payload scope, model, fact schema, evidence bounds, cost
limits, public models, or identity behavior require this contract and matching tests to
change together. Provider documentation may update wire syntax or prices, but live code
must not silently select a new model, broaden data collection, or raise a budget.

Wire references retained from the approved design direction:

- https://exa.ai/docs/reference/verticals/company-for-coding-agents
- https://exa.ai/docs/reference/search
- https://exa.ai/docs/reference/error-codes
- https://docs.apify.com/api/v2/actors-runs-post
- https://docs.apify.com/api/v2/actor-run-get
- https://apify.com/compass/crawler-google-places/input-schema
- https://api-docs.deepseek.com/api/create-chat-completion
- https://api-docs.deepseek.com/quick_start/pricing

The Superpowers implementation plan and agent prompts are internal execution order only.
They culminate in this one atomic M2 pull request and do not become product sections,
stack layers, or separate approval gates.
