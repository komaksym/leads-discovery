"""Resumable M4 contact discovery, work-email enrichment, and verification pipeline."""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.providers import (
    ApolloContactClient,
    ClayContactClient,
    ContactProviderError,
    ExaPeopleClient,
    InstantlyVerificationClient,
    clay_item_email,
)
from leads_discovery.contacts.selection import normalize_contact_name, select_contacts
from leads_discovery.models import CompanyRecord, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.costs import CostTracker
from leads_discovery.pipeline.state import (
    append_usage_event,
    load_jsonl,
    load_usage_events,
    read_json,
    write_checkpoint,
    write_json_atomic,
    write_jsonl_atomic,
    write_text_atomic,
)

_RUN_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FORMULA_PREFIXES: Final[frozenset[str]] = frozenset("=+-@")
_ARTIFACTS: Final[tuple[str, ...]] = (
    "contacts.jsonl",
    "leads.csv",
    "contact_usage_events.jsonl",
    "contact_usage.json",
    "contact_checkpoint.json",
)
_CSV_COLUMNS: Final[tuple[str, ...]] = (
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


@dataclass(frozen=True, slots=True)
class ContactEnrichmentConfig:
    """Configure one bounded M4 contact-enrichment run."""

    run_id: str
    data_root: Path = Path("data")
    max_contacts_per_company: int = 3
    max_paid_contacts_per_company: int = 2
    exa_people_budget_usd: float | None = None
    clay_max_contacts: int = 10
    apollo_credit_cap: float = 5.0
    instantly_verification_call_cap: int = 5
    execute_live: bool = False


@dataclass(frozen=True, slots=True)
class ContactEnrichmentSummary:
    """Summarize one complete or safely paused M4 publication."""

    run_id: str
    status: str
    accepted_company_count: int
    contact_count: int
    paid_candidate_count: int
    verified_email_count: int
    artifact_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _Paths:
    """Resolve M3 input and separate M4 artifacts below one validated run directory."""

    run_dir: Path
    evaluated: Path
    contacts: Path
    leads: Path
    usage_events: Path
    usage: Path
    checkpoint: Path

    def outputs(self) -> tuple[Path, ...]:
        """Return the five M4 artifacts in stable order."""
        return (self.contacts, self.leads, self.usage_events, self.usage, self.checkpoint)


def _finite_nonnegative(name: str, value: object) -> float:
    """Validate one nonnegative finite numeric quota without accepting booleans."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a nonnegative finite number")
    return float(value)


def _validate_config(config: ContactEnrichmentConfig) -> _Paths:
    """Validate scalar controls, containment, M3 input, and symlink boundaries."""
    if not isinstance(config.run_id, str) or not _RUN_ID.fullmatch(config.run_id):
        raise ValueError("run_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    if (
        isinstance(config.max_contacts_per_company, bool)
        or not isinstance(config.max_contacts_per_company, int)
        or not 1 <= config.max_contacts_per_company <= 3
    ):
        raise ValueError("max_contacts_per_company must be an integer in 1..3")
    if (
        isinstance(config.max_paid_contacts_per_company, bool)
        or not isinstance(config.max_paid_contacts_per_company, int)
        or not 0 <= config.max_paid_contacts_per_company <= 2
        or config.max_paid_contacts_per_company > config.max_contacts_per_company
    ):
        raise ValueError("max_paid_contacts_per_company must be in 0..2 and <= max contacts")
    if (
        isinstance(config.clay_max_contacts, bool)
        or not isinstance(config.clay_max_contacts, int)
        or config.clay_max_contacts < 0
    ):
        raise ValueError("clay_max_contacts must be a nonnegative integer")
    if (
        isinstance(config.instantly_verification_call_cap, bool)
        or not isinstance(config.instantly_verification_call_cap, int)
        or config.instantly_verification_call_cap < 0
    ):
        raise ValueError("instantly_verification_call_cap must be a nonnegative integer")
    if config.exa_people_budget_usd is not None:
        _finite_nonnegative("exa_people_budget_usd", config.exa_people_budget_usd)
    _finite_nonnegative("apollo_credit_cap", config.apollo_credit_cap)

    root = config.data_root.expanduser().resolve()
    candidate = root / config.run_id
    if candidate.is_symlink():
        raise ValueError("run directory must not be a symlink")
    run_dir = candidate.resolve()
    if run_dir.parent != root or not run_dir.is_dir():
        raise ValueError("run directory must exist directly beneath data_root")
    evaluated = run_dir / "companies_evaluated.jsonl"
    if evaluated.is_symlink() or not evaluated.is_file():
        raise ValueError("companies_evaluated.jsonl must be a regular M3 artifact")
    for name in _ARTIFACTS:
        if (run_dir / name).is_symlink():
            raise ValueError(f"artifact path must not be a symlink: {name}")
    return _Paths(
        run_dir=run_dir,
        evaluated=evaluated,
        contacts=run_dir / "contacts.jsonl",
        leads=run_dir / "leads.csv",
        usage_events=run_dir / "contact_usage_events.jsonl",
        usage=run_dir / "contact_usage.json",
        checkpoint=run_dir / "contact_checkpoint.json",
    )


def _load_accepted(path: Path) -> tuple[CompanyRecord, ...]:
    """Load unique completed M3 records and retain only exact accepted decisions."""
    companies: dict[str, CompanyRecord] = {}
    for payload in load_jsonl(path):
        company = CompanyRecord.from_dict(payload)
        if company.company_id in companies:
            raise ValueError("companies_evaluated.jsonl contains duplicate company IDs")
        companies[company.company_id] = company
    if len(companies) > 20:
        raise ValueError("M4 input exceeds the M3 maximum evaluated-company universe")
    accepted: list[CompanyRecord] = []
    for company in companies.values():
        if company.stage_status.get("decision") != "completed":
            raise ValueError("M4 requires completed M3 decision state")
        if company.final_decision == "accepted":
            accepted.append(company)
    accepted.sort(key=lambda item: item.company_id)
    return tuple(accepted)


def _load_contacts(path: Path) -> dict[str, ContactRecord]:
    """Load the current atomic contact snapshot keyed by stable contact ID."""
    contacts: dict[str, ContactRecord] = {}
    for payload in load_jsonl(path):
        contact = ContactRecord.from_dict(payload)
        if contact.contact_id in contacts:
            raise ValueError("contacts.jsonl contains duplicate contact IDs")
        contacts[contact.contact_id] = contact
    return contacts


def _load_checkpoint(paths: _Paths, run_id: str) -> RunCheckpoint:
    """Load or initialize the dedicated M4 checkpoint without touching M2 state."""
    payload = read_json(paths.checkpoint)
    if payload is None:
        return RunCheckpoint(run_id=run_id, provider_state={"operations": {}})
    checkpoint = RunCheckpoint.from_dict(payload)
    if checkpoint.run_id != run_id:
        raise ValueError("contact checkpoint run_id mismatch")
    if not isinstance(checkpoint.provider_state.get("operations", {}), dict):
        raise ValueError("contact checkpoint operations must be an object")
    return checkpoint


def _operations(checkpoint: RunCheckpoint) -> dict[str, Any]:
    """Return the mutable validated operation map owned by the M4 checkpoint."""
    raw = checkpoint.provider_state.setdefault("operations", {})
    if not isinstance(raw, dict):
        raise ValueError("contact checkpoint operations must be an object")
    return cast(dict[str, Any], raw)


def _save_checkpoint(path: Path, checkpoint: RunCheckpoint, status: str, reason: str | None) -> None:
    """Persist one M4 checkpoint transition with a fresh timestamp."""
    checkpoint.status = status
    checkpoint.pause_reason = reason
    checkpoint.updated_at = datetime.now(UTC).isoformat()
    write_checkpoint(path, checkpoint)


def _record_event(path: Path, event: UsageEvent) -> None:
    """Persist one provider usage event before its operation can be marked complete."""
    append_usage_event(path, event)


def _quota_totals(events: list[UsageEvent]) -> tuple[float, int, float]:
    """Replay Apollo credits plus Instantly calls and credits from M4 usage metadata."""
    apollo = 0.0
    instantly_calls = 0
    instantly_credits = 0.0
    for event in events:
        if event.provider == "apollo":
            raw = event.metadata.get("credits_used")
            if raw is None:
                raw = event.metadata.get("credits_reserved", 1.0)
            apollo += _finite_nonnegative("apollo credits", raw)
        elif event.provider == "instantly":
            instantly_calls += event.request_count
            raw = event.metadata.get("credits_used")
            if raw is not None:
                instantly_credits += _finite_nonnegative("instantly credits", raw)
    return apollo, instantly_calls, instantly_credits


def _clay_submitted(events: list[UsageEvent]) -> int:
    """Replay the exact number of contacts submitted to Clay from validated M4 events."""
    total = 0
    for event in events:
        if event.provider != "clay" or event.operation != "work_email_routine_start":
            continue
        raw = event.metadata.get("submitted_contacts")
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ValueError("Clay submitted_contacts usage must be a nonnegative integer")
        total += raw
    return total


def _publish_usage(paths: _Paths) -> None:
    """Rebuild the M4 usage summary from its separate append-only usage ledger."""
    events = load_usage_events(paths.usage_events)
    apollo, instantly_calls, instantly_credits = _quota_totals(events)
    summary = CostTracker(events).summary()
    write_json_atomic(
        paths.usage,
        {
            **summary,
            "quotas": {
                "clay_submitted_contacts": _clay_submitted(events),
                "apollo_credits": round(apollo, 10),
                "instantly_calls": instantly_calls,
                "instantly_credits": round(instantly_credits, 10),
            },
        },
    )


def _safe_csv(value: object) -> str:
    """Render an external cell with the repository's formula-injection protection."""
    if value is None:
        return ""
    text = str(value)
    stripped = text.lstrip()
    if stripped and stripped[0] in _FORMULA_PREFIXES:
        return "'" + text
    return text


def _ordered_contacts(contacts: dict[str, ContactRecord]) -> list[ContactRecord]:
    """Sort contacts by company score, decision rank, normalized name, and stable ID."""
    def key(contact: ContactRecord) -> tuple[bool, float, int, str, str]:
        score = contact.company_final_score
        return (
            score is None,
            -(score if score is not None else 0.0),
            contact.decision_rank,
            normalize_contact_name(contact.full_name),
            contact.contact_id,
        )

    return sorted(contacts.values(), key=key)


def _publish_contacts(paths: _Paths, contacts: dict[str, ContactRecord]) -> None:
    """Atomically replace canonical contacts and the primary human-review CSV."""
    ordered = _ordered_contacts(contacts)
    write_jsonl_atomic(paths.contacts, (contact.to_dict() for contact in ordered))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(_CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for contact in ordered:
        writer.writerow({name: _safe_csv(getattr(contact, name)) for name in _CSV_COLUMNS})
    write_text_atomic(paths.leads, output.getvalue())


def _paid_candidates(
    contacts: dict[str, ContactRecord], config: ContactEnrichmentConfig
) -> list[ContactRecord]:
    """Return only top-two retained rank-1/2 contacts per company in stable order."""
    grouped: dict[str, list[ContactRecord]] = {}
    for contact in _ordered_contacts(contacts):
        grouped.setdefault(contact.company_id, []).append(contact)
    paid: list[ContactRecord] = []
    for company_id in sorted(grouped):
        eligible = [item for item in grouped[company_id] if item.decision_rank in {1, 2}]
        paid.extend(eligible[: config.max_paid_contacts_per_company])
    return paid


def _has_attempt(contact: ContactRecord, provider: str, state: str) -> bool:
    """Return whether one contact has a retained provider attempt in the requested state."""
    return any(
        attempt.get("provider") == provider and attempt.get("state") == state
        for attempt in contact.provider_attempts
    )


def _attempt(contact: ContactRecord, provider: str, operation: str, state: str, **extra: Any) -> None:
    """Append one safe provider-attempt state transition to a contact snapshot."""
    contact.provider_attempts.append(
        {"provider": provider, "operation": operation, "state": state, **extra}
    )


def _summary(
    config: ContactEnrichmentConfig,
    paths: _Paths,
    status: str,
    accepted_count: int,
    contacts: dict[str, ContactRecord],
) -> ContactEnrichmentSummary:
    """Build one detached summary from durable M4 state."""
    paid = _paid_candidates(contacts, config)
    return ContactEnrichmentSummary(
        run_id=config.run_id,
        status=status,
        accepted_company_count=accepted_count,
        contact_count=len(contacts),
        paid_candidate_count=len(paid),
        verified_email_count=sum(
            item.email_verification_status == "verified" for item in contacts.values()
        ),
        artifact_paths=tuple(Path(path) for path in paths.outputs()),
    )


def _pause(
    config: ContactEnrichmentConfig,
    paths: _Paths,
    checkpoint: RunCheckpoint,
    contacts: dict[str, ContactRecord],
    accepted_count: int,
    status: str,
    reason: str,
) -> ContactEnrichmentSummary:
    """Publish all known M4 state and return one durable partial-run summary."""
    _publish_contacts(paths, contacts)
    _publish_usage(paths)
    _save_checkpoint(paths.checkpoint, checkpoint, status, reason)
    return _summary(config, paths, status, accepted_count, contacts)


def run_contact_enrichment(
    config: ContactEnrichmentConfig,
    *,
    exa: ExaPeopleClient,
    clay: ClayContactClient,
    apollo: ApolloContactClient,
    instantly: InstantlyVerificationClient,
) -> ContactEnrichmentSummary:
    """Run or safely resume the artifact-only M4 contact-enrichment stage."""
    if not config.execute_live:
        raise ValueError("run_contact_enrichment requires explicit live execution")
    paths = _validate_config(config)
    accepted = _load_accepted(paths.evaluated)
    checkpoint = _load_checkpoint(paths, config.run_id)
    operations = _operations(checkpoint)
    contacts = _load_contacts(paths.contacts)
    events = load_usage_events(paths.usage_events)

    tracker = CostTracker(events)
    for company in accepted:
        key = f"exa:{company.company_id}"
        state = operations.get(key)
        if isinstance(state, dict) and state.get("state") == "completed":
            continue
        if isinstance(state, dict) and state.get("state") == "in_flight":
            return _pause(
                config, paths, checkpoint, contacts, len(accepted), "paused_unknown", key
            )
        if config.exa_people_budget_usd is not None:
            spend = tracker.provider_estimated_spend("exa")
            if spend is None:
                return _pause(
                    config,
                    paths,
                    checkpoint,
                    contacts,
                    len(accepted),
                    "paused_unknown",
                    "exa_usage_unknown",
                )
            if spend >= config.exa_people_budget_usd:
                return _pause(
                    config,
                    paths,
                    checkpoint,
                    contacts,
                    len(accepted),
                    "paused_budget",
                    "exa_people_budget",
                )
        operations[key] = {"state": "in_flight"}
        _save_checkpoint(paths.checkpoint, checkpoint, "running", None)
        try:
            result = exa.search(company)
        except ContactProviderError as error:
            _record_event(paths.usage_events, error.usage_event)
            status = "paused_budget" if error.kind == "budget_exhausted" else "paused_unknown"
            return _pause(
                config, paths, checkpoint, contacts, len(accepted), status, key
            )
        _record_event(paths.usage_events, result.usage_event)
        tracker.record(result.usage_event)
        selected = select_contacts(
            company, result.results, limit=config.max_contacts_per_company
        )
        for contact in selected:
            contacts[contact.contact_id] = contact
        _publish_contacts(paths, contacts)
        _publish_usage(paths)
        operations[key] = {
            "state": "completed",
            "contact_ids": [item.contact_id for item in selected],
        }
        _save_checkpoint(paths.checkpoint, checkpoint, "running", None)

    paid = _paid_candidates(contacts, config)
    clay_state = operations.get("clay:batch")
    if not isinstance(clay_state, dict) or clay_state.get("state") != "completed":
        pending = [item for item in paid if not _has_attempt(item, "clay", "completed")]
        pending = pending[: config.clay_max_contacts]
        if pending:
            if (
                isinstance(clay_state, dict)
                and clay_state.get("state") == "in_flight"
                and not clay_state.get("routine_run_id")
            ):
                return _pause(
                    config,
                    paths,
                    checkpoint,
                    contacts,
                    len(accepted),
                    "paused_unknown",
                    "clay_start_unknown",
                )
            run_id = clay_state.get("routine_run_id") if isinstance(clay_state, dict) else None
            if not isinstance(run_id, str) or not run_id:
                operations["clay:batch"] = {
                    "state": "in_flight",
                    "contact_ids": [item.contact_id for item in pending],
                }
                for contact in pending:
                    _attempt(contact, "clay", "work_email_routine", "in_flight")
                _publish_contacts(paths, contacts)
                _save_checkpoint(paths.checkpoint, checkpoint, "running", None)
                try:
                    started = clay.start(pending)
                except ContactProviderError as error:
                    _record_event(paths.usage_events, error.usage_event)
                    status = (
                        "paused_budget"
                        if error.kind == "budget_exhausted"
                        else "paused_unknown"
                    )
                    return _pause(
                        config,
                        paths,
                        checkpoint,
                        contacts,
                        len(accepted),
                        status,
                        "clay_start",
                    )
                _record_event(paths.usage_events, started.usage_event)
                operations["clay:batch"] = {
                    "state": "pending",
                    "routine_run_id": started.routine_run_id,
                    "contact_ids": [item.contact_id for item in pending],
                }
                return _pause(
                    config,
                    paths,
                    checkpoint,
                    contacts,
                    len(accepted),
                    "paused_pending",
                    "clay_pending",
                )
            try:
                result = clay.results(run_id)
            except ContactProviderError as error:
                _record_event(paths.usage_events, error.usage_event)
                status = (
                    "paused_budget" if error.kind == "budget_exhausted" else "paused_pending"
                )
                return _pause(
                    config,
                    paths,
                    checkpoint,
                    contacts,
                    len(accepted),
                    status,
                    "clay_pending",
                )
            _record_event(paths.usage_events, result.usage_event)
            if result.status == "pending":
                return _pause(
                    config,
                    paths,
                    checkpoint,
                    contacts,
                    len(accepted),
                    "paused_pending",
                    "clay_pending",
                )
            expected = cast(list[str], clay_state.get("contact_ids", []))
            by_id = {
                str(item.get("id")): item
                for item in result.items
                if item.get("id") is not None
            }
            for contact_id in expected:
                contact = contacts.get(contact_id)
                if contact is None:
                    raise ValueError("Clay result references an unknown contact")
                email = clay_item_email(by_id.get(contact_id, {}))
                if email is not None:
                    contact.work_email = email
                    contact.email_source = "clay"
                _attempt(
                    contact,
                    "clay",
                    "work_email_routine",
                    "completed",
                    routine_run_id=run_id,
                )
            operations["clay:batch"] = {**clay_state, "state": "completed"}
            _publish_contacts(paths, contacts)
            _publish_usage(paths)
            _save_checkpoint(paths.checkpoint, checkpoint, "running", None)

    paid = _paid_candidates(contacts, config)
    clay_cap_exhausted = any(not _has_attempt(item, "clay", "completed") for item in paid)

    events = load_usage_events(paths.usage_events)
    apollo_used, _, _ = _quota_totals(events)
    for contact in paid:
        if contact.work_email is not None or not _has_attempt(contact, "clay", "completed"):
            continue
        key = f"apollo:{contact.contact_id}"
        state = operations.get(key)
        if isinstance(state, dict) and state.get("state") == "completed":
            continue
        if isinstance(state, dict) and state.get("state") == "in_flight":
            return _pause(
                config, paths, checkpoint, contacts, len(accepted), "paused_unknown", key
            )
        if apollo_used + 1.0 > config.apollo_credit_cap:
            return _pause(
                config,
                paths,
                checkpoint,
                contacts,
                len(accepted),
                "paused_budget",
                "apollo_credit_cap",
            )
        operations[key] = {"state": "in_flight", "credits_reserved": 1.0}
        _attempt(contact, "apollo", "people_enrichment", "in_flight")
        _publish_contacts(paths, contacts)
        _save_checkpoint(paths.checkpoint, checkpoint, "running", None)
        try:
            result = apollo.enrich(contact)
        except ContactProviderError as error:
            event = error.usage_event
            event.metadata["credits_reserved"] = 1.0
            _record_event(paths.usage_events, event)
            status = "paused_budget" if error.kind == "budget_exhausted" else "paused_unknown"
            return _pause(
                config, paths, checkpoint, contacts, len(accepted), status, key
            )
        used = _finite_nonnegative(
            "apollo credits",
            result.credits_used if result.credits_used is not None else 1.0,
        )
        result.usage_event.metadata["credits_used"] = used
        if result.credits_used is None:
            result.usage_event.metadata["credits_reserved"] = 1.0
        _record_event(paths.usage_events, result.usage_event)
        apollo_used += used
        if result.work_email is not None:
            contact.work_email = result.work_email
            contact.email_source = "apollo"
        _attempt(contact, "apollo", "people_enrichment", "completed")
        operations[key] = {"state": "completed", "credits_used": used}
        _publish_contacts(paths, contacts)
        _publish_usage(paths)
        _save_checkpoint(paths.checkpoint, checkpoint, "running", None)

    events = load_usage_events(paths.usage_events)
    _, instantly_calls, _ = _quota_totals(events)
    for contact in paid:
        if contact.work_email is None:
            continue
        key = f"instantly:{contact.contact_id}"
        state = operations.get(key)
        if isinstance(state, dict) and state.get("state") == "completed":
            continue
        pending = isinstance(state, dict) and state.get("state") == "pending"
        if isinstance(state, dict) and state.get("state") == "in_flight":
            return _pause(
                config, paths, checkpoint, contacts, len(accepted), "paused_unknown", key
            )
        if instantly_calls >= config.instantly_verification_call_cap:
            return _pause(
                config,
                paths,
                checkpoint,
                contacts,
                len(accepted),
                "paused_budget",
                "instantly_call_cap",
            )
        email = contact.work_email
        try:
            if pending:
                persisted_email = state.get("email") if isinstance(state, dict) else None
                if persisted_email != email:
                    raise ValueError("pending Instantly email does not match contact email")
                result = instantly.get(email)
            else:
                operations[key] = {"state": "in_flight", "email": email}
                _attempt(contact, "instantly", "email_verification", "in_flight")
                _publish_contacts(paths, contacts)
                _save_checkpoint(paths.checkpoint, checkpoint, "running", None)
                result = instantly.create(email)
        except ContactProviderError as error:
            _record_event(paths.usage_events, error.usage_event)
            if error.kind == "budget_exhausted":
                status = "paused_budget"
            elif pending:
                status = "paused_pending"
            else:
                status = "paused_unknown"
            return _pause(
                config, paths, checkpoint, contacts, len(accepted), status, key
            )
        _record_event(paths.usage_events, result.usage_event)
        instantly_calls += result.usage_event.request_count
        contact.email_verification_status = result.status
        if result.status == "pending":
            operations[key] = {"state": "pending", "email": email}
            _attempt(contact, "instantly", "email_verification", "pending")
            return _pause(
                config,
                paths,
                checkpoint,
                contacts,
                len(accepted),
                "paused_pending",
                key,
            )
        operations[key] = {"state": "completed", "email": email, "status": result.status}
        _attempt(
            contact,
            "instantly",
            "email_verification",
            "completed",
            status=result.status,
        )
        _publish_contacts(paths, contacts)
        _publish_usage(paths)
        _save_checkpoint(paths.checkpoint, checkpoint, "running", None)

    if clay_cap_exhausted:
        return _pause(
            config,
            paths,
            checkpoint,
            contacts,
            len(accepted),
            "paused_budget",
            "clay_max_contacts",
        )

    _publish_contacts(paths, contacts)
    _publish_usage(paths)
    _save_checkpoint(paths.checkpoint, checkpoint, "completed", None)
    return _summary(config, paths, "completed", len(accepted), contacts)


__all__ = [
    "ContactEnrichmentConfig",
    "ContactEnrichmentSummary",
    "run_contact_enrichment",
]
