"""Bounded Clay -> Apollo -> Instantly enrichment for the persisted contact shortlist."""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.providers import (
    ApolloResult,
    ClayResults,
    ClayStartResult,
    ContactProviderError,
    VerificationResult,
    clay_item_email,
)
from leads_discovery.models import CompanyRecord, RunCheckpoint
from leads_discovery.pipeline.contact_discovery import (
    _accepted,
    _accepted_fingerprint,
    _load_contacts,
    _ordered_contacts,
)
from leads_discovery.pipeline.costs import CostTracker
from leads_discovery.pipeline.paid_operations import PaidOperationLifecycle
from leads_discovery.pipeline.state import (
    load_usage_events,
    read_json,
    write_checkpoint,
    write_json_atomic,
    write_jsonl_atomic,
    write_text_atomic,
)

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FORMULA_PREFIXES = frozenset("=+-@")
_CSV_COLUMNS = (
    "company_id",
    "company_name",
    "company_domain",
    "company_final_score",
    "contact_id",
    "full_name",
    "title",
    "decision_rank",
    "decision_reason",
    "work_email",
    "email_verification_status",
    "linkedin_url",
    "profile_url",
    "email_source",
)


class _ClayProvider(Protocol):
    def start(self, contacts: list[ContactRecord]) -> ClayStartResult: ...

    def results(self, routine_run_id: str) -> ClayResults: ...


class _ApolloProvider(Protocol):
    def enrich(self, contact: ContactRecord) -> ApolloResult: ...


class _InstantlyProvider(Protocol):
    def create(self, email: str) -> VerificationResult: ...

    def get(self, email: str) -> VerificationResult: ...


@dataclass(frozen=True, slots=True)
class ContactEnrichmentConfig:
    run_id: str
    data_root: Path = Path("data")
    max_paid_contacts_per_company: int = 2
    clay_contact_cap: int = 10
    apollo_credit_cap: float = 5.0
    instantly_verification_call_cap: int = 5
    execute_live: bool = False


@dataclass(frozen=True, slots=True)
class ContactEnrichmentSummary:
    run_id: str
    status: str
    accepted_company_count: int
    contact_count: int
    paid_candidate_count: int
    verified_email_count: int
    contacts_path: Path
    leads_path: Path
    usage_path: Path
    checkpoint_path: Path


@dataclass(frozen=True, slots=True)
class _Paths:
    run_dir: Path
    evaluated: Path
    contacts: Path
    leads: Path
    usage_events: Path
    usage: Path
    checkpoint: Path


def _nonnegative(value: object, *, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a nonnegative finite number")
    return float(value)


def _paths(config: ContactEnrichmentConfig) -> _Paths:
    if not isinstance(config.run_id, str) or _RUN_ID.fullmatch(config.run_id) is None:
        raise ValueError("run_id must match the safe run-id grammar")
    if (
        isinstance(config.max_paid_contacts_per_company, bool)
        or not isinstance(config.max_paid_contacts_per_company, int)
        or not 0 <= config.max_paid_contacts_per_company <= 2
    ):
        raise ValueError("max_paid_contacts_per_company must be an integer in 0..2")
    for name, value in (
        ("clay_contact_cap", config.clay_contact_cap),
        ("instantly_verification_call_cap", config.instantly_verification_call_cap),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    _nonnegative(config.apollo_credit_cap, name="apollo_credit_cap")

    root_candidate = config.data_root.expanduser()
    if root_candidate.is_symlink():
        raise ValueError("data_root must not be a symlink")
    root = root_candidate.resolve()
    run_candidate = root / config.run_id
    if run_candidate.is_symlink():
        raise ValueError("run directory must not be a symlink")
    run_dir = run_candidate.resolve()
    if run_dir.parent != root or not run_dir.is_dir():
        raise ValueError("run directory must exist directly beneath data_root")

    evaluated = run_dir / "companies_evaluated.jsonl"
    if evaluated.is_symlink() or not evaluated.is_file():
        raise ValueError("companies_evaluated.jsonl must be a regular M3 artifact")
    outputs = (
        run_dir / "contacts.jsonl",
        run_dir / "leads.csv",
        run_dir / "contact_usage_events.jsonl",
        run_dir / "contact_usage.json",
        run_dir / "contact_discovery_checkpoint.json",
    )
    if any(path.is_symlink() for path in outputs):
        raise ValueError("contact enrichment artifacts must not be symlinks")
    return _Paths(run_dir, evaluated, *outputs)


def _load_checkpoint(path: Path, run_id: str) -> RunCheckpoint:
    payload = read_json(path)
    if payload is None:
        raise ValueError("contact discovery checkpoint is required before enrichment")
    checkpoint = RunCheckpoint.from_dict(payload)
    if checkpoint.run_id != run_id:
        raise ValueError("contact discovery checkpoint run_id mismatch")
    return checkpoint


def _safe_csv(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    stripped = text.lstrip()
    return "'" + text if stripped and stripped[0] in _FORMULA_PREFIXES else text


def _publish_outputs(paths: _Paths, contacts: dict[str, ContactRecord]) -> None:
    ordered = _ordered_contacts(contacts)
    write_jsonl_atomic(paths.contacts, (contact.to_dict() for contact in ordered))

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(_CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for contact in ordered:
        writer.writerow({name: _safe_csv(getattr(contact, name)) for name in _CSV_COLUMNS})
    write_text_atomic(paths.leads, output.getvalue())


def _paid_contacts(
    contacts: dict[str, ContactRecord],
    accepted_ids: frozenset[str],
    limit: int,
) -> list[ContactRecord]:
    counts: dict[str, int] = {}
    paid: list[ContactRecord] = []
    for contact in _ordered_contacts(contacts):
        if (
            contact.company_id not in accepted_ids
            or not contact.current_employment_confirmed
            or contact.decision_rank not in {1, 2}
        ):
            continue
        count = counts.get(contact.company_id, 0)
        if count >= limit:
            continue
        counts[contact.company_id] = count + 1
        paid.append(contact)
    return paid


def _attempted(contact: ContactRecord, provider: str, state: str = "completed") -> bool:
    return any(
        item.get("provider") == provider and item.get("state") == state
        for item in contact.provider_attempts
    )


def _mark_attempt(
    contact: ContactRecord,
    provider: str,
    operation: str,
    state: str,
    **extra: Any,
) -> None:
    if any(
        item.get("provider") == provider
        and item.get("operation") == operation
        and item.get("state") == state
        for item in contact.provider_attempts
    ):
        return
    contact.provider_attempts.append(
        {"provider": provider, "operation": operation, "state": state, **extra}
    )


def _validate_shortlist(
    accepted: tuple[CompanyRecord, ...],
    operations: dict[str, dict[str, Any]],
    contacts: dict[str, ContactRecord],
) -> None:
    accepted_ids = frozenset(company.company_id for company in accepted)
    if any(contact.company_id not in accepted_ids for contact in contacts.values()):
        raise ValueError("persisted contact is outside the current accepted-company set")
    for company in accepted:
        entry = operations.get(f"exa_people:{company.company_id}")
        if not isinstance(entry, dict) or entry.get("state") != "completed":
            raise ValueError("contact enrichment requires completed people discovery")
        raw_ids = entry.get("contact_ids")
        if not isinstance(raw_ids, list) or any(not isinstance(item, str) for item in raw_ids):
            raise ValueError("completed people discovery has malformed contact references")
        expected = set(cast(list[str], raw_ids))
        actual = {
            contact.contact_id
            for contact in contacts.values()
            if contact.company_id == company.company_id
        }
        if expected != actual:
            raise ValueError("persisted contacts do not match completed people discovery")


def _next_id(operations: dict[str, dict[str, Any]], prefix: str) -> str:
    index = 1
    while f"{prefix}:{index}" in operations:
        index += 1
    return f"{prefix}:{index}"


def _summary(
    config: ContactEnrichmentConfig,
    paths: _Paths,
    accepted: tuple[CompanyRecord, ...],
    checkpoint: RunCheckpoint,
    contacts: dict[str, ContactRecord],
) -> ContactEnrichmentSummary:
    accepted_ids = frozenset(company.company_id for company in accepted)
    paid = _paid_contacts(contacts, accepted_ids, config.max_paid_contacts_per_company)
    return ContactEnrichmentSummary(
        run_id=config.run_id,
        status=checkpoint.status,
        accepted_company_count=len(accepted),
        contact_count=len(contacts),
        paid_candidate_count=len(paid),
        verified_email_count=sum(
            contact.email_verification_status == "verified"
            for contact in contacts.values()
        ),
        contacts_path=paths.contacts,
        leads_path=paths.leads,
        usage_path=paths.usage,
        checkpoint_path=paths.checkpoint,
    )


def _record_error(
    lifecycle: PaidOperationLifecycle,
    operation_id: str,
    error: ContactProviderError,
) -> bool:
    """Record usage and return whether the provider outcome is known."""
    lifecycle.record_usage(error.usage_event)
    if error.status_code is None:
        return False
    lifecycle.finish(operation_id, state="failed", error_kind=error.kind)
    return True


def run_contact_enrichment(
    config: ContactEnrichmentConfig,
    *,
    clay: _ClayProvider,
    apollo: _ApolloProvider,
    instantly: _InstantlyProvider,
) -> ContactEnrichmentSummary:
    """Run only the paid boundary; every provider call is lifecycle-admitted."""
    if not config.execute_live:
        raise ValueError("run_contact_enrichment requires explicit live execution")

    paths = _paths(config)
    accepted = _accepted(paths.evaluated)
    checkpoint = _load_checkpoint(paths.checkpoint, config.run_id)
    contacts = _load_contacts(paths.contacts)

    raw_operations = checkpoint.provider_state.get("operations")
    if raw_operations and not paths.usage_events.is_file():
        raise ValueError("authoritative contact usage ledger is missing")
    events = load_usage_events(paths.usage_events)
    tracker = CostTracker(events)
    expected_usage = cast(dict[str, Any], tracker.summary())
    try:
        current_usage = read_json(paths.usage)
    except ValueError:
        current_usage = None
    if current_usage != expected_usage:
        write_json_atomic(paths.usage, expected_usage)

    def persist_checkpoint() -> None:
        write_checkpoint(paths.checkpoint, checkpoint)

    def publish_usage() -> None:
        write_json_atomic(paths.usage, cast(dict[str, Any], tracker.summary()))

    lifecycle = PaidOperationLifecycle(
        checkpoint=checkpoint,
        tracker=tracker,
        usage_path=paths.usage_events,
        persist_checkpoint=persist_checkpoint,
        publish_usage=publish_usage,
    )
    operations = lifecycle.operations()

    def stop(status: str, reason: str) -> ContactEnrichmentSummary:
        lifecycle.pause(status=status, reason=reason, stage="contact_enrichment")
        return _summary(config, paths, accepted, checkpoint, contacts)

    if checkpoint.status == "paused_unknown":
        return _summary(config, paths, accepted, checkpoint, contacts)

    stored_fingerprint = checkpoint.provider_state.get("accepted_fingerprint")
    current_fingerprint = _accepted_fingerprint(accepted)
    if stored_fingerprint != current_fingerprint:
        return stop("paused_unknown", "contact_shortlist_stale")
    if lifecycle.freeze_if_unknown(reason_prefix="contact_paid_outcome_unknown") is not None:
        return _summary(config, paths, accepted, checkpoint, contacts)

    _validate_shortlist(accepted, operations, contacts)
    accepted_ids = frozenset(company.company_id for company in accepted)
    paid = _paid_contacts(contacts, accepted_ids, config.max_paid_contacts_per_company)
    paid_ids = {contact.contact_id for contact in paid}

    pending_clay = [
        (operation_id, entry)
        for operation_id, entry in sorted(operations.items())
        if operation_id.startswith("clay_batch:") and entry.get("state") == "pending"
    ]
    if len(pending_clay) > 1:
        raise ValueError("multiple pending Clay batches are not supported")
    if pending_clay:
        parent_id, parent = pending_clay[0]
        raw_ids = parent.get("contact_ids")
        routine_run_id = parent.get("routine_run_id")
        if (
            not isinstance(raw_ids, list)
            or any(not isinstance(item, str) or item not in contacts for item in raw_ids)
            or not isinstance(routine_run_id, str)
            or not routine_run_id
        ):
            raise ValueError("pending Clay operation is malformed")
        contact_ids = cast(list[str], raw_ids)
        if any(contact_id not in paid_ids for contact_id in contact_ids):
            return stop("paused_unknown", "clay_authorization_changed")

        result_id = _next_id(operations, f"clay_results:{parent_id}")
        if not lifecycle.admit_quota(
            result_id,
            provider="clay",
            operation="work_email_routine_results",
            ceiling=None,
            reservation=0.0,
            budget_reason="clay_contact_cap",
            quota_operation="work_email_routine_start",
            metadata_field="submitted_contacts",
            fields={"routine_run_id": routine_run_id, "parent_operation_id": parent_id},
            pending_stage="clay_work_email_results",
        ):
            return _summary(config, paths, accepted, checkpoint, contacts)
        try:
            clay_result = clay.results(routine_run_id)
        except ContactProviderError as error:
            known = _record_error(lifecycle, result_id, error)
            if not known:
                return stop("paused_unknown", f"clay_results_unknown:{parent_id}")
            if error.kind == "budget_exhausted":
                return stop("paused_budget", "clay_provider_budget")
            return stop("paused_pending", f"clay_results_failed:{error.kind}")

        lifecycle.record_usage(clay_result.usage_event)
        lifecycle.finish(result_id, fields={"result_status": clay_result.status})
        if clay_result.status == "pending":
            return stop("paused_pending", "clay_results_pending")

        by_id: dict[str, dict[str, Any]] = {}
        for item in clay_result.items:
            raw_id = item.get("id")
            if isinstance(raw_id, str):
                by_id[raw_id] = item
        for contact_id in contact_ids:
            contact = contacts[contact_id]
            email = clay_item_email(by_id.get(contact_id, {}))
            if email is not None:
                contact.work_email = email
                contact.email_source = "clay"
            _mark_attempt(
                contact,
                "clay",
                "work_email_routine",
                "completed",
                routine_run_id=routine_run_id,
            )
        _publish_outputs(paths, contacts)
        lifecycle.finish(parent_id, fields={"routine_run_id": routine_run_id})

    paid = _paid_contacts(contacts, accepted_ids, config.max_paid_contacts_per_company)
    clay_pending = [contact for contact in paid if not _attempted(contact, "clay")]
    if clay_pending:
        used = lifecycle.quota_used(
            "clay",
            operation="work_email_routine_start",
            metadata_field="submitted_contacts",
        )
        remaining = max(0, int(math.floor(config.clay_contact_cap - used + 1e-12)))
        if remaining == 0:
            return stop("paused_budget", "clay_contact_cap")
        batch = clay_pending[:remaining]
        operation_id = _next_id(operations, "clay_batch")
        if not lifecycle.admit_quota(
            operation_id,
            provider="clay",
            operation="work_email_routine_start",
            ceiling=float(config.clay_contact_cap),
            reservation=float(len(batch)),
            budget_reason="clay_contact_cap",
            quota_operation="work_email_routine_start",
            metadata_field="submitted_contacts",
            fields={"contact_ids": [contact.contact_id for contact in batch]},
            pending_stage="clay_work_email",
        ):
            return _summary(config, paths, accepted, checkpoint, contacts)
        try:
            started = clay.start(batch)
        except ContactProviderError as error:
            known = _record_error(lifecycle, operation_id, error)
            if not known:
                return stop("paused_unknown", f"clay_start_unknown:{operation_id}")
            if error.kind == "budget_exhausted":
                return stop("paused_budget", "clay_provider_budget")
            return stop("paused_pending", f"clay_start_failed:{error.kind}")

        lifecycle.record_usage(started.usage_event)
        lifecycle.finish(
            operation_id,
            state="pending",
            fields={
                "routine_run_id": started.routine_run_id,
                "contact_ids": [contact.contact_id for contact in batch],
            },
        )
        return stop("paused_pending", "clay_results_pending")

    for contact in paid:
        if contact.work_email is not None or _attempted(contact, "apollo"):
            continue
        operation_id = f"apollo:{contact.contact_id}"
        state = operations.get(operation_id)
        if state is not None and state.get("state") == "completed":
            continue
        if not lifecycle.admit_quota(
            operation_id,
            provider="apollo",
            operation="people_enrichment",
            ceiling=config.apollo_credit_cap,
            reservation=1.0,
            budget_reason="apollo_credit_cap",
            quota_operation="people_enrichment",
            metadata_field="credits_used",
            company_id=contact.company_id,
            fields={"contact_id": contact.contact_id},
            pending_stage="apollo_enrichment",
        ):
            return _summary(config, paths, accepted, checkpoint, contacts)
        try:
            apollo_result = apollo.enrich(contact)
        except ContactProviderError as error:
            known = _record_error(lifecycle, operation_id, error)
            if not known:
                return stop("paused_unknown", f"apollo_unknown:{contact.contact_id}")
            if error.kind == "budget_exhausted":
                return stop("paused_budget", "apollo_provider_budget")
            return stop("paused_pending", f"apollo_failed:{error.kind}")

        lifecycle.record_usage(apollo_result.usage_event)
        if apollo_result.work_email is not None:
            contact.work_email = apollo_result.work_email
            contact.email_source = "apollo"
        _mark_attempt(contact, "apollo", "people_enrichment", "completed")
        _publish_outputs(paths, contacts)
        lifecycle.finish(
            operation_id,
            fields={"credits_used": apollo_result.credits_used},
        )

    for contact in paid:
        email = contact.work_email
        if email is None or _attempted(contact, "instantly"):
            continue
        parent_id = f"instantly:{contact.contact_id}"
        instantly_state = operations.get(parent_id)
        pending = (
            instantly_state is not None and instantly_state.get("state") == "pending"
        )
        if instantly_state is not None and instantly_state.get("state") == "completed":
            continue
        if pending:
            assert instantly_state is not None
            if instantly_state.get("email") != email:
                raise ValueError("pending Instantly email does not match the contact email")

        dispatch_id = (
            _next_id(operations, f"instantly_get:{contact.contact_id}")
            if pending
            else parent_id
        )
        operation = "email_verification_get" if pending else "email_verification_create"
        if not lifecycle.admit_quota(
            dispatch_id,
            provider="instantly",
            operation=operation,
            ceiling=float(config.instantly_verification_call_cap),
            reservation=1.0,
            budget_reason="instantly_verification_call_cap",
            company_id=contact.company_id,
            fields={"email": email, "parent_operation_id": parent_id},
            pending_stage="instantly_verification",
        ):
            return _summary(config, paths, accepted, checkpoint, contacts)

        try:
            verification_result = (
                instantly.get(email) if pending else instantly.create(email)
            )
        except ContactProviderError as error:
            known = _record_error(lifecycle, dispatch_id, error)
            if not known:
                return stop("paused_unknown", f"instantly_unknown:{contact.contact_id}")
            if error.kind == "budget_exhausted":
                return stop("paused_budget", "instantly_provider_budget")
            return stop("paused_pending", f"instantly_failed:{error.kind}")

        lifecycle.record_usage(verification_result.usage_event)
        if pending:
            lifecycle.finish(
                dispatch_id,
                fields={"result_status": verification_result.status},
            )

        contact.email_verification_status = verification_result.status
        if verification_result.status == "pending":
            _mark_attempt(contact, "instantly", "email_verification", "pending")
            _publish_outputs(paths, contacts)
            lifecycle.finish(parent_id, state="pending", fields={"email": email})
            return stop("paused_pending", f"instantly_pending:{contact.contact_id}")

        _mark_attempt(
            contact,
            "instantly",
            "email_verification",
            "completed",
            status=verification_result.status,
        )
        _publish_outputs(paths, contacts)
        lifecycle.finish(
            parent_id,
            fields={"email": email, "status": verification_result.status},
        )

    if any(not _attempted(contact, "clay") for contact in paid):
        return stop("paused_budget", "clay_contact_cap")

    _publish_outputs(paths, contacts)
    checkpoint.status = "completed"
    checkpoint.pause_reason = None
    checkpoint.pending_company_id = None
    checkpoint.pending_stage = None
    persist_checkpoint()
    return _summary(config, paths, accepted, checkpoint, contacts)


__all__ = [
    "ContactEnrichmentConfig",
    "ContactEnrichmentSummary",
    "run_contact_enrichment",
]
