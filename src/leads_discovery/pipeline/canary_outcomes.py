"""Deterministic sanitized evidence model for the fixed production canary."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.providers import usable_work_email
from leads_discovery.models import CompanyRecord, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.canary_paid_operations import CanaryPaidOperations
from leads_discovery.pipeline.contact_enrichment import (
    ContactEnrichmentConfig,
    validate_contact_enrichment_state,
)
from leads_discovery.pipeline.state import (
    load_jsonl,
    load_usage_events,
    read_json,
    write_json_atomic,
)

Outcome = Literal["success", "inconclusive", "failure"]
CoverageSource = Literal["normal", "coverage_only"]

_RUN_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REPORT_NAME: Final[str] = "canary_coverage_report.json"
_REQUIRED_INTEGRATIONS: Final[tuple[str, ...]] = (
    "exa_discovery",
    "exa_research",
    "deepseek",
    "exa_people",
    "clay",
    "apollo",
    "instantly",
)
_PIPELINE_FAILURE_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "normal_state_missing",
        "normal_state_invalid",
        "normal_pipeline_failed",
        "normal_budget_blocked",
        "normal_paid_outcome_unresolved",
        "contact_state_missing",
        "contact_state_invalid",
        "canonical_output_invalid",
        "contact_budget_blocked",
        "contact_paid_outcome_unresolved",
    }
)
_PRIVATE_BUSINESS_OUTCOMES: Final[dict[str, frozenset[str]]] = {
    "exa_people_search": frozenset({"contact_selected", "no_qualifying_contact"}),
    "clay_start": frozenset({"email_found", "no_email", "pending"}),
    "apollo_enrichment": frozenset({"email_found", "matched_no_email", "no_match"}),
    "instantly_create": frozenset({"verified", "invalid", "pending"}),
}


@dataclass(frozen=True, slots=True)
class IntegrationCoverage:
    """One sanitized logical provider-integration result."""

    provider: str
    source: CoverageSource
    integration_outcome: Outcome
    business_outcome: str
    operation_count: int
    request_count: int

    def to_dict(self) -> dict[str, object]:
        """Return the fixed machine-readable representation without raw provider state."""
        return {
            "provider": self.provider,
            "source": self.source,
            "integration_outcome": self.integration_outcome,
            "business_outcome": self.business_outcome,
            "operation_count": self.operation_count,
            "usage": {"request_count": self.request_count},
        }


@dataclass(frozen=True, slots=True)
class CanaryCoverageReport:
    """Separate normal pipeline health, provider coverage, and overall readiness."""

    pipeline_outcome: Outcome
    overall_outcome: Outcome
    providers: tuple[IntegrationCoverage, ...]
    safety_flags: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return deterministic sanitized report data with no run identity or PII."""
        return {
            "pipeline_outcome": self.pipeline_outcome,
            "overall_outcome": self.overall_outcome,
            "providers": [provider.to_dict() for provider in self.providers],
            "safety_flags": list(self.safety_flags),
        }


@dataclass(slots=True)
class _State:
    """Validated replay inputs used only to derive the report."""

    run_dir: Path
    m2_checkpoint: RunCheckpoint | None
    contact_checkpoint: RunCheckpoint | None
    private_checkpoint: RunCheckpoint | None
    m2_usage: list[UsageEvent]
    contact_usage: list[UsageEvent]
    private_usage: list[UsageEvent]
    companies: list[CompanyRecord]
    contacts: list[ContactRecord]
    lead_rows: list[dict[str, str]]
    safety_flags: list[str]


def _run_dir(data_root: Path, run_id: str) -> Path:
    """Resolve one direct private run directory without following writable symlinks."""
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    expanded = data_root.expanduser()
    if expanded.is_symlink():
        raise ValueError("data_root must not be a symlink")
    root = expanded.resolve()
    root.mkdir(parents=True, exist_ok=True)
    candidate = root / run_id
    if candidate.is_symlink():
        raise ValueError("run directory must not be a symlink")
    run_dir = candidate.resolve()
    if run_dir.parent != root:
        raise ValueError("run directory must remain directly beneath data_root")
    run_dir.mkdir(exist_ok=True)
    report = run_dir / _REPORT_NAME
    if report.is_symlink():
        raise ValueError("coverage report path must not be a symlink")
    return run_dir


def _checkpoint(path: Path, run_id: str) -> RunCheckpoint | None:
    """Load one checkpoint and bind it to this exact canary run."""
    payload = read_json(path)
    if payload is None:
        return None
    checkpoint = RunCheckpoint.from_dict(payload)
    if checkpoint.run_id != run_id:
        raise ValueError("checkpoint run_id mismatch")
    return checkpoint


def _operations(checkpoint: RunCheckpoint | None) -> dict[str, dict[str, Any]]:
    """Return one validated operation map without mutating persisted state."""
    if checkpoint is None:
        return {}
    raw = checkpoint.provider_state.get("operations", {})
    if not isinstance(raw, dict):
        raise ValueError("checkpoint operations must be an object")
    operations: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise ValueError("checkpoint operation entries must be objects")
        state = value.get("state")
        if state not in {"in_flight", "completed", "failed", "pending"}:
            raise ValueError("checkpoint operation state is invalid")
        operations[key] = cast(dict[str, Any], value)
    return operations


def _load_leads(path: Path) -> list[dict[str, str]]:
    """Load only canonical CSV cells needed to prove a real lead row."""
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise ValueError("leads.csv must be a regular file")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "company_id",
            "contact_id",
            "work_email",
            "email_verification_status",
            "email_source",
        }
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("leads.csv schema is invalid")
        rows: list[dict[str, str]] = []
        for row in reader:
            rows.append({key: str(row.get(key) or "") for key in required})
        return rows


def _load_state(run_dir: Path, run_id: str) -> _State:
    """Load authoritative state, converting malformed domains into sanitized failure flags."""
    flags: list[str] = []
    m2_checkpoint: RunCheckpoint | None = None
    contact_checkpoint: RunCheckpoint | None = None
    private_checkpoint: RunCheckpoint | None = None
    m2_usage: list[UsageEvent] = []
    contact_usage: list[UsageEvent] = []
    private_usage: list[UsageEvent] = []
    companies: list[CompanyRecord] = []
    contacts: list[ContactRecord] = []
    lead_rows: list[dict[str, str]] = []

    try:
        m2_checkpoint = _checkpoint(run_dir / "checkpoint.json", run_id)
        if m2_checkpoint is None:
            flags.append("normal_state_missing")
        else:
            _operations(m2_checkpoint)
        m2_usage = load_usage_events(run_dir / "usage_events.jsonl")
        companies = [
            CompanyRecord.from_dict(payload)
            for payload in load_jsonl(run_dir / "companies_evaluated.jsonl")
        ]
    except (KeyError, TypeError, ValueError):
        flags.append("normal_state_invalid")
        m2_checkpoint = None
        m2_usage = []
        companies = []

    try:
        contact_paths = (
            run_dir / "contact_checkpoint.json",
            run_dir / "contact_usage_events.jsonl",
            run_dir / "contacts.jsonl",
            run_dir / "leads.csv",
        )
        if any(path.exists() for path in contact_paths):
            validate_contact_enrichment_state(
                ContactEnrichmentConfig(run_id=run_id, data_root=run_dir.parent)
            )
        contact_checkpoint = _checkpoint(run_dir / "contact_checkpoint.json", run_id)
        if contact_checkpoint is not None:
            _operations(contact_checkpoint)
        elif m2_checkpoint is not None and m2_checkpoint.status == "completed":
            flags.append("contact_state_missing")
        contact_usage = load_usage_events(run_dir / "contact_usage_events.jsonl")
        contacts = [
            ContactRecord.from_dict(payload)
            for payload in load_jsonl(run_dir / "contacts.jsonl")
        ]
        lead_rows = _load_leads(run_dir / "leads.csv")
        if (
            contact_checkpoint is not None
            and contact_checkpoint.status == "completed"
            and (
                not (run_dir / "contacts.jsonl").is_file()
                or not (run_dir / "leads.csv").is_file()
            )
        ):
            flags.append("canonical_output_invalid")
    except (KeyError, TypeError, ValueError):
        flags.append("contact_state_invalid")
        contact_checkpoint = None
        contact_usage = []
        contacts = []
        lead_rows = []

    private_path = run_dir / "canary_paid_checkpoint.json"
    private_usage_path = run_dir / "canary_paid_usage_events.jsonl"
    if private_path.exists() or private_usage_path.exists():
        try:
            private = CanaryPaidOperations.open(run_dir, run_id=run_id)
            private_checkpoint = private.checkpoint
            private_usage = load_usage_events(private.usage_path)
        except (KeyError, RuntimeError, TypeError, ValueError):
            flags.append("coverage_state_invalid")
            private_checkpoint = None
            private_usage = []

    flags.extend(_checkpoint_safety_flags(m2_checkpoint, contact_checkpoint, private_checkpoint))
    return _State(
        run_dir=run_dir,
        m2_checkpoint=m2_checkpoint,
        contact_checkpoint=contact_checkpoint,
        private_checkpoint=private_checkpoint,
        m2_usage=m2_usage,
        contact_usage=contact_usage,
        private_usage=private_usage,
        companies=companies,
        contacts=contacts,
        lead_rows=lead_rows,
        safety_flags=sorted(set(flags)),
    )


def _checkpoint_safety_flags(
    m2: RunCheckpoint | None,
    contact: RunCheckpoint | None,
    private: RunCheckpoint | None,
) -> list[str]:
    """Classify only safety/provider defects; bounded async pending remains non-failing."""
    flags: list[str] = []
    if m2 is not None:
        if m2.status == "failed":
            flags.append("normal_pipeline_failed")
        elif m2.status == "paused_budget":
            flags.append("normal_budget_blocked")
        elif m2.status in {"paused_unknown", "paused_retryable"}:
            flags.append("normal_paid_outcome_unresolved")
        for entry in _operations(m2).values():
            if entry["state"] in {"in_flight", "pending"}:
                flags.append("normal_paid_outcome_unresolved")
    if contact is not None:
        if contact.status == "paused_budget":
            flags.append("contact_budget_blocked")
        elif contact.status == "paused_unknown":
            flags.append("contact_paid_outcome_unresolved")
        for entry in _operations(contact).values():
            if entry["state"] == "in_flight":
                flags.append("contact_paid_outcome_unresolved")
    if private is not None:
        for entry in _operations(private).values():
            if entry["state"] == "in_flight":
                flags.append("coverage_paid_outcome_unresolved")
    return flags


def _matching_usage(
    events: list[UsageEvent], *, provider: str, operations: frozenset[str]
) -> list[UsageEvent]:
    """Return authoritative calls for one logical integration after basic count validation."""
    matched: list[UsageEvent] = []
    for event in events:
        if event.provider != provider or event.operation not in operations:
            continue
        if isinstance(event.request_count, bool) or not isinstance(event.request_count, int):
            raise ValueError("usage request_count must be an integer")
        if event.request_count < 0:
            raise ValueError("usage request_count must be nonnegative")
        matched.append(event)
    return matched


def _request_count(events: list[UsageEvent]) -> int:
    """Return one bounded integer request total without copying provider metadata."""
    return sum(event.request_count for event in events)


def _normal_integration(
    state: _State,
    *,
    provider_name: str,
    provider: str,
    operations: frozenset[str],
    operation_prefix: str,
    prerequisite: bool,
    business_outcome: str,
) -> IntegrationCoverage:
    """Derive one normal-only integration from completed operation state plus known usage."""
    checkpoint = state.m2_checkpoint
    entries = [
        value
        for key, value in _operations(checkpoint).items()
        if key.startswith(operation_prefix)
        and value.get("provider") == provider
        and value.get("operation") in operations
    ]
    usage = _matching_usage(state.m2_usage, provider=provider, operations=operations)
    calls = _request_count(usage)
    if any(entry["state"] in {"failed", "in_flight", "pending"} for entry in entries):
        outcome: Outcome = "failure"
    elif any(entry["state"] == "completed" for entry in entries) and calls > 0:
        outcome = "success"
    elif not prerequisite:
        outcome = "inconclusive"
    else:
        outcome = "failure"
    return IntegrationCoverage(
        provider=provider_name,
        source="normal",
        integration_outcome=outcome,
        business_outcome=business_outcome if outcome == "success" else "not_exercised",
        operation_count=len(entries),
        request_count=calls,
    )


def _private_entry(
    state: _State, resource: str
) -> tuple[str, dict[str, Any]] | None:
    """Return the sole coverage-only operation for one fixed-canary resource."""
    matches = [
        (key, value)
        for key, value in _operations(state.private_checkpoint).items()
        if value.get("resource") == resource
    ]
    if len(matches) > 1:
        raise ValueError("coverage-only resource has multiple logical operations")
    return None if not matches else matches[0]


def _private_coverage(
    state: _State,
    *,
    provider_name: str,
    resource: str,
    provider: str,
    operations: frozenset[str],
) -> IntegrationCoverage | None:
    """Derive one coverage-only result from private operation identity and authoritative usage."""
    match = _private_entry(state, resource)
    if match is None:
        return None
    _key, entry = match
    usage = _matching_usage(state.private_usage, provider=provider, operations=operations)
    calls = _request_count(usage)
    sequence = entry.get("dispatch_sequence")
    operation_count = (
        sequence + 1
        if isinstance(sequence, int) and not isinstance(sequence, bool)
        else 0
    )
    raw_business = entry.get("business_outcome")
    allowed = _PRIVATE_BUSINESS_OUTCOMES[resource]
    business = raw_business if isinstance(raw_business, str) and raw_business in allowed else None
    entry_state = entry["state"]
    if entry_state in {"failed", "in_flight"}:
        outcome: Outcome = "failure"
    elif business is None or calls <= 0:
        outcome = "failure"
    elif entry_state == "completed":
        outcome = "success"
    elif entry_state == "pending" and resource in {"clay_start", "instantly_create"}:
        # A parsed async start alone is not enough coverage: at least one bounded status read
        # must also have been accepted by the production parser.
        outcome = "success" if operation_count >= 2 else "inconclusive"
    else:
        outcome = "failure"
    return IntegrationCoverage(
        provider=provider_name,
        source="coverage_only",
        integration_outcome=outcome,
        business_outcome=business or "invalid_evidence",
        operation_count=operation_count,
        request_count=calls,
    )


def _normal_m4_entry(
    state: _State,
    *,
    provider_name: str,
    provider: str,
    operations: frozenset[str],
    key_prefix: str,
    prerequisite: bool,
    business: str,
    required_operation: str | None = None,
    completed_requires_operation: str | None = None,
    pending_requires_operation: str | None = None,
) -> IntegrationCoverage | None:
    """Return a normal M4 result only when durable state has authoritative usage provenance."""
    entries = [
        value
        for key, value in _operations(state.contact_checkpoint).items()
        if key.startswith(key_prefix)
    ]
    if not entries:
        return None
    usage = _matching_usage(state.contact_usage, provider=provider, operations=operations)
    calls = _request_count(usage)
    states = {cast(str, entry["state"]) for entry in entries}
    required_calls = (
        calls
        if required_operation is None
        else _request_count(
            _matching_usage(
                state.contact_usage,
                provider=provider,
                operations=frozenset({required_operation}),
            )
        )
    )
    if "in_flight" in states or "failed" in states:
        outcome: Outcome = "failure"
    elif required_calls <= 0:
        outcome = "failure"
    elif "completed" in states and calls > 0:
        completed_calls = (
            calls
            if completed_requires_operation is None
            else _request_count(
                _matching_usage(
                    state.contact_usage,
                    provider=provider,
                    operations=frozenset({completed_requires_operation}),
                )
            )
        )
        outcome = "success" if completed_calls > 0 else "failure"
    elif "pending" in states:
        if pending_requires_operation is None:
            outcome = "inconclusive"
        else:
            read_calls = _request_count(
                _matching_usage(
                    state.contact_usage,
                    provider=provider,
                    operations=frozenset({pending_requires_operation}),
                )
            )
            outcome = "success" if read_calls > 0 else "inconclusive"
    elif prerequisite:
        outcome = "failure"
    else:
        outcome = "inconclusive"
    return IntegrationCoverage(
        provider=provider_name,
        source="normal",
        integration_outcome=outcome,
        business_outcome=business if outcome != "failure" else "failed",
        operation_count=len(entries),
        request_count=calls,
    )


def _accepted_companies(state: _State) -> list[CompanyRecord]:
    """Return only currently accepted, completed M3 decisions."""
    return [
        company
        for company in state.companies
        if company.stage_status.get("decision") == "completed"
        and company.final_decision == "accepted"
    ]


def _exa_people_business(state: _State) -> str:
    """Classify the selected-contact result from normal Exa checkpoint state only."""
    for key, entry in _operations(state.contact_checkpoint).items():
        if not key.startswith("exa:") or entry.get("state") != "completed":
            continue
        ids = entry.get("contact_ids")
        if isinstance(ids, list) and any(isinstance(item, str) and item for item in ids):
            return "contact_selected"
    return "no_qualifying_contact"


def _clay_business(state: _State) -> str:
    """Classify only whether normal Clay produced a production-approved work email."""
    entry = _operations(state.contact_checkpoint).get("clay:batch")
    if entry is not None and entry.get("state") == "pending":
        return "pending"
    ids = entry.get("contact_ids", []) if entry is not None else []
    submitted = {item for item in ids if isinstance(item, str)} if isinstance(ids, list) else set()
    if any(
        contact.contact_id in submitted
        and contact.email_source == "clay"
        and usable_work_email(contact.work_email) is not None
        for contact in state.contacts
    ):
        return "email_found"
    return "no_email"


def _apollo_business(state: _State) -> str:
    """Use sanitized match evidence plus canonical email state, never raw response data."""
    events = _matching_usage(
        state.contact_usage,
        provider="apollo",
        operations=frozenset({"people_enrichment"}),
    )
    matched_values = [event.metadata.get("matched") for event in events]
    if any(value is False for value in matched_values):
        return "no_match"
    apollo_ids = {
        key.removeprefix("apollo:")
        for key, entry in _operations(state.contact_checkpoint).items()
        if key.startswith("apollo:") and entry.get("state") == "completed"
    }
    if any(
        contact.contact_id in apollo_ids
        and contact.email_source == "apollo"
        and usable_work_email(contact.work_email) is not None
        for contact in state.contacts
    ):
        return "email_found"
    return "matched_no_email"


def _instantly_business(state: _State) -> str:
    """Return the sanitized verification state already retained by normal M4."""
    values = [
        entry.get("status")
        for key, entry in _operations(state.contact_checkpoint).items()
        if key.startswith("instantly:") and entry.get("state") == "completed"
    ]
    if "verified" in values:
        return "verified"
    if "invalid" in values:
        return "invalid"
    if any(
        entry.get("state") == "pending"
        for key, entry in _operations(state.contact_checkpoint).items()
        if key.startswith("instantly:")
    ):
        return "pending"
    return "not_exercised"


def _m4_coverage(state: _State) -> tuple[IntegrationCoverage, ...]:
    """Prefer normal evidence, then private evidence, then prerequisite classification."""
    accepted = _accepted_companies(state)
    any_evaluated = bool(state.companies)

    exa_normal = _normal_m4_entry(
        state,
        provider_name="exa_people",
        provider="exa",
        operations=frozenset({"people_search"}),
        key_prefix="exa:",
        prerequisite=bool(accepted),
        business=_exa_people_business(state),
    )
    exa_private = _private_coverage(
        state,
        provider_name="exa_people",
        resource="exa_people_search",
        provider="exa",
        operations=frozenset({"people_search"}),
    )
    exa = exa_normal or exa_private
    if exa is None:
        exa = IntegrationCoverage(
            "exa_people",
            "coverage_only",
            "failure" if any_evaluated else "inconclusive",
            "not_exercised",
            0,
            0,
        )

    contact_prerequisite = (
        exa.integration_outcome == "success"
        and exa.business_outcome == "contact_selected"
    )
    clay_normal = _normal_m4_entry(
        state,
        provider_name="clay",
        provider="clay",
        operations=frozenset({"work_email_routine_start", "work_email_routine_results"}),
        key_prefix="clay:batch",
        prerequisite=contact_prerequisite,
        business=_clay_business(state),
        required_operation="work_email_routine_start",
        completed_requires_operation="work_email_routine_results",
        pending_requires_operation="work_email_routine_results",
    )
    clay_private = _private_coverage(
        state,
        provider_name="clay",
        resource="clay_start",
        provider="clay",
        operations=frozenset({"work_email_routine_start", "work_email_routine_results"}),
    )
    clay = clay_normal or clay_private
    if clay is None:
        clay = IntegrationCoverage(
            "clay",
            "coverage_only",
            "failure" if contact_prerequisite else "inconclusive",
            "not_exercised",
            0,
            0,
        )

    apollo_prerequisite = (
        contact_prerequisite
        and clay.integration_outcome == "success"
        and clay.business_outcome != "pending"
    )
    apollo_normal = _normal_m4_entry(
        state,
        provider_name="apollo",
        provider="apollo",
        operations=frozenset({"people_enrichment"}),
        key_prefix="apollo:",
        prerequisite=apollo_prerequisite,
        business=_apollo_business(state),
    )
    apollo_private = _private_coverage(
        state,
        provider_name="apollo",
        resource="apollo_enrichment",
        provider="apollo",
        operations=frozenset({"people_enrichment"}),
    )
    apollo = apollo_normal or apollo_private
    if apollo is None:
        apollo = IntegrationCoverage(
            "apollo",
            "coverage_only",
            "failure" if apollo_prerequisite else "inconclusive",
            "not_exercised",
            0,
            0,
        )

    email_prerequisite = any(
        item.business_outcome == "email_found" and item.integration_outcome == "success"
        for item in (clay, apollo)
    )
    instantly_normal = _normal_m4_entry(
        state,
        provider_name="instantly",
        provider="instantly",
        operations=frozenset({"email_verification_create", "email_verification_get"}),
        key_prefix="instantly:",
        prerequisite=email_prerequisite,
        business=_instantly_business(state),
        required_operation="email_verification_create",
        pending_requires_operation="email_verification_get",
    )
    instantly_private = _private_coverage(
        state,
        provider_name="instantly",
        resource="instantly_create",
        provider="instantly",
        operations=frozenset({"email_verification_create", "email_verification_get"}),
    )
    instantly = instantly_normal or instantly_private
    if instantly is None:
        instantly = IntegrationCoverage(
            "instantly",
            "coverage_only",
            "failure" if email_prerequisite else "inconclusive",
            "not_exercised",
            0,
            0,
        )
    return exa, clay, apollo, instantly


def _lead_row_matches(contact: ContactRecord, rows: list[dict[str, str]]) -> bool:
    """Require the CSV row to represent this exact canonical verified contact."""
    return any(
        row["company_id"] == contact.company_id
        and row["contact_id"] == contact.contact_id
        and row["work_email"] == (contact.work_email or "")
        and row["email_source"] == (contact.email_source or "")
        and row["email_verification_status"] == "verified"
        for row in rows
    )


def _pipeline_success(state: _State) -> bool:
    """Prove a real currently authorized verified lead; artifact existence is insufficient."""
    if state.contact_checkpoint is None or state.contact_checkpoint.status != "completed":
        return False
    accepted_ids = {company.company_id for company in _accepted_companies(state)}
    operations = _operations(state.contact_checkpoint)
    for contact in state.contacts:
        if contact.company_id not in accepted_ids:
            continue
        if contact.email_source not in {"clay", "apollo"}:
            continue
        if usable_work_email(contact.work_email) is None:
            continue
        if contact.email_verification_status != "verified":
            continue
        exa = operations.get(f"exa:{contact.company_id}")
        clay = operations.get("clay:batch")
        instantly = operations.get(f"instantly:{contact.contact_id}")
        if not isinstance(exa, dict) or exa.get("state") != "completed":
            continue
        ids = exa.get("contact_ids")
        if not isinstance(ids, list) or contact.contact_id not in ids:
            continue
        if not isinstance(clay, dict) or clay.get("state") != "completed":
            continue
        submitted = clay.get("contact_ids")
        if not isinstance(submitted, list) or contact.contact_id not in submitted:
            continue
        if (
            not isinstance(instantly, dict)
            or instantly.get("state") != "completed"
            or instantly.get("status") != "verified"
        ):
            continue
        if _lead_row_matches(contact, state.lead_rows):
            return True
    return False


def _m1_m3_coverage(state: _State) -> tuple[IntegrationCoverage, ...]:
    """Build the three normal-only provider integrations that are never shadowed."""
    any_company = bool(state.companies)
    any_evidence = any(bool(company.evidence) for company in state.companies)
    discovery = _normal_integration(
        state,
        provider_name="exa_discovery",
        provider="exa",
        operations=frozenset({"company_search"}),
        operation_prefix="discovery:",
        prerequisite=True,
        business_outcome="candidate_found" if any_company else "no_company",
    )
    research = _normal_integration(
        state,
        provider_name="exa_research",
        provider="exa",
        operations=frozenset({"company_research"}),
        operation_prefix="research:",
        prerequisite=any_company,
        business_outcome="evidence_found" if any_evidence else "no_evidence",
    )
    deepseek = _normal_integration(
        state,
        provider_name="deepseek",
        provider="deepseek",
        operations=frozenset({"structured_extraction"}),
        operation_prefix="extraction:",
        prerequisite=any_evidence,
        business_outcome="parsed",
    )
    return discovery, research, deepseek


def build_canary_coverage_report(
    data_root: Path | str,
    *,
    run_id: str,
) -> CanaryCoverageReport:
    """Rebuild the derived report from authoritative state with zero provider calls."""
    run_dir = _run_dir(Path(data_root), run_id)
    state = _load_state(run_dir, run_id)
    providers = (*_m1_m3_coverage(state), *_m4_coverage(state))
    if tuple(item.provider for item in providers) != _REQUIRED_INTEGRATIONS:
        raise AssertionError("canary integration order changed unexpectedly")

    normal_provider_failed = any(
        item.source == "normal" and item.integration_outcome == "failure"
        for item in providers
    )
    if _PIPELINE_FAILURE_FLAGS.intersection(state.safety_flags) or normal_provider_failed:
        pipeline: Outcome = "failure"
    elif _pipeline_success(state):
        pipeline = "success"
    else:
        pipeline = "inconclusive"

    if state.safety_flags or any(item.integration_outcome == "failure" for item in providers):
        overall: Outcome = "failure"
    elif pipeline == "success" and all(
        item.integration_outcome == "success" for item in providers
    ):
        overall = "success"
    else:
        overall = "inconclusive"

    report = CanaryCoverageReport(
        pipeline_outcome=pipeline,
        overall_outcome=overall,
        providers=tuple(providers),
        safety_flags=tuple(state.safety_flags),
    )
    write_json_atomic(run_dir / _REPORT_NAME, report.to_dict())
    return report


__all__ = [
    "CanaryCoverageReport",
    "CoverageSource",
    "IntegrationCoverage",
    "Outcome",
    "build_canary_coverage_report",
]
