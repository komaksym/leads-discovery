# M2 Candidate Intelligence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to execute this plan. For this milestone, the product owner
> explicitly assigns production, independent contract tests, and red-team review to separate
> agents; do not collapse those roles or expose production code to the test author before the
> first test commit.

**Goal:** Implement and execute one bounded, resumable U.S./Canada company-intelligence
batch from discovery through evidence-linked DeepSeek facts.

**Architecture:** Thin synchronous HTTP adapters translate Exa, Apify, and DeepSeek while
pure functions own planning, normalization, deduplication, selection, and evidence bounds.
A narrow M2 runner persists intent, raw output, usage, company snapshots, and checkpoints
around every paid call. M2 produces facts, not scores or lead decisions.

**Tech Stack:** Python 3.12+, standard-library dataclasses/argparse, `httpx`, `tldextract`,
JSON/JSONL state, Ruff, mypy, pytest, build.

**Spec:** `docs/superpowers/specs/2026-08-23-m2-discovery-deduplication-design.md`

## Global Constraints

- M2 is one implementation and one PR; tasks below are internal execution order only.
- Automated tests and CI make no network, DNS, Exa, Apify, or DeepSeek calls and spend `$0`.
- Discovery requests total at most 100 and never target Mexico.
- Apify is optional; default aggregate cap `$0.25`, explicit maximum `$1.00`.
- DeepSeek paid execution has no implicit default; the current acceptance run explicitly
  authorizes no more than the available `$1.00` balance.
- Exa is request/result bounded and may also receive an explicit local dollar ceiling.
- Budget wins over the target of at most 20 extracted companies.
- Unknown data remains unknown; every non-null fact cites retained evidence.
- Every paid operation is durably marked before the call and persisted before the next call.
- No credential, authentication header, body, full provider response, or unsafe exception
  chain appears in logs, errors, commits, or PR text.
- Every new or changed function/class has a useful docstring.
- Preserve existing M1 imports, serialized keys, defaults, and passing behavior.

## Execution topology

```text
production agent ─┐
                  ├── one integration branch/PR ── serial red-team agent
test agent ───────┘
```

Production and test agents branch from the same immutable planning commit, work in separate
worktrees, and do not inspect one another. The test branch may fail collection because M2
modules do not exist at its base; it must still pass syntax and lint. After both commits are
integrated, adjudicate failures against the spec. Production defects return to the production
agent with the failing test retained; test defects return to the test agent with a cited
contract clause; true ambiguity stops for product-owner direction.

## Frozen public API

The two independent branches use these names. Do not rename them without changing the spec,
plan, and both branches together.

```python
# leads_discovery.discovery.base
class DiscoveryProvider(Protocol):
    def search(self, request: DiscoveryRequest) -> DiscoveryBatch: ...

class DiscoveryProviderError(RuntimeError): ...

# leads_discovery.discovery.queries
def build_discovery_requests(
    *, include_apify: bool, max_candidates: int = 100,
    apify_budget_usd: float = 0.25,
) -> tuple[DiscoveryRequest, ...]: ...

# leads_discovery.discovery.exa / apify
class ExaDiscoveryProvider:
    def __init__(self, *, api_key: str, client: httpx.Client) -> None: ...

class ApifyDiscoveryProvider:
    def __init__(
        self, *, api_token: str, client: httpx.Client,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_run_started: Callable[[str], None] | None = None,
    ) -> None: ...
    def resume(self, request: DiscoveryRequest, run_id: str) -> DiscoveryBatch: ...

# leads_discovery.dedup
def normalize_website_domain(url: str | None) -> str | None: ...
def normalize_company_name(name: str | None) -> str | None: ...
def deduplicate(records: Iterable[DiscoveryRecord]) -> DeduplicationResult: ...

# leads_discovery.research.evidence
def select_research_companies(
    companies: Iterable[CompanyRecord], *, limit: int = 20,
) -> tuple[CompanyRecord, ...]: ...
def build_research_requests(company: CompanyRecord) -> tuple[ResearchRequest, ...]: ...
def build_evidence_bundle(
    *, company: CompanyRecord, items: Iterable[EvidenceItem],
    raw_records: Iterable[dict[str, Any]],
    usage_events: Iterable[UsageEvent],
) -> EvidenceBundle: ...
class ExaEvidenceResearcher:
    def __init__(self, *, api_key: str, client: httpx.Client) -> None: ...
    def research(self, company: CompanyRecord) -> EvidenceBundle: ...

# leads_discovery.research.extract
@dataclass(frozen=True, slots=True)
class DeepSeekPriceSchedule:
    cache_hit_input_per_million: float
    cache_miss_input_per_million: float
    output_per_million: float

class DeepSeekExtractor:
    def __init__(
        self, *, api_key: str, client: httpx.Client, model: str,
        prices: DeepSeekPriceSchedule,
    ) -> None: ...
    def extract(self, company: CompanyRecord, bundle: EvidenceBundle) -> ExtractionResult: ...

def apply_extraction(
    company: CompanyRecord, bundle: EvidenceBundle, result: ExtractionResult,
) -> CompanyRecord: ...

# leads_discovery.pipeline.m2_batch
@dataclass(frozen=True, slots=True)
class M2BatchConfig:
    run_id: str
    data_root: Path
    max_candidates: int = 100
    max_extracted: int = 20
    include_apify: bool = False
    apify_budget_usd: float = 0.25
    deepseek_budget_usd: float | None = None
    exa_budget_usd: float | None = None
    execute_live: bool = False

def run_m2_batch(
    config: M2BatchConfig, *, discovery: Mapping[str, DiscoveryProvider],
    researcher: ExaEvidenceResearcher, extractor: DeepSeekExtractor,
) -> RunCheckpoint: ...

def main(argv: Sequence[str] | None = None) -> int: ...
```

## File map

- Modify `pyproject.toml`: add only `httpx>=0.27,<1` and `tldextract>=5.3,<6`.
- Modify `.gitignore`: ignore `data/` and live run artifacts.
- Modify `src/leads_discovery/models.py`: all discovery/evidence/extraction contracts and
  defensive JSON round-trips.
- Create `src/leads_discovery/discovery/{__init__,base,queries,exa,apify}.py`.
- Create `src/leads_discovery/dedup.py`.
- Create `src/leads_discovery/research/{__init__,evidence,extract}.py`.
- Modify `src/leads_discovery/pipeline/{state,costs}.py`: generic fsynced JSONL usage/event
  persistence and replayable budget accounting.
- Create `src/leads_discovery/pipeline/m2_batch.py`: injected orchestration and narrow CLI.
- Create only the seven test files named in the spec.
- Modify `PLANS.md` only after integrated automated and live acceptance gates pass.

---

### Task 1: Contracts and dependencies

**Files:** `pyproject.toml`, `.gitignore`, `src/leads_discovery/models.py`

**Produces:** The frozen dataclasses from the spec, including defensive nested
`to_dict()`/`from_dict()` behavior and unchanged M1 defaults.

- [ ] Add the two exact runtime dependency ranges and no provider SDK.
- [ ] Add all model types with exact field names/types from the spec.
- [ ] Make constructors/from-dict paths copy caller-owned lists/dictionaries recursively.
- [ ] Verify `CompanyRecord.from_dict(CompanyRecord.to_dict())` retains M1 and new nested data.
- [ ] Run `ruff check src/leads_discovery/models.py` and `mypy src`.
- [ ] Commit with subject `feat(m2): add intelligence contracts` and an explanatory body.

### Task 2: Discovery and provider safety

**Files:** `src/leads_discovery/discovery/*.py`, `tests/test_queries.py`,
`tests/test_exa_discovery.py`, `tests/test_apify_discovery.py`

**Produces:** The frozen discovery API, exact query plan, Exa company adapter, and one-run
Apify adapter.

- [ ] Implement QRY-01 through QRY-05 exactly, including stable quotient/remainder allocation.
- [ ] Implement shared validation, raw IDs, safe usage, and sanitized failure classification.
- [ ] Translate the exact Exa company-search payload and cap ordered output.
- [ ] Start/poll/fetch only one capped Apify run with every enrichment disabled.
- [ ] Persist/expose an Apify run ID so a resumed operation polls the same run.
- [ ] Run the three focused test files after integration, then lint and mypy.
- [ ] Commit with subject `feat(m2): add bounded discovery` and an explanatory body.

### Task 3: Conservative identity resolution

**Files:** `src/leads_discovery/dedup.py`, `tests/test_deduplication.py`

**Produces:** Offline normalization and `deduplicate()` satisfying DED-01 through DED-08.

- [ ] Instantiate one offline `tldextract.TLDExtract` with the exact spec configuration.
- [ ] Implement URL, company-name, location, region, and reported-country normalization.
- [ ] Implement domain-first grouping and only the complete fallback merge table.
- [ ] Build deterministic canonical values, review codes, unique IDs, provenance, and ordering.
- [ ] Prove serialized permutation invariance and raw-row conservation after integration.
- [ ] Run the focused test file, lint, and mypy.
- [ ] Commit with subject `feat(m2): resolve company identity` and an explanatory body.

### Task 4: Exa evidence research

**Files:** `src/leads_discovery/research/__init__.py`,
`src/leads_discovery/research/evidence.py`, `tests/test_evidence.py`

**Produces:** Deterministic selection, the exact three-query catalog, Exa research parsing,
bounded evidence, raw-row preservation, and research usage.

- [ ] Implement the exact selection key and `1..20` validation.
- [ ] Build the three exact queries and collapse substitution whitespace.
- [ ] Send three bounded `type=auto` searches with highlights and no synthesis/deep mode.
- [ ] Create stable evidence IDs and preserve complete rows outside the bounded prompt bundle.
- [ ] Apply URL/domain/source/excerpt/item/character limits in pure code.
- [ ] Run the focused test file after integration, lint, and mypy.
- [ ] Commit with subject `feat(m2): collect bounded evidence` and an explanatory body.

### Task 5: DeepSeek extraction and budget reservation

**Files:** `src/leads_discovery/research/extract.py`, `tests/test_extraction.py`

**Produces:** Exact non-thinking JSON request, strict fact parsing, evidence-link validation,
usage/cost calculation, and pure application to `CompanyRecord`.

- [ ] Define the complete fixed fact schema and prompt that treats evidence as untrusted data.
- [ ] Translate the exact `deepseek-v4-flash` request with `max_tokens=2048`.
- [ ] Validate every key, value type, confidence, citation, and unknown representation.
- [ ] Parse authenticated token counters and estimate cost from the injected schedule.
- [ ] Reserve worst-case cache-miss input/output cost before calls at the runner boundary.
- [ ] Apply valid facts without changing coverage, scores, decisions, or rejection fields.
- [ ] Run the focused test file after integration, lint, and mypy.
- [ ] Commit with subject `feat(m2): extract evidence facts` and an explanatory body.

### Task 6: Durable M2 runner

**Files:** `src/leads_discovery/pipeline/state.py`,
`src/leads_discovery/pipeline/costs.py`, `src/leads_discovery/pipeline/m2_batch.py`,
`tests/test_m2_batch.py`

**Produces:** A path-safe, dependency-injected, resumable batch with independent budgets and
the seven exact artifacts in the spec.

- [ ] Add generic append-and-fsync JSONL helpers and strict event deserialization.
- [ ] Replay usage events and reservations so resume cannot reset spend.
- [ ] Validate run IDs and resolve all artifacts beneath the configured data root.
- [ ] Execute discovery, deduplication, selection, research, and extraction in fixed order.
- [ ] Persist `in_flight`, usage, raw output, company snapshot, and `completed` in exact order.
- [ ] Implement `paused_budget`, `paused_unknown`, optional-Apify continuation, and no-repeat
  semantics; never auto-repeat an unknown Exa/DeepSeek outcome.
- [ ] Add the narrow argparse/environment command with explicit `--execute-live`.
- [ ] Run the fake-provider end-to-end/resume tests, then the full repository gate.
- [ ] Commit with subject `feat(m2): run resumable batch` and an explanatory body.

### Task 7: Integration, adversarial closure, and live acceptance

**Files:** all M2 files, `PLANS.md`, PR body; no committed `data/` rows.

**Produces:** One green M2 PR and sanitized evidence from a real batch.

- [ ] Integrate production and independent test commits without rewriting tests to match code.
- [ ] Run `ruff check .`, `mypy src tests`, `python -m pytest`, and `python -m build`.
- [ ] Run the serial adversary; retain every valid regression test and resolve P0–P2 findings.
- [ ] Re-run the full gate from a clean worktree.
- [ ] With explicit credentials, run one-result/item/minimal-extraction wire smokes; keep Apify
  optional and never raise a rejected `$0.05` smoke cap.
- [ ] Execute the production M2 command with an explicit DeepSeek ceiling no greater than the
  current `$1.00` balance and normal `$0.25` Apify default when enabled.
- [ ] Require at least one completed real extraction; continue toward 20 while provider budgets
  permit, otherwise verify a durable `paused_budget` checkpoint.
- [ ] Confirm `git status --short` contains no secret or live artifact.
- [ ] Mark M2 complete in `PLANS.md`, commit, and open one PR with the system DAG, actual gates,
  sanitized counts/costs/status, risks, and no frontend screenshot.

## Completion audit

- Every INV/QRY/PRV/DED rule maps to production behavior and an independent test.
- Tests kill mutations that raise budgets, enable enrichment, merge different domains, treat
  `source_url` as identity, infer country, accept uncited facts, repeat in-flight calls, or
  reset spend on resume.
- Automated spend is `$0`; live spend is reported per provider, never pooled.
- M2 completes real discovery, research, and extraction for at least one company.
- All original scoring/decision/full-CLI/output/calibration work remains explicitly in M3.
