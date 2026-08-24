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
from leads_discovery.contacts.selection import (
    contact_decision_order_key,
    normalize_contact_name,
    select_contacts,
)
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
_CHECKPOINT_STATUSES: Final[frozenset[str]] = frozenset(
    {"running", "paused_budget", "paused_unknown", "paused_pending", "completed"}
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


def _accepted_ids(path: Path) -> frozenset[str]:
    """Reload the latest canonical M3 state and return only currently accepted company IDs."""
    return frozenset(company.company_id for company in _load_accepted(path))


def _load_contacts(path: Path) -> dict[str, ContactRecord]:
    """Load the current atomic contact snapshot keyed by stable contact ID."""
    contacts: dict[str, ContactRecord] = {}
    for payload in load_jsonl(path):
        contact = ContactRecord.from_dict(payload)
        if contact.contact_id in contacts:
            raise ValueError("contacts.jsonl contains duplicate contact IDs")
        contacts[contact.contact_id] = contact
    return contacts


def _require_exact_keys(operation: str, value: dict[str, Any], expected: set[str]) -> None:
    """Reject checkpoint operation payloads whose shape is outside the persisted state machine."""
    if set(value) != expected:
        raise ValueError(f"malformed contact checkpoint operation: {operation}")


def _contact_ids(operation: str, value: object) -> list[str]:
    """Validate one duplicate-free list of persisted contact identifiers."""
    if not isinstance(value, list):
        raise ValueError(f"malformed contact checkpoint operation: {operation}")
    ids: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError(f"malformed contact checkpoint operation: {operation}")
        ids.append(item)
    if len(ids) != len(set(ids)):
        raise ValueError(f"malformed contact checkpoint operation: {operation}")
    return ids


def _nonblank_string(operation: str, value: object) -> str:
    """Validate one nonblank string stored in checkpoint operation state."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"malformed contact checkpoint operation: {operation}")
    return value


def _validate_operation(operation: str, value: object) -> None:
    """Validate one operation against the exact M4 persisted replay state machine."""
    if not isinstance(value, dict):
        raise ValueError(f"malformed contact checkpoint operation: {operation}")
    state = value.get("state")
    if not isinstance(state, str):
        raise ValueError(f"malformed contact checkpoint operation: {operation}")

    if operation.startswith("exa:") and operation != "exa:":
        if state == "in_flight":
            _require_exact_keys(operation, value, {"state"})
            return
        if state == "completed":
            _require_exact_keys(operation, value, {"state", "contact_ids"})
            _contact_ids(operation, value["contact_ids"])
            return
        raise ValueError(f"unsupported contact checkpoint state: {operation}:{state}")

    if operation == "clay:batch":
        if state == "in_flight":
            _require_exact_keys(operation, value, {"state", "contact_ids"})
            _contact_ids(operation, value["contact_ids"])
            return
        if state in {"pending", "completed"}:
            _require_exact_keys(
                operation,
                value,
                {"state", "routine_run_id", "contact_ids"},
            )
            _nonblank_string(operation, value["routine_run_id"])
            _contact_ids(operation, value["contact_ids"])
            return
        raise ValueError(f"unsupported contact checkpoint state: {operation}:{state}")

    if operation.startswith("apollo:") and operation != "apollo:":
        if state == "in_flight":
            _require_exact_keys(operation, value, {"state", "credits_reserved"})
            if _finite_nonnegative("apollo credits_reserved", value["credits_reserved"]) != 1.0:
                raise ValueError(f"malformed contact checkpoint operation: {operation}")
            return
        if state == "completed":
            _require_exact_keys(operation, value, {"state", "credits_used"})
            _finite_nonnegative("apollo credits_used", value["credits_used"])
            return
        raise ValueError(f"unsupported contact checkpoint state: {operation}:{state}")

    if operation.startswith("instantly:") and operation != "instantly:":
        if state in {"in_flight", "pending"}:
            _require_exact_keys(operation, value, {"state", "email"})
            _nonblank_string(operation, value["email"])
            return
        if state == "completed":
            _require_exact_keys(operation, value, {"state", "email", "status"})
            _nonblank_string(operation, value["email"])
            if value["status"] not in {"verified", "invalid"}:
                raise ValueError(f"malformed contact checkpoint operation: {operation}")
            return
        raise ValueError(f"unsupported contact checkpoint state: {operation}:{state}")

    raise ValueError(f"unsupported contact checkpoint operation: {operation}")


def _validate_operations(operations: dict[str, Any]) -> None:
    """Reject every unknown or malformed persisted operation before any provider is used."""
    for operation, value in operations.items():
        if not isinstance(operation, str) or not operation:
            raise ValueError("contact checkpoint operation names must be nonblank strings")
        _validate_operation(operation, value)


def _load_checkpoint(paths: _Paths, run_id: str) -> RunCheckpoint:
    """Load and strictly validate the dedicated M4 checkpoint without touching M2 state."""
    payload = read_json(paths.checkpoint)
    if payload is None:
        return RunCheckpoint(run_id=run_id, provider_state={"operations": {}})
    checkpoint = RunCheckpoint.from_dict(payload)
    if checkpoint.run_id != run_id:
        raise ValueError("contact checkpoint run_id mismatch")
    if checkpoint.status not in _CHECKPOINT_STATUSES:
        raise ValueError("contact checkpoint status is unsupported")
    raw = checkpoint.provider_state.get("operations", {})
    if not isinstance(raw, dict):
        raise ValueError("contact checkpoint operations must be an object")
    _validate_operations(cast(dict[str, Any], raw))
    return checkpoint


def _operations(checkpoint: RunCheckpoint) -> dict[str, Any]:
    """Return the mutable strictly validated operation map owned by the M4 checkpoint."""
    raw = checkpoint.provider_state.setdefault("operations", {})
    if not isinstance(raw, dict):
        raise ValueError("contact checkpoint operations must be an object")
    operations = cast(dict[str, Any], raw)
    _validate_operations(operations)
    return operations


def _validate_operation_references(
    operations: dict[str, Any], contacts: dict[str, ContactRecord]
) -> None:
    """Reject checkpoint contact references that cannot be reconciled with durable artifacts."""
    for operation, value in operations.items():
        state = cast(str, value["state"])
        if operation.startswith("exa:") and state == "completed":
            company_id = operation.removeprefix("exa:")
            for contact_id in _contact_ids(operation, value["contact_ids"]):
                contact = contacts.get(contact_id)
                if contact is None or contact.company_id != company_id:
                    raise ValueError(f"malformed contact checkpoint operation: {operation}")
        elif operation == "clay:batch":
            for contact_id in _contact_ids(operation, value["contact_ids"]):
                if contact_id not in contacts:
                    raise ValueError(f"malformed contact checkpoint operation: {operation}")
        elif operation.startswith(("apollo:", "instantly:")):
            contact_id = operation.split(":", 1)[1]
            if contact_id not in contacts:
                raise ValueError(f"malformed contact checkpoint operation: {operation}")


def _require_completed_exa(
    operations: dict[str, Any], contacts: dict[str, ContactRecord], contact_ids: list[str]
) -> None:
    """Require later-provider contacts to retain their completed Exa selection provenance."""
    for contact_id in contact_ids:
        contact = contacts[contact_id]
        operation = f"exa:{contact.company_id}"
        value = operations.get(operation)
        if not isinstance(value, dict) or value.get("state") != "completed":
            raise ValueError("later M4 provider state lacks completed Exa prerequisite")
        selected = _contact_ids(operation, value.get("contact_ids"))
        if contact_id not in selected:
            raise ValueError("later M4 provider state is inconsistent with Exa selection")


def _require_completed_clay(operations: dict[str, Any], contact_ids: list[str]) -> None:
    """Require Apollo or Instantly state to retain the completed Clay batch prerequisite."""
    value = operations.get("clay:batch")
    if not isinstance(value, dict) or value.get("state") != "completed":
        raise ValueError("later M4 provider state lacks completed Clay prerequisite")
    submitted = _contact_ids("clay:batch", value.get("contact_ids"))
    if any(contact_id not in submitted for contact_id in contact_ids):
        raise ValueError("later M4 provider state is inconsistent with Clay batch history")


def _validate_provider_prerequisites(
    operations: dict[str, Any], contacts: dict[str, ContactRecord]
) -> None:
    """Require durable later-stage state to retain the earlier paid-work history it proves."""
    for operation, value in operations.items():
        if operation == "clay:batch":
            ids = _contact_ids(operation, value["contact_ids"])
            _require_completed_exa(operations, contacts, ids)
        elif operation.startswith(("apollo:", "instantly:")):
            contact_id = operation.split(":", 1)[1]
            _require_completed_exa(operations, contacts, [contact_id])
            _require_completed_clay(operations, [contact_id])


def _require_operation_state(
    operations: dict[str, Any], operation: str, allowed: frozenset[str]
) -> None:
    """Require pause evidence to reference one durable operation in an allowed replay state."""
    value = operations.get(operation)
    if not isinstance(value, dict) or value.get("state") not in allowed:
        raise ValueError("contact checkpoint pause evidence is missing or inconsistent")


def _validate_checkpoint_consistency(
    checkpoint: RunCheckpoint,
    operations: dict[str, Any],
    events: list[UsageEvent],
) -> None:
    """Require top-level M4 status and pause metadata to agree with durable replay evidence."""
    if checkpoint.pending_company_id is not None or checkpoint.pending_stage is not None:
        raise ValueError("M4 checkpoint must not use M2 pending company/stage fields")

    reason = checkpoint.pause_reason
    if checkpoint.status in {"running", "completed"}:
        if reason is not None:
            raise ValueError("active/completed M4 checkpoint must not have a pause reason")
        if checkpoint.status == "completed" and any(
            cast(str, value["state"]) in {"in_flight", "pending"}
            for value in operations.values()
        ):
            raise ValueError("completed M4 checkpoint contains unfinished provider work")
        if checkpoint.status == "running" and any(
            cast(str, value["state"]) == "pending" for value in operations.values()
        ):
            raise ValueError("running M4 checkpoint contains pending async provider work")
        return

    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("paused M4 checkpoint requires a nonblank pause reason")

    if checkpoint.status == "paused_unknown":
        if reason == "exa_usage_unknown":
            if CostTracker(events).provider_estimated_spend("exa") is not None:
                raise ValueError("Exa usage-unknown pause lacks incomplete Exa usage evidence")
            return
        if reason in {"clay_start", "clay_start_unknown"}:
            _require_operation_state(operations, "clay:batch", frozenset({"in_flight"}))
            return
        if reason == "clay_authorization_changed":
            _require_operation_state(operations, "clay:batch", frozenset({"pending"}))
            return
        if reason.startswith(("exa:", "apollo:", "instantly:")):
            _require_operation_state(operations, reason, frozenset({"in_flight"}))
            return
        raise ValueError("paused_unknown M4 checkpoint has unsupported pause evidence")

    if checkpoint.status == "paused_pending":
        if reason == "clay_pending":
            _require_operation_state(operations, "clay:batch", frozenset({"pending"}))
            return
        if reason.startswith("instantly:"):
            _require_operation_state(operations, reason, frozenset({"pending"}))
            return
        raise ValueError("paused_pending M4 checkpoint has unsupported pause evidence")

    if checkpoint.status == "paused_budget":
        if reason in {
            "exa_people_budget",
            "clay_max_contacts",
            "apollo_credit_cap",
            "instantly_call_cap",
        }:
            return
        if reason == "clay_start":
            _require_operation_state(operations, "clay:batch", frozenset({"in_flight"}))
            return
        if reason == "clay_pending":
            _require_operation_state(operations, "clay:batch", frozenset({"pending"}))
            return
        if reason.startswith(("exa:", "apollo:")):
            _require_operation_state(operations, reason, frozenset({"in_flight"}))
            return
        if reason.startswith("instantly:"):
            _require_operation_state(
                operations, reason, frozenset({"in_flight", "pending"})
            )
            return
        raise ValueError("paused_budget M4 checkpoint has unsupported pause evidence")

    raise AssertionError("validated M4 checkpoint status is unreachable")


def _save_checkpoint(
    path: Path, checkpoint: RunCheckpoint, status: str, reason: str | None
) -> None:
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


def _usage_payload(events: list[UsageEvent]) -> dict[str, Any]:
    """Build the deterministic derived M4 usage summary from authoritative events."""
    apollo, instantly_calls, instantly_credits = _quota_totals(events)
    return {
        **CostTracker(events).summary(),
        "quotas": {
            "clay_submitted_contacts": _clay_submitted(events),
            "apollo_credits": round(apollo, 10),
            "instantly_calls": instantly_calls,
            "instantly_credits": round(instantly_credits, 10),
        },
    }


def _publish_usage(paths: _Paths) -> None:
    """Rebuild the M4 usage summary from its separate append-only usage ledger."""
    write_json_atomic(paths.usage, _usage_payload(load_usage_events(paths.usage_events)))


def _repair_usage_summary(paths: _Paths, events: list[UsageEvent]) -> None:
    """Repair only a missing or corrupted derived usage summary on completed reruns."""
    expected = _usage_payload(events)
    try:
        current = read_json(paths.usage)
    except ValueError:
        current = None
    if current != expected:
        write_json_atomic(paths.usage, expected)


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
    contacts: dict[str, ContactRecord],
    config: ContactEnrichmentConfig,
    accepted_company_ids: frozenset[str],
) -> list[ContactRecord]:
    """Return paid candidates only for companies accepted in the latest canonical M3 state."""
    grouped: dict[str, list[ContactRecord]] = {}
    for contact in contacts.values():
        if contact.company_id not in accepted_company_ids:
            continue
        grouped.setdefault(contact.company_id, []).append(contact)
    paid: list[ContactRecord] = []
    for company_id in sorted(grouped):
        eligible = sorted(
            (item for item in grouped[company_id] if item.decision_rank in {1, 2}),
            key=contact_decision_order_key,
        )
        paid.extend(eligible[: config.max_paid_contacts_per_company])
    return paid


def _current_paid_candidates(
    paths: _Paths,
    contacts: dict[str, ContactRecord],
    config: ContactEnrichmentConfig,
) -> list[ContactRecord]:
    """Reload M3 authorization and derive the currently authorized paid-contact set."""
    return _paid_candidates(contacts, config, _accepted_ids(paths.evaluated))


def _has_attempt(contact: ContactRecord, provider: str, state: str) -> bool:
    """Return whether one contact has a retained provider attempt in the requested state."""
    return any(
        attempt.get("provider") == provider and attempt.get("state") == state
        for attempt in contact.provider_attempts
    )


def _attempt(
    contact: ContactRecord, provider: str, operation: str, state: str, **extra: Any
) -> None:
    """Append one safe provider-attempt state transition to a contact snapshot."""
    contact.provider_attempts.append(
        {"provider": provider, "operation": operation, "state": state, **extra}
    )


def _summary(
    config: ContactEnrichmentConfig,
    paths: _Paths,
    status: str,
    contacts: dict[str, ContactRecord],
) -> ContactEnrichmentSummary:
    """Build one detached summary using the latest M3 accepted-company authorization."""
    accepted_ids = _accepted_ids(paths.evaluated)
    paid = _paid_candidates(contacts, config, accepted_ids)
    return ContactEnrichmentSummary(
        run_id=config.run_id,
        status=status,
        accepted_company_count=len(accepted_ids),
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
    status: str,
    reason: str,
) -> ContactEnrichmentSummary:
    """Publish all known M4 state and return one durable partial-run summary."""
    _publish_contacts(paths, contacts)
    _publish_usage(paths)
    _save_checkpoint(paths.checkpoint, checkpoint, status, reason)
    return _summary(config, paths, status, contacts)


def _load_runtime_state(
    config: ContactEnrichmentConfig,
) -> tuple[
    _Paths,
    tuple[CompanyRecord, ...],
    RunCheckpoint,
    dict[str, Any],
    dict[str, ContactRecord],
    list[UsageEvent],
]:
    """Load and validate every durable M3/M4 input before any provider may be used."""
    paths = _validate_config(config)
    accepted = _load_accepted(paths.evaluated)
    checkpoint = _load_checkpoint(paths, config.run_id)
    operations = _operations(checkpoint)
    contacts = _load_contacts(paths.contacts)
    _validate_operation_references(operations, contacts)
    events = load_usage_events(paths.usage_events)
    _validate_checkpoint_consistency(checkpoint, operations, events)
    if checkpoint.status != "paused_unknown":
        _validate_provider_prerequisites(operations, contacts)
    return paths, accepted, checkpoint, operations, contacts, events


def validate_contact_enrichment_state(config: ContactEnrichmentConfig) -> None:
    """Preflight persisted M4 state so malformed replay data fails before provider construction."""
    _load_runtime_state(config)


def _authorized_contact_ids(
    paths: _Paths,
    contacts: dict[str, ContactRecord],
    contact_ids: list[str],
) -> bool:
    """Return whether every persisted contact is still authorized by current M3 acceptance."""
    accepted = _accepted_ids(paths.evaluated)
    return all(contacts[contact_id].company_id in accepted for contact_id in contact_ids)


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
    paths, accepted, checkpoint, operations, contacts, events = _load_runtime_state(config)
    if checkpoint.status == "completed":
        _repair_usage_summary(paths, events)
        return _summary(config, paths, "completed", contacts)
    if checkpoint.status == "paused_unknown":
        return _summary(config, paths, "paused_unknown", contacts)

    tracker = CostTracker(events)
    for company in accepted:
        key = f"exa:{company.company_id}"
        state = operations.get(key)
        if state is not None:
            state_name = cast(str, state["state"])
            if state_name == "completed":
                continue
            if state_name == "in_flight":
                return _pause(config, paths, checkpoint, contacts, "paused_unknown", key)
            raise AssertionError("validated Exa state is unreachable")
        if company.company_id not in _accepted_ids(paths.evaluated):
            continue
        if config.exa_people_budget_usd is not None:
            spend = tracker.provider_estimated_spend("exa")
            if spend is None:
                return _pause(
                    config,
                    paths,
                    checkpoint,
                    contacts,
                    "paused_unknown",
                    "exa_usage_unknown",
                )
            if spend >= config.exa_people_budget_usd:
                return _pause(
                    config,
                    paths,
                    checkpoint,
                    contacts,
                    "paused_budget",
                    "exa_people_budget",
                )
        operations[key] = {"state": "in_flight"}
        _save_checkpoint(paths.checkpoint, checkpoint, "running", None)
        try:
            exa_result = exa.search(company)
        except ContactProviderError as error:
            _record_event(paths.usage_events, error.usage_event)
            status = "paused_budget" if error.kind == "budget_exhausted" else "paused_unknown"
            return _pause(config, paths, checkpoint, contacts, status, key)
        _record_event(paths.usage_events, exa_result.usage_event)
        tracker.record(exa_result.usage_event)
        selected = select_contacts(
            company, exa_result.results, limit=config.max_contacts_per_company
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

    paid = _current_paid_candidates(paths, contacts, config)
    clay_state = operations.get("clay:batch")
    if clay_state is not None:
        clay_state_name = cast(str, clay_state["state"])
        if clay_state_name == "in_flight":
            return _pause(
                config,
                paths,
                checkpoint,
                contacts,
                "paused_unknown",
                "clay_start_unknown",
            )
        if clay_state_name == "pending":
            expected = _contact_ids("clay:batch", clay_state["contact_ids"])
            if not _authorized_contact_ids(paths, contacts, expected):
                return _pause(
                    config,
                    paths,
                    checkpoint,
                    contacts,
                    "paused_unknown",
                    "clay_authorization_changed",
                )
            run_id = cast(str, clay_state["routine_run_id"])
            try:
                clay_result = clay.results(run_id)
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
                    status,
                    "clay_pending",
                )
            _record_event(paths.usage_events, clay_result.usage_event)
            if clay_result.status == "pending":
                return _pause(
                    config,
                    paths,
                    checkpoint,
                    contacts,
                    "paused_pending",
                    "clay_pending",
                )
            by_id = {
                str(item.get("id")): item
                for item in clay_result.items
                if item.get("id") is not None
            }
            for contact_id in expected:
                contact = contacts[contact_id]
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
        elif clay_state_name != "completed":
            raise AssertionError("validated Clay state is unreachable")
    else:
        clay_pending = [
            item for item in paid if not _has_attempt(item, "clay", "completed")
        ]
        clay_pending = clay_pending[: config.clay_max_contacts]
        if clay_pending:
            current_ids = _accepted_ids(paths.evaluated)
            clay_pending = [item for item in clay_pending if item.company_id in current_ids]
        if clay_pending:
            operations["clay:batch"] = {
                "state": "in_flight",
                "contact_ids": [item.contact_id for item in clay_pending],
            }
            for contact in clay_pending:
                _attempt(contact, "clay", "work_email_routine", "in_flight")
            _publish_contacts(paths, contacts)
            _save_checkpoint(paths.checkpoint, checkpoint, "running", None)
            try:
                started = clay.start(clay_pending)
            except ContactProviderError as error:
                _record_event(paths.usage_events, error.usage_event)
                status = (
                    "paused_budget" if error.kind == "budget_exhausted" else "paused_unknown"
                )
                return _pause(
                    config,
                    paths,
                    checkpoint,
                    contacts,
                    status,
                    "clay_start",
                )
            _record_event(paths.usage_events, started.usage_event)
            operations["clay:batch"] = {
                "state": "pending",
                "routine_run_id": started.routine_run_id,
                "contact_ids": [item.contact_id for item in clay_pending],
            }
            return _pause(
                config,
                paths,
                checkpoint,
                contacts,
                "paused_pending",
                "clay_pending",
            )

    paid = _current_paid_candidates(paths, contacts, config)
    clay_cap_exhausted = any(not _has_attempt(item, "clay", "completed") for item in paid)

    events = load_usage_events(paths.usage_events)
    apollo_used, _, _ = _quota_totals(events)
    for contact in paid:
        if contact.work_email is not None or not _has_attempt(contact, "clay", "completed"):
            continue
        key = f"apollo:{contact.contact_id}"
        state = operations.get(key)
        if state is not None:
            state_name = cast(str, state["state"])
            if state_name == "completed":
                continue
            if state_name == "in_flight":
                return _pause(config, paths, checkpoint, contacts, "paused_unknown", key)
            raise AssertionError("validated Apollo state is unreachable")
        if contact.company_id not in _accepted_ids(paths.evaluated):
            continue
        if apollo_used + 1.0 > config.apollo_credit_cap:
            return _pause(
                config,
                paths,
                checkpoint,
                contacts,
                "paused_budget",
                "apollo_credit_cap",
            )
        operations[key] = {"state": "in_flight", "credits_reserved": 1.0}
        _attempt(contact, "apollo", "people_enrichment", "in_flight")
        _publish_contacts(paths, contacts)
        _save_checkpoint(paths.checkpoint, checkpoint, "running", None)
        try:
            apollo_result = apollo.enrich(contact)
        except ContactProviderError as error:
            event = error.usage_event
            event.metadata["credits_reserved"] = 1.0
            _record_event(paths.usage_events, event)
            status = "paused_budget" if error.kind == "budget_exhausted" else "paused_unknown"
            return _pause(config, paths, checkpoint, contacts, status, key)
        used = _finite_nonnegative(
            "apollo credits",
            apollo_result.credits_used if apollo_result.credits_used is not None else 1.0,
        )
        apollo_result.usage_event.metadata["credits_used"] = used
        if apollo_result.credits_used is None:
            apollo_result.usage_event.metadata["credits_reserved"] = 1.0
        _record_event(paths.usage_events, apollo_result.usage_event)
        apollo_used += used
        if apollo_result.work_email is not None:
            contact.work_email = apollo_result.work_email
            contact.email_source = "apollo"
        _attempt(contact, "apollo", "people_enrichment", "completed")
        operations[key] = {"state": "completed", "credits_used": used}
        _publish_contacts(paths, contacts)
        _publish_usage(paths)
        _save_checkpoint(paths.checkpoint, checkpoint, "running", None)

    paid = _current_paid_candidates(paths, contacts, config)
    events = load_usage_events(paths.usage_events)
    _, instantly_calls, _ = _quota_totals(events)
    for contact in paid:
        if contact.work_email is None:
            continue
        key = f"instantly:{contact.contact_id}"
        state = operations.get(key)
        if state is not None:
            state_name = cast(str, state["state"])
            if state_name == "completed":
                continue
            if state_name == "in_flight":
                return _pause(config, paths, checkpoint, contacts, "paused_unknown", key)
            if state_name != "pending":
                raise AssertionError("validated Instantly state is unreachable")
        if contact.company_id not in _accepted_ids(paths.evaluated):
            continue
        if instantly_calls >= config.instantly_verification_call_cap:
            return _pause(
                config,
                paths,
                checkpoint,
                contacts,
                "paused_budget",
                "instantly_call_cap",
            )
        email = contact.work_email
        is_pending = state is not None and cast(str, state["state"]) == "pending"
        try:
            if is_pending:
                persisted_state = cast(dict[str, Any], state)
                persisted_email = cast(str, persisted_state["email"])
                if persisted_email != email:
                    raise ValueError("pending Instantly email does not match contact email")
                verification = instantly.get(email)
            else:
                operations[key] = {"state": "in_flight", "email": email}
                _attempt(contact, "instantly", "email_verification", "in_flight")
                _publish_contacts(paths, contacts)
                _save_checkpoint(paths.checkpoint, checkpoint, "running", None)
                verification = instantly.create(email)
        except ContactProviderError as error:
            _record_event(paths.usage_events, error.usage_event)
            if error.kind == "budget_exhausted":
                status = "paused_budget"
            elif is_pending:
                status = "paused_pending"
            else:
                status = "paused_unknown"
            return _pause(config, paths, checkpoint, contacts, status, key)
        _record_event(paths.usage_events, verification.usage_event)
        instantly_calls += verification.usage_event.request_count
        contact.email_verification_status = verification.status
        if verification.status == "pending":
            operations[key] = {"state": "pending", "email": email}
            _attempt(contact, "instantly", "email_verification", "pending")
            return _pause(
                config,
                paths,
                checkpoint,
                contacts,
                "paused_pending",
                key,
            )
        operations[key] = {
            "state": "completed",
            "email": email,
            "status": verification.status,
        }
        _attempt(
            contact,
            "instantly",
            "email_verification",
            "completed",
            status=verification.status,
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
            "paused_budget",
            "clay_max_contacts",
        )

    _publish_contacts(paths, contacts)
    _publish_usage(paths)
    _save_checkpoint(paths.checkpoint, checkpoint, "completed", None)
    return _summary(config, paths, "completed", contacts)


__all__ = [
    "ContactEnrichmentConfig",
    "ContactEnrichmentSummary",
    "run_contact_enrichment",
    "validate_contact_enrichment_state",
]
