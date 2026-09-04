"""Deterministic sanitized readiness evidence for the fixed production canary."""

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
_REPORT_NAME = "canary_coverage_report.json"
_REQUIRED = (
    "exa_discovery",
    "exa_research",
    "deepseek",
    "exa_people",
    "clay",
    "apollo",
    "instantly",
)
_PIPELINE_FAILURE_FLAGS = frozenset({
    "normal_state_missing", "normal_state_invalid", "normal_pipeline_failed",
    "normal_budget_blocked", "normal_paid_outcome_unresolved", "contact_state_missing",
    "contact_state_invalid", "canonical_output_invalid", "contact_budget_blocked",
    "contact_paid_outcome_unresolved",
})


@dataclass(frozen=True, slots=True)
class IntegrationCoverage:
    provider: str
    source: CoverageSource
    integration_outcome: Outcome
    business_outcome: str
    operation_count: int
    request_count: int

    def to_dict(self) -> dict[str, object]:
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
    pipeline_outcome: Outcome
    overall_outcome: Outcome
    providers: tuple[IntegrationCoverage, ...]
    safety_flags: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "pipeline_outcome": self.pipeline_outcome,
            "overall_outcome": self.overall_outcome,
            "providers": [item.to_dict() for item in self.providers],
            "safety_flags": list(self.safety_flags),
        }


@dataclass(slots=True)
class _State:
    normal_checkpoint: RunCheckpoint | None
    contact_checkpoint: RunCheckpoint | None
    private_checkpoint: RunCheckpoint | None
    normal_usage: list[UsageEvent]
    contact_usage: list[UsageEvent]
    private_usage: list[UsageEvent]
    companies: list[CompanyRecord]
    contacts: list[ContactRecord]
    leads: list[dict[str, str]]
    safety_flags: list[str]


def _run_dir(data_root: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    root = data_root.expanduser()
    if root.is_symlink():
        raise ValueError("data_root must not be a symlink")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    candidate = root / run_id
    if candidate.is_symlink():
        raise ValueError("run directory must not be a symlink")
    run_dir = candidate.resolve()
    if run_dir.parent != root:
        raise ValueError("run directory must remain directly beneath data_root")
    run_dir.mkdir(exist_ok=True)
    return run_dir


def _checkpoint(path: Path, run_id: str) -> RunCheckpoint | None:
    payload = read_json(path)
    if payload is None:
        return None
    checkpoint = RunCheckpoint.from_dict(payload)
    if checkpoint.run_id != run_id:
        raise ValueError("checkpoint run_id mismatch")
    return checkpoint


def _operations(checkpoint: RunCheckpoint | None) -> dict[str, dict[str, Any]]:
    if checkpoint is None:
        return {}
    raw = checkpoint.provider_state.get("operations", {})
    if not isinstance(raw, dict):
        raise ValueError("checkpoint operations must be an object")
    result: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key or not isinstance(value, dict):
            raise ValueError("checkpoint operation entries must be objects")
        if value.get("state") not in {"in_flight", "completed", "failed", "pending"}:
            raise ValueError("checkpoint operation state is invalid")
        result[key] = cast(dict[str, Any], value)
    return result


def _load_leads(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise ValueError("leads.csv must be a regular file")
    required = {
        "company_id",
        "contact_id",
        "work_email",
        "email_verification_status",
        "email_source",
    }
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("leads.csv schema is invalid")
        return [{key: str(row.get(key) or "") for key in required} for row in reader]


def _state_flags(
    normal: RunCheckpoint | None,
    contact: RunCheckpoint | None,
    private: RunCheckpoint | None,
) -> list[str]:
    flags: list[str] = []
    if normal is not None:
        if normal.status == "failed":
            flags.append("normal_pipeline_failed")
        elif normal.status == "paused_budget":
            flags.append("normal_budget_blocked")
        elif normal.status in {"paused_unknown", "paused_retryable"}:
            flags.append("normal_paid_outcome_unresolved")
        if any(
            item.get("state") in {"in_flight", "pending"}
            for item in _operations(normal).values()
        ):
            flags.append("normal_paid_outcome_unresolved")
    if contact is not None:
        if contact.status == "paused_budget":
            flags.append("contact_budget_blocked")
        elif contact.status in {"paused_unknown", "paused_retryable"}:
            flags.append("contact_paid_outcome_unresolved")
        if any(item.get("state") == "in_flight" for item in _operations(contact).values()):
            flags.append("contact_paid_outcome_unresolved")
    if private is not None and any(
        item.get("state") == "in_flight"
        for item in _operations(private).values()
    ):
        flags.append("coverage_paid_outcome_unresolved")
    return flags


def _load_state(run_dir: Path, run_id: str) -> _State:
    flags: list[str] = []
    normal_checkpoint: RunCheckpoint | None = None
    normal_usage: list[UsageEvent] = []
    companies: list[CompanyRecord] = []
    try:
        normal_checkpoint = _checkpoint(run_dir / "checkpoint.json", run_id)
        if normal_checkpoint is None:
            flags.append("normal_state_missing")
        else:
            _operations(normal_checkpoint)
        normal_usage = load_usage_events(run_dir / "usage_events.jsonl")
        companies = [
            CompanyRecord.from_dict(row)
            for row in load_jsonl(run_dir / "companies_evaluated.jsonl")
        ]
    except (KeyError, TypeError, ValueError):
        flags.append("normal_state_invalid")
        normal_checkpoint = None
        normal_usage = []
        companies = []

    contact_checkpoint: RunCheckpoint | None = None
    contact_usage: list[UsageEvent] = []
    contacts: list[ContactRecord] = []
    leads: list[dict[str, str]] = []
    contact_paths = [
        run_dir / name
        for name in (
            "contact_checkpoint.json",
            "contact_usage_events.jsonl",
            "contacts.jsonl",
            "leads.csv",
        )
    ]
    try:
        if any(path.exists() for path in contact_paths):
            validate_contact_enrichment_state(
                ContactEnrichmentConfig(run_id=run_id, data_root=run_dir.parent)
            )
        contact_checkpoint = _checkpoint(run_dir / "contact_checkpoint.json", run_id)
        if contact_checkpoint is None:
            if normal_checkpoint is not None and normal_checkpoint.status == "completed":
                flags.append("contact_state_missing")
        else:
            _operations(contact_checkpoint)
        contact_usage = load_usage_events(run_dir / "contact_usage_events.jsonl")
        contacts = [ContactRecord.from_dict(row) for row in load_jsonl(run_dir / "contacts.jsonl")]
        leads = _load_leads(run_dir / "leads.csv")
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
        leads = []

    private_checkpoint: RunCheckpoint | None = None
    private_usage: list[UsageEvent] = []
    if (run_dir / "canary_paid_checkpoint.json").exists() or (
        run_dir / "canary_paid_usage_events.jsonl"
    ).exists():
        try:
            private = CanaryPaidOperations.open(run_dir, run_id=run_id)
            private_checkpoint = private.checkpoint
            private_usage = load_usage_events(private.usage_path)
        except (KeyError, RuntimeError, TypeError, ValueError):
            flags.append("coverage_state_invalid")

    flags.extend(_state_flags(normal_checkpoint, contact_checkpoint, private_checkpoint))
    return _State(
        normal_checkpoint, contact_checkpoint, private_checkpoint,
        normal_usage, contact_usage, private_usage,
        companies, contacts, leads, sorted(set(flags)),
    )


def _matching(events: list[UsageEvent], provider: str, operations: set[str]) -> list[UsageEvent]:
    return [
        event
        for event in events
        if event.provider == provider and event.operation in operations
    ]


def _requests(events: list[UsageEvent]) -> int:
    return sum(event.request_count for event in events)


def _coverage(
    provider: str,
    source: CoverageSource,
    outcome: Outcome,
    business: str,
    operations: int,
    requests: int,
) -> IntegrationCoverage:
    return IntegrationCoverage(provider, source, outcome, business, operations, requests)


def _normal_integration(
    state: _State,
    name: str,
    provider: str,
    operations: set[str],
    prefix: str,
    prerequisite: bool,
    business: str,
    required_operation: str | None = None,
    completed_requires: str | None = None,
    pending_requires: str | None = None,
) -> IntegrationCoverage | None:
    normal_domain = name in {"exa_discovery", "exa_research", "deepseek"}
    checkpoint = state.normal_checkpoint if normal_domain else state.contact_checkpoint
    usage_events = state.normal_usage if normal_domain else state.contact_usage
    entries = [item for key, item in _operations(checkpoint).items() if key.startswith(prefix)]
    if not entries and name not in {"exa_discovery", "exa_research", "deepseek"}:
        return None
    usage = _matching(usage_events, provider, operations)
    calls = _requests(usage)
    states = {item.get("state") for item in entries}
    required_calls = (
        calls
        if required_operation is None
        else _requests(_matching(usage_events, provider, {required_operation}))
    )
    identity_invalid = normal_domain and any(
        item.get("provider") != provider or item.get("operation") not in operations
        for item in entries
    )
    if identity_invalid or states.intersection({"failed", "in_flight"}):
        outcome: Outcome = "failure"
    elif entries and required_calls <= 0:
        outcome = "failure"
    elif "completed" in states:
        completed_calls = (
            calls
            if completed_requires is None
            else _requests(_matching(usage_events, provider, {completed_requires}))
        )
        outcome = "success" if completed_calls > 0 else "failure"
    elif "pending" in states:
        if pending_requires is None:
            outcome = "inconclusive"
        else:
            outcome = (
                "success"
                if _requests(_matching(usage_events, provider, {pending_requires})) > 0
                else "inconclusive"
            )
    elif prerequisite:
        outcome = "failure"
    else:
        outcome = "inconclusive"
    return _coverage(
        name,
        "normal",
        outcome,
        business if outcome != "failure" else "failed",
        len(entries),
        calls,
    )


def _exa_business(state: _State) -> str:
    for key, entry in _operations(state.contact_checkpoint).items():
        ids = entry.get("contact_ids")
        if (
            key.startswith("exa:")
            and entry.get("state") == "completed"
            and isinstance(ids, list)
            and bool(ids)
        ):
            return "contact_selected"
    return "no_qualifying_contact"


def _clay_business(state: _State) -> str:
    entry = _operations(state.contact_checkpoint).get("clay:batch")
    if entry is not None and entry.get("state") == "pending":
        return "pending"
    ids = entry.get("contact_ids", []) if entry is not None else []
    submitted = set(ids) if isinstance(ids, list) else set()
    return "email_found" if any(
        contact.contact_id in submitted
        and contact.email_source == "clay"
        and usable_work_email(contact.work_email) is not None
        for contact in state.contacts
    ) else "no_email"


def _apollo_business(state: _State) -> str:
    events = _matching(state.contact_usage, "apollo", {"people_enrichment"})
    if any(event.metadata.get("matched") is False for event in events):
        return "no_match"
    apollo_ids = {
        key.removeprefix("apollo:")
        for key, entry in _operations(state.contact_checkpoint).items()
        if key.startswith("apollo:") and entry.get("state") == "completed"
    }
    return "email_found" if any(
        contact.contact_id in apollo_ids
        and contact.email_source == "apollo"
        and usable_work_email(contact.work_email) is not None
        for contact in state.contacts
    ) else "matched_no_email"


def _instantly_business(state: _State) -> str:
    entries = [
        entry
        for key, entry in _operations(state.contact_checkpoint).items()
        if key.startswith("instantly:")
    ]
    statuses = {entry.get("status") for entry in entries if entry.get("state") == "completed"}
    if "verified" in statuses:
        return "verified"
    if "invalid" in statuses:
        return "invalid"
    return (
        "pending"
        if any(entry.get("state") == "pending" for entry in entries)
        else "not_exercised"
    )


def _private_business(name: str, entry: dict[str, Any], events: list[UsageEvent]) -> str | None:
    raw = entry.get("business_outcome")
    if name == "exa_people":
        return {
            "contact_selected": "contact_selected",
            "no_contact": "no_qualifying_contact",
        }.get(cast(str, raw))
    if name == "clay":
        return {
            "email": "email_found",
            "no_email": "no_email",
            "pending": "pending",
        }.get(cast(str, raw))
    if name == "apollo":
        if raw == "email":
            return "email_found"
        if raw == "no_email":
            matched = [event.metadata.get("matched") for event in events]
            if any(value is False for value in matched):
                return "no_match"
            if any(value is True for value in matched):
                return "matched_no_email"
        return None
    if name == "instantly" and raw in {"verified", "invalid", "pending"}:
        return cast(str, raw)
    return None


def _private_integration(
    state: _State,
    name: str,
    operation_id: str,
    provider: str,
    operations: set[str],
) -> IntegrationCoverage | None:
    entry = _operations(state.private_checkpoint).get(operation_id)
    if entry is None:
        return None
    events = _matching(state.private_usage, provider, operations)
    calls = _requests(events)
    sequence = entry.get("dispatch_sequence")
    count = (
        sequence + 1
        if isinstance(sequence, int)
        and not isinstance(sequence, bool)
        and sequence >= 0
        else 0
    )
    business = _private_business(name, entry, events)
    current = entry.get("state")
    if current in {"failed", "in_flight"} or business is None or calls <= 0:
        outcome: Outcome = "failure"
    elif current == "completed":
        outcome = "success"
    elif current == "pending" and name in {"clay", "instantly"}:
        outcome = "success" if count >= 2 else "inconclusive"
    else:
        outcome = "failure"
    return _coverage(name, "coverage_only", outcome, business or "invalid_evidence", count, calls)


def _m1_m3(state: _State) -> tuple[IntegrationCoverage, ...]:
    any_company = bool(state.companies)
    any_evidence = any(bool(company.evidence) for company in state.companies)
    rows = (
        _normal_integration(
            state, "exa_discovery", "exa", {"company_search"}, "discovery:", True,
            "candidate_found" if any_company else "no_company",
        ),
        _normal_integration(
            state, "exa_research", "exa", {"company_research"}, "research:",
            any_company, "evidence_found" if any_evidence else "no_evidence",
        ),
        _normal_integration(
            state, "deepseek", "deepseek", {"structured_extraction"}, "extraction:",
            any_evidence, "parsed",
        ),
    )
    return cast(tuple[IntegrationCoverage, ...], rows)


def _m4(state: _State) -> tuple[IntegrationCoverage, ...]:
    accepted = any(
        company.stage_status.get("decision") == "completed"
        and company.final_decision == "accepted"
        for company in state.companies
    )
    exa = _normal_integration(
        state, "exa_people", "exa", {"people_search"}, "exa:", accepted,
        _exa_business(state),
    ) or _private_integration(
        state, "exa_people", "coverage:exa_people", "exa", {"people_search"}
    )
    if exa is None:
        exa = _coverage(
            "exa_people", "coverage_only",
            "failure" if state.companies else "inconclusive", "not_exercised", 0, 0,
        )
    if accepted and exa.source != "normal":
        exa = _coverage("exa_people", "normal", "failure", "not_exercised", 0, 0)

    has_contact = (
        exa.integration_outcome == "success"
        and exa.business_outcome == "contact_selected"
    )
    clay_ops = {"work_email_routine_start", "work_email_routine_results"}
    clay = _normal_integration(
        state, "clay", "clay", clay_ops, "clay:batch", has_contact,
        _clay_business(state), "work_email_routine_start",
        "work_email_routine_results", "work_email_routine_results",
    ) or _private_integration(state, "clay", "coverage:clay", "clay", clay_ops)
    if clay is None:
        clay = _coverage(
            "clay", "coverage_only", "failure" if has_contact else "inconclusive",
            "not_exercised", 0, 0,
        )

    apollo_prerequisite = (
        has_contact
        and clay.integration_outcome == "success"
        and clay.business_outcome != "pending"
    )
    apollo = _normal_integration(
        state, "apollo", "apollo", {"people_enrichment"}, "apollo:",
        apollo_prerequisite, _apollo_business(state),
    ) or _private_integration(
        state, "apollo", "coverage:apollo", "apollo", {"people_enrichment"}
    )
    if apollo is None:
        apollo = _coverage(
            "apollo", "coverage_only",
            "failure" if apollo_prerequisite else "inconclusive",
            "not_exercised", 0, 0,
        )

    has_email = any(
        item.integration_outcome == "success" and item.business_outcome == "email_found"
        for item in (clay, apollo)
    )
    instantly_ops = {"email_verification_create", "email_verification_get"}
    instantly = _normal_integration(
        state, "instantly", "instantly", instantly_ops, "instantly:", has_email,
        _instantly_business(state), "email_verification_create", None,
        "email_verification_get",
    ) or _private_integration(
        state, "instantly", "coverage:instantly", "instantly", instantly_ops
    )
    if instantly is None:
        instantly = _coverage(
            "instantly", "coverage_only", "failure" if has_email else "inconclusive",
            "not_exercised", 0, 0,
        )
    return exa, clay, apollo, instantly


def _lead_matches(contact: ContactRecord, rows: list[dict[str, str]]) -> bool:
    return any(
        row["company_id"] == contact.company_id and row["contact_id"] == contact.contact_id
        and row["work_email"] == (contact.work_email or "")
        and row["email_source"] == (contact.email_source or "")
        and row["email_verification_status"] == "verified" for row in rows
    )


def _pipeline_success(state: _State) -> bool:
    if state.contact_checkpoint is None or state.contact_checkpoint.status != "completed":
        return False
    accepted = {
        company.company_id
        for company in state.companies
        if company.stage_status.get("decision") == "completed"
        and company.final_decision == "accepted"
    }
    operations = _operations(state.contact_checkpoint)
    clay = operations.get("clay:batch")
    for contact in state.contacts:
        if (
            contact.company_id not in accepted
            or contact.email_source not in {"clay", "apollo"}
            or usable_work_email(contact.work_email) is None
        ):
            continue
        if (
            contact.email_verification_status != "verified"
            or not _lead_matches(contact, state.leads)
        ):
            continue
        exa = operations.get(f"exa:{contact.company_id}")
        instant = operations.get(f"instantly:{contact.contact_id}")
        if (
            not isinstance(exa, dict)
            or exa.get("state") != "completed"
            or not isinstance(exa.get("contact_ids"), list)
            or contact.contact_id not in exa["contact_ids"]
        ):
            continue
        if (
            not isinstance(clay, dict)
            or clay.get("state") != "completed"
            or not isinstance(clay.get("contact_ids"), list)
            or contact.contact_id not in clay["contact_ids"]
        ):
            continue
        if contact.email_source == "apollo":
            apollo = operations.get(f"apollo:{contact.contact_id}")
            if not isinstance(apollo, dict) or apollo.get("state") != "completed":
                continue
        if (
            isinstance(instant, dict)
            and instant.get("state") == "completed"
            and instant.get("status") == "verified"
        ):
            return True
    return False


def build_canary_coverage_report(data_root: Path | str, *, run_id: str) -> CanaryCoverageReport:
    """Rebuild the private report from authoritative state without provider calls."""
    run_dir = _run_dir(Path(data_root), run_id)
    state = _load_state(run_dir, run_id)
    providers = (*_m1_m3(state), *_m4(state))
    if tuple(item.provider for item in providers) != _REQUIRED:
        raise AssertionError("canary integration order changed unexpectedly")

    normal_failed = any(
        item.source == "normal" and item.integration_outcome == "failure"
        for item in providers
    )
    if _PIPELINE_FAILURE_FLAGS.intersection(state.safety_flags) or normal_failed:
        pipeline: Outcome = "failure"
    elif _pipeline_success(state):
        pipeline = "success"
    else:
        pipeline = "inconclusive"

    if state.safety_flags or any(item.integration_outcome == "failure" for item in providers):
        overall: Outcome = "failure"
    elif pipeline == "success" and all(item.integration_outcome == "success" for item in providers):
        overall = "success"
    else:
        overall = "inconclusive"

    report = CanaryCoverageReport(pipeline, overall, tuple(providers), tuple(state.safety_flags))
    write_json_atomic(run_dir / _REPORT_NAME, report.to_dict())
    return report


__all__ = [
    "CanaryCoverageReport",
    "CoverageSource",
    "IntegrationCoverage",
    "Outcome",
    "build_canary_coverage_report",
]
