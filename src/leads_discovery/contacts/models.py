"""Persisted M4 contact model kept separate from company records."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, cast

VerificationStatus = Literal["verified", "invalid", "pending"]


@dataclass(slots=True)
class ContactRecord:
    """Store one selected company contact plus enrichment and verification state."""

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
    email_verification_status: VerificationStatus | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    provider_attempts: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Detach mutable provenance and provider-attempt collections from caller state."""
        self.sources = deepcopy(self.sources)
        self.provider_attempts = deepcopy(self.provider_attempts)

    def to_dict(self) -> dict[str, Any]:
        """Convert the complete contact to a defensive JSON-safe dictionary."""
        return deepcopy(asdict(self))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ContactRecord:
        """Rebuild a contact without aliasing persisted nested collections."""
        data = deepcopy(payload)
        status = data.get("email_verification_status")
        if status not in {None, "verified", "invalid", "pending"}:
            raise ValueError("email_verification_status is invalid")
        return cls(
            contact_id=str(data["contact_id"]),
            company_id=str(data["company_id"]),
            company_name=str(data["company_name"]),
            company_domain=str(data["company_domain"]),
            company_final_score=(
                None
                if data.get("company_final_score") is None
                else float(data["company_final_score"])
            ),
            full_name=str(data["full_name"]),
            title=str(data["title"]),
            decision_rank=int(data["decision_rank"]),
            decision_reason=str(data["decision_reason"]),
            linkedin_url=cast(str | None, data.get("linkedin_url")),
            profile_url=cast(str | None, data.get("profile_url")),
            current_employment_confirmed=bool(data["current_employment_confirmed"]),
            work_email=cast(str | None, data.get("work_email")),
            email_source=cast(str | None, data.get("email_source")),
            email_verification_status=cast(VerificationStatus | None, status),
            sources=cast(list[dict[str, Any]], data.get("sources", [])),
            provider_attempts=cast(
                list[dict[str, Any]], data.get("provider_attempts", [])
            ),
        )
