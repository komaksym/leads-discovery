"""Canonical persisted models for the leads-discovery pipeline."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

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
FactValue = bool | int | float | str | list[str] | None
DecisionKind = Literal["review", "rejection"]


def _utc_now() -> str:
    """Return an ISO-8601 UTC timestamp suitable for persisted records."""
    return datetime.now(UTC).isoformat()


def _copy_dict(value: dict[str, Any]) -> dict[str, Any]:
    """Return a recursive copy of a JSON-like dictionary."""
    return deepcopy(value)


@dataclass(slots=True)
class EvidenceItem:
    """Represent one public evidence item retained for later feature extraction."""

    evidence_id: str
    url: str
    title: str | None = None
    excerpt: str | None = None
    source_type: str | None = None
    provider: str | None = None
    retrieved_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        """Convert this evidence item to a defensive JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EvidenceItem:
        """Rebuild an evidence item from persisted JSON-safe data."""
        return cls(**deepcopy(payload))


@dataclass(slots=True)
class DecisionReason:
    """Explain one review or rejection decision with retained citations."""

    code: str
    kind: DecisionKind
    explanation: str
    confidence: float | None = None
    evidence_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Detach retained evidence identifiers from caller-owned collections."""
        self.evidence_ids = deepcopy(self.evidence_ids)

    def to_dict(self) -> dict[str, Any]:
        """Convert the decision reason to a defensive JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DecisionReason:
        """Rebuild one decision reason without aliasing persisted nested values."""
        data = deepcopy(payload)
        return cls(
            code=str(data["code"]),
            kind=cast(DecisionKind, data["kind"]),
            explanation=str(data["explanation"]),
            confidence=(
                None if data.get("confidence") is None else float(data["confidence"])
            ),
            evidence_ids=[str(item) for item in data.get("evidence_ids", [])],
        )


@dataclass(slots=True)
class CompanyRecord:
    """Store one canonical company plus provenance, evidence, and pipeline state."""

    company_id: str
    name: str
    normalized_name: str | None = None
    domain: str | None = None
    normalized_domain: str | None = None
    country: str | None = None
    locations_if_known: list[str] = field(default_factory=list)
    status: str = "active"
    discovery_sources: list[str] = field(default_factory=list)
    discovery_queries: list[str] = field(default_factory=list)
    discovery_records: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    features: dict[str, Any] = field(default_factory=dict)
    feature_confidence: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, float] = field(default_factory=dict)
    score_components: dict[str, float] = field(default_factory=dict)
    final_score: float | None = None
    final_decision: str | None = None
    review_reasons: list[str] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    decision_reasons: list[DecisionReason] = field(default_factory=list)
    evaluation_policy_version: str | None = None
    stage_status: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        """Detach all mutable collection fields from caller-owned values."""
        self.locations_if_known = deepcopy(self.locations_if_known)
        self.discovery_sources = deepcopy(self.discovery_sources)
        self.discovery_queries = deepcopy(self.discovery_queries)
        self.discovery_records = deepcopy(self.discovery_records)
        self.evidence = [EvidenceItem.from_dict(item.to_dict()) for item in self.evidence]
        self.features = deepcopy(self.features)
        self.feature_confidence = deepcopy(self.feature_confidence)
        self.coverage = deepcopy(self.coverage)
        self.score_components = deepcopy(self.score_components)
        self.review_reasons = deepcopy(self.review_reasons)
        self.rejection_reasons = deepcopy(self.rejection_reasons)
        self.decision_reasons = [
            DecisionReason.from_dict(reason.to_dict()) for reason in self.decision_reasons
        ]
        self.stage_status = deepcopy(self.stage_status)

    def to_dict(self) -> dict[str, Any]:
        """Convert the complete company record to a defensive JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CompanyRecord:
        """Rebuild a company record from a persisted dictionary without aliasing input data."""
        data = deepcopy(payload)
        raw_evidence = data.get("evidence", [])
        data["evidence"] = [
            item if isinstance(item, EvidenceItem) else EvidenceItem.from_dict(item)
            for item in raw_evidence
        ]
        raw_reasons = data.get("decision_reasons", [])
        data["decision_reasons"] = [
            item if isinstance(item, DecisionReason) else DecisionReason.from_dict(item)
            for item in raw_reasons
        ]
        return cls(**data)


@dataclass(slots=True)
class UsageEvent:
    """Represent one provider usage event for cost and quota accounting."""

    provider: str
    operation: str
    request_count: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = None
    exact_cost_usd: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    recorded_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        """Detach usage metadata from caller-owned nested dictionaries."""
        self.metadata = _copy_dict(self.metadata)

    def to_dict(self) -> dict[str, Any]:
        """Convert this usage event to a defensive JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> UsageEvent:
        """Rebuild a usage event from persisted JSON-safe data."""
        return cls(**deepcopy(payload))


@dataclass(slots=True)
class RunCheckpoint:
    """Capture resumable run-level state and any current pause reason."""

    run_id: str
    status: str = "running"
    pending_company_id: str | None = None
    pending_stage: str | None = None
    pause_reason: str | None = None
    provider_state: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        """Detach persisted provider state from caller-owned nested data."""
        self.provider_state = _copy_dict(self.provider_state)

    def to_dict(self) -> dict[str, Any]:
        """Convert this checkpoint to a defensive JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RunCheckpoint:
        """Rebuild a checkpoint from a persisted dictionary without aliasing input data."""
        return cls(**deepcopy(payload))


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    """Describe one bounded discovery operation against a supported provider."""

    request_id: str
    provider: DiscoveryProviderName
    query_family: str
    target_country_code: CountryCode
    queries: tuple[str, ...]
    max_results_per_query: int
    max_results_total: int
    max_cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert this request to a JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DiscoveryRequest:
        """Rebuild a discovery request while freezing its query collection."""
        data = deepcopy(payload)
        return cls(
            request_id=str(data["request_id"]),
            provider=cast(DiscoveryProviderName, data["provider"]),
            query_family=str(data["query_family"]),
            target_country_code=cast(CountryCode, data["target_country_code"]),
            queries=tuple(str(query) for query in data["queries"]),
            max_results_per_query=int(data["max_results_per_query"]),
            max_results_total=int(data["max_results_total"]),
            max_cost_usd=(
                None
                if data.get("max_cost_usd") is None
                else float(data["max_cost_usd"])
            ),
        )


@dataclass(slots=True)
class DiscoveryRecord:
    """Represent one raw provider discovery row with request provenance."""

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

    def __post_init__(self) -> None:
        """Detach raw provider metadata from caller-owned nested data."""
        self.raw_metadata = _copy_dict(self.raw_metadata)

    def to_dict(self) -> dict[str, Any]:
        """Convert this discovery row to a defensive JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DiscoveryRecord:
        """Rebuild a discovery row from persisted JSON-safe data."""
        data = deepcopy(payload)
        return cls(
            record_id=str(data["record_id"]),
            provider=cast(DiscoveryProviderName, data["provider"]),
            request_id=str(data["request_id"]),
            target_country_code=cast(CountryCode, data["target_country_code"]),
            query=cast(str | None, data.get("query")),
            provider_result_id=cast(str | None, data.get("provider_result_id")),
            name=cast(str | None, data.get("name")),
            source_url=cast(str | None, data.get("source_url")),
            website_url=cast(str | None, data.get("website_url")),
            city=cast(str | None, data.get("city")),
            region=cast(str | None, data.get("region")),
            postal_code=cast(str | None, data.get("postal_code")),
            country_code=cast(str | None, data.get("country_code")),
            title=cast(str | None, data.get("title")),
            snippet=cast(str | None, data.get("snippet")),
            raw_metadata=cast(dict[str, Any], data.get("raw_metadata", {})),
            retrieved_at=str(data["retrieved_at"]),
        )


@dataclass(slots=True)
class DiscoveryBatch:
    """Bundle one discovery request, its rows, and aggregate provider usage."""

    request: DiscoveryRequest
    records: list[DiscoveryRecord]
    usage_events: list[UsageEvent]

    def __post_init__(self) -> None:
        """Detach nested request output collections from caller-owned values."""
        self.request = DiscoveryRequest.from_dict(self.request.to_dict())
        self.records = [DiscoveryRecord.from_dict(record.to_dict()) for record in self.records]
        self.usage_events = [UsageEvent.from_dict(event.to_dict()) for event in self.usage_events]

    def to_dict(self) -> dict[str, Any]:
        """Convert this discovery batch to a defensive JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DiscoveryBatch:
        """Rebuild a discovery batch from persisted JSON-safe data."""
        data = deepcopy(payload)
        return cls(
            request=DiscoveryRequest.from_dict(cast(dict[str, Any], data["request"])),
            records=[DiscoveryRecord.from_dict(item) for item in data.get("records", [])],
            usage_events=[UsageEvent.from_dict(item) for item in data.get("usage_events", [])],
        )


@dataclass(slots=True)
class DeduplicationResult:
    """Return canonical companies plus raw rows that cannot form an identity."""

    companies: list[CompanyRecord]
    unresolved_records: list[DiscoveryRecord]

    def __post_init__(self) -> None:
        """Detach canonical and unresolved collections from caller-owned values."""
        self.companies = [CompanyRecord.from_dict(company.to_dict()) for company in self.companies]
        self.unresolved_records = [
            DiscoveryRecord.from_dict(record.to_dict()) for record in self.unresolved_records
        ]

    def to_dict(self) -> dict[str, Any]:
        """Convert this result to a defensive JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DeduplicationResult:
        """Rebuild a deduplication result from persisted JSON-safe data."""
        data = deepcopy(payload)
        return cls(
            companies=[CompanyRecord.from_dict(item) for item in data.get("companies", [])],
            unresolved_records=[
                DiscoveryRecord.from_dict(item) for item in data.get("unresolved_records", [])
            ],
        )


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    """Describe one bounded Exa evidence-research query for a company."""

    request_id: str
    company_id: str
    query_family: str
    query: str
    max_results: int

    def to_dict(self) -> dict[str, Any]:
        """Convert this research request to a JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ResearchRequest:
        """Rebuild a research request from persisted JSON-safe data."""
        return cls(**deepcopy(payload))


@dataclass(slots=True)
class EvidenceBundle:
    """Hold bounded prompt evidence while retaining complete raw research rows separately."""

    company_id: str
    items: list[EvidenceItem]
    raw_records: list[dict[str, Any]]
    usage_events: list[UsageEvent]

    def __post_init__(self) -> None:
        """Detach all evidence collections from caller-owned mutable values."""
        self.items = [EvidenceItem.from_dict(item.to_dict()) for item in self.items]
        self.raw_records = deepcopy(self.raw_records)
        self.usage_events = [UsageEvent.from_dict(event.to_dict()) for event in self.usage_events]

    def to_dict(self) -> dict[str, Any]:
        """Convert this bundle to a defensive JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EvidenceBundle:
        """Rebuild an evidence bundle from persisted JSON-safe data."""
        data = deepcopy(payload)
        return cls(
            company_id=str(data["company_id"]),
            items=[EvidenceItem.from_dict(item) for item in data.get("items", [])],
            raw_records=cast(list[dict[str, Any]], data.get("raw_records", [])),
            usage_events=[UsageEvent.from_dict(item) for item in data.get("usage_events", [])],
        )


@dataclass(slots=True)
class ExtractedFact:
    """Represent one extracted fact with confidence and retained evidence citations."""

    value: FactValue
    confidence: float
    evidence_ids: list[str]

    def __post_init__(self) -> None:
        """Detach list-valued facts and citations from caller-owned collections."""
        if isinstance(self.value, list):
            self.value = deepcopy(self.value)
        self.evidence_ids = deepcopy(self.evidence_ids)

    def to_dict(self) -> dict[str, Any]:
        """Convert this extracted fact to a defensive JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ExtractedFact:
        """Rebuild an extracted fact from persisted JSON-safe data."""
        data = deepcopy(payload)
        return cls(
            value=cast(FactValue, data.get("value")),
            confidence=float(data["confidence"]),
            evidence_ids=[str(item) for item in data.get("evidence_ids", [])],
        )


@dataclass(slots=True)
class ExtractionResult:
    """Hold one model extraction result and its authenticated usage accounting."""

    company_id: str
    model: str
    facts: dict[str, ExtractedFact]
    usage_event: UsageEvent

    def __post_init__(self) -> None:
        """Detach nested extracted facts and usage from caller-owned values."""
        self.facts = {
            key: ExtractedFact.from_dict(value.to_dict()) for key, value in self.facts.items()
        }
        self.usage_event = UsageEvent.from_dict(self.usage_event.to_dict())

    def to_dict(self) -> dict[str, Any]:
        """Convert this extraction result to a defensive JSON-safe dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ExtractionResult:
        """Rebuild an extraction result from persisted JSON-safe data."""
        data = deepcopy(payload)
        raw_facts = cast(dict[str, dict[str, Any]], data.get("facts", {}))
        return cls(
            company_id=str(data["company_id"]),
            model=str(data["model"]),
            facts={key: ExtractedFact.from_dict(value) for key, value in raw_facts.items()},
            usage_event=UsageEvent.from_dict(cast(dict[str, Any], data["usage_event"])),
        )
