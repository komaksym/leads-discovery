"""Canonical persisted models for the leads-discovery pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def _utc_now() -> str:
    """Return an ISO-8601 UTC timestamp suitable for persisted records."""
    return datetime.now(UTC).isoformat()


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
        """Convert this evidence item to a JSON-serializable dictionary."""
        return asdict(self)


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
    stage_status: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        """Convert the complete company record to a JSON-serializable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CompanyRecord:
        """Rebuild a company record from a persisted dictionary."""
        data = dict(payload)
        raw_evidence = data.get("evidence", [])
        data["evidence"] = [
            item if isinstance(item, EvidenceItem) else EvidenceItem(**item)
            for item in raw_evidence
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
    estimated_cost_usd: float = 0.0
    exact_cost_usd: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    recorded_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        """Convert this usage event to a JSON-serializable dictionary."""
        return asdict(self)


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

    def to_dict(self) -> dict[str, Any]:
        """Convert this checkpoint to a JSON-serializable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RunCheckpoint:
        """Rebuild a checkpoint from a persisted dictionary."""
        return cls(**payload)
