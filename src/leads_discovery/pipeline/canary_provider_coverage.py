"""Canary-only M4 provider coverage composed strictly after the normal product path."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import httpx

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.providers import (
    ApolloContactProvider,
    ApolloResult,
    ClayContactProvider,
    ClayResults,
    ClayStartResult,
    ContactProviderError,
    ExaPeopleProvider,
    ExaPeopleResult,
    InstantlyVerificationProvider,
    VerificationResult,
    clay_item_email,
    usable_work_email,
)
from leads_discovery.contacts.selection import contact_decision_order_key, select_contacts
from leads_discovery.models import CompanyRecord, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.canary_paid_operations import CanaryPaidOperations
from leads_discovery.pipeline.state import load_jsonl, read_json

CoverageStatus = Literal["completed", "pending"]

_EXA_OPERATION = "coverage:exa_people"
_CLAY_OPERATION = "coverage:clay"
_APOLLO_OPERATION = "coverage:apollo"
_INSTANTLY_OPERATION = "coverage:instantly"


class _ExaPeopleProvider(Protocol):
    """Expose the production Exa People boundary used by M4."""

    def search(self, company: CompanyRecord) -> ExaPeopleResult:
        """Search current people for one production-derived company."""


class _ClayContactProvider(Protocol):
    """Expose the production Clay async routine boundary used by M4."""

    def start(self, contacts: list[ContactRecord]) -> ClayStartResult:
        """Start one bounded work-email routine."""

    def results(self, routine_run_id: str) -> ClayResults:
        """Read one bounded work-email routine status."""


class _ApolloContactProvider(Protocol):
    """Expose the production Apollo enrichment boundary used by M4."""

    def enrich(self, contact: ContactRecord) -> ApolloResult:
        """Enrich one production-selected contact."""


class _InstantlyVerificationProvider(Protocol):
    """Expose the production Instantly verification boundary used by M4."""

    def create(self, email: str) -> VerificationResult:
        """Create one bounded email verification."""

    def get(self, email: str) -> VerificationResult:
        """Read one bounded email verification status."""


@dataclass(frozen=True, slots=True)
class CanaryProviderCoverageSummary:
    """Return only the resumability state owned by this composition layer."""

    run_id: str
    status: CoverageStatus


def _completed_checkpoint(path: Path, run_id: str) -> RunCheckpoint:
    """Require one normal checkpoint to be terminal before coverage composition."""
    payload = read_json(path)
    if payload is None:
        raise ValueError(f"missing normal checkpoint: {path.name}")
    checkpoint = RunCheckpoint.from_dict(payload)
    if checkpoint.run_id != run_id:
        raise ValueError("normal checkpoint run_id mismatch")
    if checkpoint.status != "completed":
        raise RuntimeError("normal canary path is not completed")
    return checkpoint


def _operations(checkpoint: RunCheckpoint) -> dict[str, dict[str, Any]]:
    """Return a validated detached normal-operation mapping."""
    raw = checkpoint.provider_state.get("operations", {})
    if not isinstance(raw, dict):
        raise ValueError("normal operations must be an object")
    operations: dict[str, dict[str, Any]] = {}
    for operation_id, entry in raw.items():
        if not isinstance(operation_id, str) or not operation_id or not isinstance(entry, dict):
            raise ValueError("normal operation entry is invalid")
        operations[operation_id] = cast(dict[str, Any], entry)
    return operations


def _load_contacts(path: Path) -> dict[str, ContactRecord]:
    """Load canonical M4 contacts without granting the artifact provider authority by itself."""
    if not path.exists():
        return {}
    contacts: dict[str, ContactRecord] = {}
    for payload in load_jsonl(path):
        contact = ContactRecord.from_dict(payload)
        if contact.contact_id in contacts:
            raise ValueError("duplicate canonical contact id")
        contacts[contact.contact_id] = contact
    return contacts


def _load_company(run_dir: Path) -> CompanyRecord | None:
    """Load the fixed canary's zero-or-one evaluated production company."""
    path = run_dir / "companies_evaluated.jsonl"
    if not path.exists():
        raise ValueError("missing evaluated companies artifact")
    rows = load_jsonl(path)
    if len(rows) > 1:
        raise ValueError("provider coverage requires at most one evaluated company")
    if not rows:
        return None
    company = CompanyRecord.from_dict(rows[0])
    if company.stage_status.get("decision") != "completed":
        raise ValueError("provider coverage requires completed M3 decision state")
    if company.final_decision not in {"accepted", "rejected", "uncertain"}:
        raise ValueError("provider coverage requires a terminal company decision")
    return company


def _normal_selected_contact(
    company: CompanyRecord,
    operations: dict[str, dict[str, Any]],
    contacts: dict[str, ContactRecord],
) -> tuple[bool, ContactRecord | None]:
    """Use canonical contacts only when a completed normal Exa operation names them."""
    entry = operations.get(f"exa:{company.company_id}")
    if entry is None:
        return False, None
    if entry.get("state") != "completed":
        raise RuntimeError("normal Exa People operation is not resolved")
    raw_ids = entry.get("contact_ids")
    if not isinstance(raw_ids, list) or any(not isinstance(item, str) for item in raw_ids):
        raise ValueError("normal Exa People contact ids are invalid")
    selected: list[ContactRecord] = []
    for contact_id in raw_ids:
        contact = contacts.get(contact_id)
        if contact is None or contact.company_id != company.company_id:
            raise ValueError("normal Exa People contact reference is invalid")
        selected.append(contact)
    selected.sort(key=contact_decision_order_key)
    return True, selected[0] if selected else None


def _private_contact(entry: dict[str, Any]) -> ContactRecord | None:
    """Rebuild only a production-selected contact persisted in private canary state."""
    raw = entry.get("selected_contact")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("private selected contact is invalid")
    return ContactRecord.from_dict(cast(dict[str, Any], raw))


def _completed_sync_entry(entry: dict[str, Any], provider: str) -> None:
    """Reject replay of an unresolved synchronous provider outcome."""
    if entry.get("state") != "completed":
        raise RuntimeError(f"canary {provider} outcome is unresolved")


def _record_error_usage(
    paid: CanaryPaidOperations,
    operation_id: str,
    resource: Literal[
        "exa_people_search", "clay_start", "clay_status_read", "apollo_enrichment",
        "instantly_create", "instantly_status_read"
    ],
    *,
    input_value: object,
    error: ContactProviderError,
    apollo_reserved: bool = False,
) -> None:
    """Durably account a provider error while leaving its operation fail-closed in flight."""
    event = UsageEvent.from_dict(error.usage_event.to_dict())
    if apollo_reserved:
        event.metadata["credits_reserved"] = 1.0
    paid.record_usage(
        operation_id,
        resource,
        input_value=input_value,
        event=event,
    )


def _exa_contact(
    paid: CanaryPaidOperations,
    company: CompanyRecord,
    normal_exa_completed: bool,
    normal_contact: ContactRecord | None,
    exa: _ExaPeopleProvider,
) -> ContactRecord | None:
    """Prefer normal Exa lineage; otherwise perform at most one private exact-company search."""
    if normal_exa_completed:
        return normal_contact
    input_value = company.to_dict()
    entry = paid.operation(_EXA_OPERATION, input_value=input_value)
    if entry is not None:
        _completed_sync_entry(entry, "Exa People")
        return _private_contact(entry)
    if not paid.resource_allows("exa_people_search"):
        return None

    paid.begin(_EXA_OPERATION, "exa_people_search", input_value=input_value)
    try:
        result = exa.search(company)
    except ContactProviderError as error:
        _record_error_usage(
            paid,
            _EXA_OPERATION,
            "exa_people_search",
            input_value=input_value,
            error=error,
        )
        raise
    paid.record_usage(
        _EXA_OPERATION,
        "exa_people_search",
        input_value=input_value,
        event=result.usage_event,
    )
    selected = select_contacts(company, result.results, limit=1)
    contact = selected[0] if selected else None
    paid.finish(
        _EXA_OPERATION,
        input_value=input_value,
        fields={
            "business_outcome": "contact_selected" if contact is not None else "no_contact",
            "selected_contact": None if contact is None else contact.to_dict(),
        },
    )
    return contact


def _clay_email(
    paid: CanaryPaidOperations,
    contact: ContactRecord,
    clay: _ClayContactProvider,
) -> tuple[str | None, bool]:
    """Return production-normalized Clay email and whether composition must pause."""
    if contact.email_source == "clay" and contact.work_email is not None:
        return usable_work_email(contact.work_email), False

    input_value = contact.to_dict()
    entry = paid.operation(_CLAY_OPERATION, input_value=input_value)
    if entry is None:
        if not paid.resource_allows("clay_start"):
            return None, False
        paid.begin(_CLAY_OPERATION, "clay_start", input_value=input_value)
        try:
            started = clay.start([contact])
        except ContactProviderError as error:
            _record_error_usage(
                paid,
                _CLAY_OPERATION,
                "clay_start",
                input_value=input_value,
                error=error,
            )
            raise
        paid.record_usage(
            _CLAY_OPERATION,
            "clay_start",
            input_value=input_value,
            event=started.usage_event,
        )
        paid.finish(
            _CLAY_OPERATION,
            input_value=input_value,
            state="pending",
            fields={"routine_run_id": started.routine_run_id, "business_outcome": "pending"},
        )
        return None, True

    state = entry.get("state")
    if state == "completed":
        raw_email = entry.get("work_email")
        if raw_email is not None and not isinstance(raw_email, str):
            raise ValueError("private Clay work email is invalid")
        return usable_work_email(raw_email), False
    if state != "pending":
        raise RuntimeError("canary Clay outcome is unresolved")
    if not paid.resource_allows("clay_status_read"):
        return None, True
    routine_run_id = entry.get("routine_run_id")
    if not isinstance(routine_run_id, str) or not routine_run_id:
        raise ValueError("private Clay routine id is invalid")

    paid.reserve_async_read(
        _CLAY_OPERATION,
        "clay_status_read",
        input_value=input_value,
    )
    try:
        result = clay.results(routine_run_id)
    except ContactProviderError as error:
        _record_error_usage(
            paid,
            _CLAY_OPERATION,
            "clay_status_read",
            input_value=input_value,
            error=error,
        )
        raise
    paid.record_usage(
        _CLAY_OPERATION,
        "clay_status_read",
        input_value=input_value,
        event=result.usage_event,
    )
    if result.status == "pending":
        paid.finish(
            _CLAY_OPERATION,
            input_value=input_value,
            state="pending",
            fields={"routine_run_id": routine_run_id, "business_outcome": "pending"},
        )
        return None, True

    by_id = {
        str(item.get("id")): item
        for item in result.items
        if isinstance(item, dict) and item.get("id") is not None
    }
    email = clay_item_email(by_id.get(contact.contact_id, {}))
    paid.finish(
        _CLAY_OPERATION,
        input_value=input_value,
        fields={
            "routine_run_id": routine_run_id,
            "work_email": email,
            "business_outcome": "email" if email is not None else "no_email",
        },
    )
    return email, False


def _finite_nonnegative(value: float, label: str) -> float:
    """Reject malformed quota evidence before it enters private accounting."""
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite and nonnegative")
    return value


def _apollo_email(
    paid: CanaryPaidOperations,
    contact: ContactRecord,
    apollo: _ApolloContactProvider,
) -> str | None:
    """Run exactly one private Apollo fallback when normal Apollo did not consume the slot."""
    if contact.email_source == "apollo" and contact.work_email is not None:
        normal_email = usable_work_email(contact.work_email)
    else:
        normal_email = None

    input_value = contact.to_dict()
    entry = paid.operation(_APOLLO_OPERATION, input_value=input_value)
    if entry is not None:
        _completed_sync_entry(entry, "Apollo")
        raw_email = entry.get("work_email")
        if raw_email is not None and not isinstance(raw_email, str):
            raise ValueError("private Apollo work email is invalid")
        return usable_work_email(raw_email)
    if not paid.resource_allows("apollo_enrichment"):
        return normal_email

    paid.begin(_APOLLO_OPERATION, "apollo_enrichment", input_value=input_value)
    try:
        result = apollo.enrich(contact)
    except ContactProviderError as error:
        _record_error_usage(
            paid,
            _APOLLO_OPERATION,
            "apollo_enrichment",
            input_value=input_value,
            error=error,
            apollo_reserved=True,
        )
        raise
    used = _finite_nonnegative(
        1.0 if result.credits_used is None else float(result.credits_used),
        "Apollo credits",
    )
    event = UsageEvent.from_dict(result.usage_event.to_dict())
    event.metadata["credits_used"] = used
    if result.credits_used is None:
        event.metadata["credits_reserved"] = 1.0
    paid.record_usage(
        _APOLLO_OPERATION,
        "apollo_enrichment",
        input_value=input_value,
        event=event,
    )
    email = usable_work_email(result.work_email)
    paid.finish(
        _APOLLO_OPERATION,
        input_value=input_value,
        fields={
            "credits_used": used,
            "work_email": email,
            "business_outcome": "email" if email is not None else "no_email",
        },
    )
    return email


def _verify_email(
    paid: CanaryPaidOperations,
    email: str,
    instantly: _InstantlyVerificationProvider,
) -> bool:
    """Verify the production-normalized Clay-first/Apollo-second email with bounded replay."""
    normalized = usable_work_email(email)
    if normalized is None or normalized != email:
        raise ValueError("canary verification requires a production-normalized work email")
    input_value: object = normalized
    entry = paid.operation(_INSTANTLY_OPERATION, input_value=input_value)
    if entry is None:
        if not paid.resource_allows("instantly_create"):
            return False
        paid.begin(_INSTANTLY_OPERATION, "instantly_create", input_value=input_value)
        try:
            result = instantly.create(normalized)
        except ContactProviderError as error:
            _record_error_usage(
                paid,
                _INSTANTLY_OPERATION,
                "instantly_create",
                input_value=input_value,
                error=error,
            )
            raise
        paid.record_usage(
            _INSTANTLY_OPERATION,
            "instantly_create",
            input_value=input_value,
            event=result.usage_event,
        )
        if result.status == "pending":
            paid.finish(
                _INSTANTLY_OPERATION,
                input_value=input_value,
                state="pending",
                fields={"email": normalized, "business_outcome": "pending"},
            )
            return True
        paid.finish(
            _INSTANTLY_OPERATION,
            input_value=input_value,
            fields={
                "email": normalized,
                "verification_status": result.status,
                "business_outcome": result.status,
            },
        )
        return False

    state = entry.get("state")
    if state == "completed":
        return False
    if state != "pending":
        raise RuntimeError("canary Instantly outcome is unresolved")
    if not paid.resource_allows("instantly_status_read"):
        return True

    paid.reserve_async_read(
        _INSTANTLY_OPERATION,
        "instantly_status_read",
        input_value=input_value,
    )
    try:
        result = instantly.get(normalized)
    except ContactProviderError as error:
        _record_error_usage(
            paid,
            _INSTANTLY_OPERATION,
            "instantly_status_read",
            input_value=input_value,
            error=error,
        )
        raise
    paid.record_usage(
        _INSTANTLY_OPERATION,
        "instantly_status_read",
        input_value=input_value,
        event=result.usage_event,
    )
    if result.status == "pending":
        paid.finish(
            _INSTANTLY_OPERATION,
            input_value=input_value,
            state="pending",
            fields={"email": normalized, "business_outcome": "pending"},
        )
        return True
    paid.finish(
        _INSTANTLY_OPERATION,
        input_value=input_value,
        fields={
            "email": normalized,
            "verification_status": result.status,
            "business_outcome": result.status,
        },
    )
    return False


def _finish_summary(
    paid: CanaryPaidOperations,
    run_id: str,
    *,
    pending: bool,
) -> CanaryProviderCoverageSummary:
    """Complete private state only after every possible bounded operation is terminal."""
    if pending:
        return CanaryProviderCoverageSummary(run_id, "pending")
    if paid.checkpoint.status != "completed":
        paid.complete()
    return CanaryProviderCoverageSummary(run_id, "completed")


def run_provider_coverage(
    run_dir: Path,
    *,
    run_id: str,
    exa: _ExaPeopleProvider,
    clay: _ClayContactProvider,
    apollo: _ApolloContactProvider,
    instantly: _InstantlyVerificationProvider,
) -> CanaryProviderCoverageSummary:
    """Run only provider calls legitimately skipped by the completed normal one-company M4 path."""
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ValueError("canary run directory must be a real directory")
    _completed_checkpoint(run_dir / "checkpoint.json", run_id)
    contact_checkpoint = _completed_checkpoint(run_dir / "contact_checkpoint.json", run_id)
    normal_operations = _operations(contact_checkpoint)
    contacts = _load_contacts(run_dir / "contacts.jsonl")
    company = _load_company(run_dir)
    paid = CanaryPaidOperations.open(run_dir, run_id=run_id)
    if company is None:
        return _finish_summary(paid, run_id, pending=False)

    normal_exa_completed, normal_contact = _normal_selected_contact(
        company,
        normal_operations,
        contacts,
    )
    contact = _exa_contact(paid, company, normal_exa_completed, normal_contact, exa)
    if contact is None:
        return _finish_summary(paid, run_id, pending=False)

    clay_email, clay_pending = _clay_email(paid, contact, clay)
    if clay_pending:
        return _finish_summary(paid, run_id, pending=True)
    apollo_email = _apollo_email(paid, contact, apollo)
    verification_email = clay_email or apollo_email
    if verification_email is None:
        return _finish_summary(paid, run_id, pending=False)
    verification_pending = _verify_email(paid, verification_email, instantly)
    return _finish_summary(paid, run_id, pending=verification_pending)


def run_live_provider_coverage(
    data_root: Path,
    *,
    run_id: str,
) -> CanaryProviderCoverageSummary:
    """Construct the same production M4 adapters only after normal canary execution succeeds."""
    names = (
        "EXA_API_KEY",
        "CLAY_PUBLIC_API_KEY",
        "CLAY_CONTACT_ROUTINE_ID",
        "APOLLO_API_KEY",
        "INSTANTLY_API_KEY",
    )
    credentials = {name: os.environ.get(name, "") for name in names}
    if any(not credentials[name] for name in names):
        raise RuntimeError("required provider credentials missing")
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
        return run_provider_coverage(
            data_root / run_id,
            run_id=run_id,
            exa=ExaPeopleProvider(api_key=credentials["EXA_API_KEY"], client=client),
            clay=ClayContactProvider(
                api_key=credentials["CLAY_PUBLIC_API_KEY"],
                routine_id=credentials["CLAY_CONTACT_ROUTINE_ID"],
                client=client,
            ),
            apollo=ApolloContactProvider(
                api_key=credentials["APOLLO_API_KEY"],
                client=client,
            ),
            instantly=InstantlyVerificationProvider(
                api_key=credentials["INSTANTLY_API_KEY"],
                client=client,
            ),
        )


__all__ = [
    "CanaryProviderCoverageSummary",
    "run_live_provider_coverage",
    "run_provider_coverage",
]
