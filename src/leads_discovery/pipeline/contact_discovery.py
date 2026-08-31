"""Accepted-only, resumable M4 people discovery and deterministic shortlist publication."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.providers import ExaPeopleResult
from leads_discovery.contacts.selection import contact_decision_order_key, select_contacts
from leads_discovery.discovery.base import DiscoveryProviderError
from leads_discovery.models import CompanyRecord, RunCheckpoint
from leads_discovery.pipeline.costs import CostTracker
from leads_discovery.pipeline.paid_operations import PaidOperationLifecycle
from leads_discovery.pipeline.state import (
    load_jsonl,
    load_usage_events,
    read_json,
    write_checkpoint,
    write_json_atomic,
    write_jsonl_atomic,
)

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_EXA_PEOPLE_RESERVATION_USD = 0.017


@dataclass(frozen=True, slots=True)
class ContactDiscoveryConfig:
    """Configure one accepted-company people-discovery pass."""

    run_id: str
    data_root: Path = Path("data")
    max_contacts_per_company: int = 3
    exa_people_budget_usd: float | None = None
    execute_live: bool = False


@dataclass(frozen=True, slots=True)
class ContactDiscoverySummary:
    """Summarize the current deterministic shortlist state."""

    run_id: str
    status: str
    accepted_company_count: int
    contact_count: int
    contacts_path: Path
    checkpoint_path: Path


@dataclass(frozen=True, slots=True)
class _Paths:
    run_dir: Path
    evaluated: Path
    contacts: Path
    usage_events: Path
    usage: Path
    checkpoint: Path


def _paths(config: ContactDiscoveryConfig) -> _Paths:
    if not isinstance(config.run_id, str) or _RUN_ID.fullmatch(config.run_id) is None:
        raise ValueError("run_id must match the safe run-id grammar")
    if (
        isinstance(config.max_contacts_per_company, bool)
        or not isinstance(config.max_contacts_per_company, int)
        or not 1 <= config.max_contacts_per_company <= 3
    ):
        raise ValueError("max_contacts_per_company must be an integer in 1..3")
    if config.exa_people_budget_usd is not None:
        value = config.exa_people_budget_usd
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("exa_people_budget_usd must be a nonnegative finite number")

    expanded_root = config.data_root.expanduser()
    if expanded_root.is_symlink():
        raise ValueError("data_root must not be a symlink")
    root = expanded_root.resolve()
    candidate = root / config.run_id
    if candidate.is_symlink():
        raise ValueError("run directory must not be a symlink")
    run_dir = candidate.resolve()
    if run_dir.parent != root or not run_dir.is_dir():
        raise ValueError("run directory must exist directly beneath data_root")
    evaluated = run_dir / "companies_evaluated.jsonl"
    if evaluated.is_symlink() or not evaluated.is_file():
        raise ValueError("companies_evaluated.jsonl must be a regular M3 artifact")
    outputs = [
        run_dir / "contacts.jsonl",
        run_dir / "contact_usage_events.jsonl",
        run_dir / "contact_usage.json",
        run_dir / "contact_discovery_checkpoint.json",
    ]
    if any(path.is_symlink() for path in outputs):
        raise ValueError("contact discovery artifacts must not be symlinks")
    return _Paths(run_dir, evaluated, *outputs)


def _accepted(path: Path) -> tuple[CompanyRecord, ...]:
    companies: dict[str, CompanyRecord] = {}
    for payload in load_jsonl(path):
        company = CompanyRecord.from_dict(payload)
        if company.company_id in companies:
            raise ValueError("companies_evaluated.jsonl contains duplicate company IDs")
        if company.stage_status.get("decision") != "completed":
            raise ValueError("M4 requires completed M3 decision state")
        companies[company.company_id] = company
    if len(companies) > 20:
        raise ValueError("M4 input exceeds the M3 evaluated-company ceiling")
    return tuple(
        sorted(
            (item for item in companies.values() if item.final_decision == "accepted"),
            key=lambda item: item.company_id,
        )
    )


def _accepted_fingerprint(companies: tuple[CompanyRecord, ...]) -> str:
    """Hash the exact M3 authorization and identity fields used for people search."""
    payload = [
        [
            item.company_id,
            item.name,
            item.normalized_domain or item.domain,
            item.final_decision,
        ]
        for item in companies
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _load_contacts(path: Path) -> dict[str, ContactRecord]:
    contacts: dict[str, ContactRecord] = {}
    for payload in load_jsonl(path):
        contact = ContactRecord.from_dict(payload)
        if contact.contact_id in contacts:
            raise ValueError("contacts.jsonl contains duplicate contact IDs")
        contacts[contact.contact_id] = contact
    return contacts


def _ordered_contacts(contacts: dict[str, ContactRecord]) -> list[ContactRecord]:
    return sorted(
        contacts.values(),
        key=lambda item: (item.company_id, *contact_decision_order_key(item)),
    )


def _write_contacts(path: Path, contacts: dict[str, ContactRecord]) -> None:
    write_jsonl_atomic(path, (item.to_dict() for item in _ordered_contacts(contacts)))


def _load_checkpoint(path: Path, run_id: str) -> RunCheckpoint:
    payload = read_json(path)
    if payload is None:
        return RunCheckpoint(run_id=run_id, provider_state={"operations": {}})
    checkpoint = RunCheckpoint.from_dict(payload)
    if checkpoint.run_id != run_id:
        raise ValueError("contact discovery checkpoint run_id mismatch")
    return checkpoint


def _fingerprint(checkpoint: RunCheckpoint) -> str | None:
    value = checkpoint.provider_state.get("accepted_fingerprint")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("contact discovery accepted fingerprint is malformed")
    return value


def _summary(
    config: ContactDiscoveryConfig,
    paths: _Paths,
    accepted: tuple[CompanyRecord, ...],
    checkpoint: RunCheckpoint,
    contacts: dict[str, ContactRecord],
) -> ContactDiscoverySummary:
    return ContactDiscoverySummary(
        run_id=config.run_id,
        status=checkpoint.status,
        accepted_company_count=len(accepted),
        contact_count=len(contacts),
        contacts_path=paths.contacts,
        checkpoint_path=paths.checkpoint,
    )


def run_contact_discovery(
    config: ContactDiscoveryConfig,
    *,
    exa_search: Callable[[CompanyRecord], ExaPeopleResult],
) -> ContactDiscoverySummary:
    """Discover and shortlist people only for the current M3 accepted set."""
    if not config.execute_live:
        raise ValueError("run_contact_discovery requires explicit live execution")
    paths = _paths(config)
    accepted = _accepted(paths.evaluated)
    current_fingerprint = _accepted_fingerprint(accepted)
    checkpoint = _load_checkpoint(paths.checkpoint, config.run_id)
    contacts = _load_contacts(paths.contacts)
    tracker = CostTracker(load_usage_events(paths.usage_events))

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
    saved_fingerprint = _fingerprint(checkpoint)

    if saved_fingerprint != current_fingerprint:
        unknown = lifecycle.unknown_in_flight()
        if unknown is not None:
            operation_id, _entry = unknown
            lifecycle.pause(
                status="paused_unknown",
                reason=f"accepted_set_changed_with_unknown:{operation_id}",
                stage="people_search",
            )
            return _summary(config, paths, accepted, checkpoint, contacts)
        operations.clear()
        contacts.clear()
        checkpoint.provider_state["accepted_fingerprint"] = current_fingerprint
        checkpoint.status = "running"
        checkpoint.pause_reason = None
        checkpoint.pending_company_id = None
        checkpoint.pending_stage = None
        _write_contacts(paths.contacts, contacts)
        persist_checkpoint()
    elif checkpoint.status == "completed":
        expected = cast(dict[str, Any], tracker.summary())
        try:
            current_usage = read_json(paths.usage)
        except ValueError:
            current_usage = None
        if current_usage != expected:
            write_json_atomic(paths.usage, expected)
        return _summary(config, paths, accepted, checkpoint, contacts)

    if lifecycle.freeze_if_unknown() is not None:
        return _summary(config, paths, accepted, checkpoint, contacts)

    accepted_ids = {item.company_id for item in accepted}
    if any(contact.company_id not in accepted_ids for contact in contacts.values()):
        raise ValueError("persisted contact is outside the current accepted-company set")

    for company in accepted:
        operation_id = f"exa_people:{company.company_id}"
        state = operations.get(operation_id)
        if state is not None:
            if state.get("state") == "completed":
                contact_ids = state.get("contact_ids")
                if not isinstance(contact_ids, list) or any(
                    not isinstance(item, str)
                    or item not in contacts
                    or contacts[item].company_id != company.company_id
                    for item in contact_ids
                ):
                    raise ValueError("completed people-search state lacks its persisted contacts")
                continue
            if state.get("state") == "in_flight":
                lifecycle.pause(
                    status="paused_unknown",
                    reason=f"unknown_in_flight:{operation_id}",
                    company_id=company.company_id,
                    stage="people_search",
                )
                return _summary(config, paths, accepted, checkpoint, contacts)
            raise ValueError("unsupported people-search operation state")

        if company.company_id not in {item.company_id for item in _accepted(paths.evaluated)}:
            continue
        if not lifecycle.admit(
            operation_id,
            provider="exa",
            operation="people_search",
            ceiling=config.exa_people_budget_usd,
            reservation_usd=_EXA_PEOPLE_RESERVATION_USD,
            budget_reason="exa_people_budget",
            usage_unknown_reason="exa_people_usage_unknown",
            company_id=company.company_id,
            pending_stage="people_search",
        ):
            return _summary(config, paths, accepted, checkpoint, contacts)

        try:
            result = exa_search(company)
        except DiscoveryProviderError as exc:
            lifecycle.record_usage(exc.usage_event)
            if exc.kind == "budget_exhausted":
                lifecycle.finish(operation_id, state="failed", error_kind=exc.kind)
                lifecycle.pause(
                    status="paused_budget",
                    reason="exa_people_budget",
                    company_id=company.company_id,
                    stage="people_search",
                )
            else:
                lifecycle.pause(
                    status="paused_unknown",
                    reason=f"ambiguous_paid_outcome:{operation_id}",
                    company_id=company.company_id,
                    stage="people_search",
                )
            return _summary(config, paths, accepted, checkpoint, contacts)

        lifecycle.record_usage(result.usage_event)
        selected = select_contacts(
            company,
            result.results,
            limit=config.max_contacts_per_company,
        )
        for contact in selected:
            contacts[contact.contact_id] = contact
        _write_contacts(paths.contacts, contacts)
        lifecycle.finish(
            operation_id,
            fields={"contact_ids": [item.contact_id for item in selected]},
        )

    checkpoint.provider_state["accepted_fingerprint"] = current_fingerprint
    checkpoint.status = "completed"
    checkpoint.pause_reason = None
    checkpoint.pending_company_id = None
    checkpoint.pending_stage = None
    _write_contacts(paths.contacts, contacts)
    publish_usage()
    persist_checkpoint()
    return _summary(config, paths, accepted, checkpoint, contacts)


__all__ = [
    "ContactDiscoveryConfig",
    "ContactDiscoverySummary",
    "run_contact_discovery",
]
