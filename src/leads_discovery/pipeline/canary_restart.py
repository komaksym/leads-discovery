"""Typed canary restart adapter for bounded production-derived normal prerequisites."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.models import CompanyRecord, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.canary_paths import canary_run_dir
from leads_discovery.pipeline.git_journal import (
    git_journal_configured,
    load_canary_private_state,
    load_canary_restart_state,
    persist_canary_restart_state,
)
from leads_discovery.pipeline.state import (
    load_jsonl,
    read_json,
    write_json_atomic,
    write_jsonl_atomic,
    write_text_atomic,
)

_MAX_LEADS_BYTES: Final[int] = 256 * 1024
_RESUMABLE_CONTACT_STATUSES: Final[frozenset[str]] = frozenset(
    {"running", "paused_budget", "paused_pending", "completed"}
)
_CONTACT_STATUS_RANK: Final[dict[str, int]] = {
    "running": 0,
    "paused_pending": 1,
    "paused_budget": 2,
    "completed": 3,
}
_OPERATION_STATUS_RANK: Final[dict[str, int]] = {
    "in_flight": 0,
    "pending": 1,
    "failed": 2,
    "completed": 2,
}
_OPERATION_IDENTITY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "provider",
        "operation",
        "dispatch_id",
        "input_fingerprint",
        "routine_run_id",
        "email",
        "contact_ids",
        "company_id",
        "resource",
        "dispatch_resource",
        "dispatch_sequence",
    }
)


@dataclass(frozen=True, slots=True)
class CanaryRestartState:
    """Bounded normal authority required to resume shadow work and publication."""

    checkpoint: dict[str, Any]
    usage_events: tuple[dict[str, Any], ...]
    companies_evaluated: tuple[dict[str, Any], ...]
    contact_checkpoint: dict[str, Any]
    contact_usage_events: tuple[dict[str, Any], ...]
    contacts: tuple[dict[str, Any], ...]
    leads_csv: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint": self.checkpoint,
            "usage_events": list(self.usage_events),
            "companies_evaluated": list(self.companies_evaluated),
            "contact_checkpoint": self.contact_checkpoint,
            "contact_usage_events": list(self.contact_usage_events),
            "contacts": list(self.contacts),
            "leads_csv": self.leads_csv,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, run_id: str) -> CanaryRestartState:
        expected = {
            "checkpoint",
            "usage_events",
            "companies_evaluated",
            "contact_checkpoint",
            "contact_usage_events",
            "contacts",
            "leads_csv",
        }
        if set(payload) != expected:
            raise ValueError("canary normal restart state shape is invalid")
        checkpoint = payload["checkpoint"]
        contact_checkpoint = payload["contact_checkpoint"]
        leads_csv = payload["leads_csv"]
        if not isinstance(checkpoint, dict) or not isinstance(contact_checkpoint, dict):
            raise ValueError("canary normal restart checkpoints are invalid")
        if not isinstance(leads_csv, str):
            raise ValueError("canary normal restart leads artifact is invalid")
        if len(leads_csv.encode("utf-8")) > _MAX_LEADS_BYTES:
            raise ValueError("canary normal restart leads artifact exceeds its fixed bound")
        if "\x00" in leads_csv:
            raise ValueError("canary normal restart leads artifact is invalid")

        normal = RunCheckpoint.from_dict(checkpoint)
        contact = RunCheckpoint.from_dict(contact_checkpoint)
        if normal.run_id != run_id or contact.run_id != run_id:
            raise ValueError("canary normal restart checkpoint run_id mismatch")
        if normal.status != "completed":
            raise ValueError("canary normal restart prerequisites must be completed")
        if contact.status not in _RESUMABLE_CONTACT_STATUSES:
            raise ValueError("canary normal restart contact state is not safely resumable")

        def rows(name: str) -> tuple[dict[str, Any], ...]:
            value = payload[name]
            if not isinstance(value, list):
                raise ValueError(f"canary normal restart {name} is invalid")
            if any(not isinstance(row, dict) for row in value):
                raise ValueError(f"canary normal restart {name} is invalid")
            return tuple(cast(dict[str, Any], row) for row in value)

        usage_events = rows("usage_events")
        companies_evaluated = rows("companies_evaluated")
        contact_usage_events = rows("contact_usage_events")
        contacts = rows("contacts")
        for row in usage_events:
            UsageEvent.from_dict(row)
        for row in companies_evaluated:
            CompanyRecord.from_dict(row)
        for row in contact_usage_events:
            UsageEvent.from_dict(row)
        for row in contacts:
            ContactRecord.from_dict(row)

        return cls(
            checkpoint=normal.to_dict(),
            usage_events=usage_events,
            companies_evaluated=companies_evaluated,
            contact_checkpoint=contact.to_dict(),
            contact_usage_events=contact_usage_events,
            contacts=contacts,
            leads_csv=leads_csv,
        )


def _required_json(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    if payload is None:
        raise ValueError(f"missing canary restart prerequisite: {path.name}")
    return payload


def _leads_text(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("leads.csv must be a regular canary restart prerequisite")
    data = path.read_bytes()
    if len(data) > _MAX_LEADS_BYTES:
        raise ValueError("leads.csv exceeds the fixed canary restart bound")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("leads.csv must be UTF-8") from exc
    if "\x00" in text:
        raise ValueError("leads.csv is invalid")
    return text


def _operation_map(checkpoint: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    """Return one checkpoint's operation map when its persisted shape is usable."""
    provider_state = checkpoint.get("provider_state")
    if not isinstance(provider_state, dict):
        return None
    operations = provider_state.get("operations", {})
    if not isinstance(operations, dict):
        return None
    if any(
        not isinstance(key, str) or not isinstance(value, dict)
        for key, value in operations.items()
    ):
        return None
    return cast(dict[str, dict[str, Any]], operations)


def _checkpoint_progresses(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Return whether a normal M4 checkpoint advances without changing paid identity."""
    previous_status = previous.get("status")
    current_status = current.get("status")
    if (
        previous_status not in _CONTACT_STATUS_RANK
        or current_status not in _CONTACT_STATUS_RANK
        or _CONTACT_STATUS_RANK[current_status]
        < _CONTACT_STATUS_RANK[previous_status]
    ):
        return False
    previous_operations = _operation_map(previous)
    current_operations = _operation_map(current)
    if previous_operations is None or current_operations is None:
        return False
    for operation_id, previous_entry in previous_operations.items():
        current_entry = current_operations.get(operation_id)
        if current_entry is None:
            return False
        for field in _OPERATION_IDENTITY_FIELDS:
            if field in previous_entry and current_entry.get(field) != previous_entry[field]:
                return False
        previous_operation_status = previous_entry.get("state")
        current_operation_status = current_entry.get("state")
        if (
            previous_operation_status not in _OPERATION_STATUS_RANK
            or current_operation_status not in _OPERATION_STATUS_RANK
            or _OPERATION_STATUS_RANK[current_operation_status]
            < _OPERATION_STATUS_RANK[previous_operation_status]
        ):
            return False
        previous_reads = previous_entry.get("status_reads_admitted", 0)
        current_reads = current_entry.get("status_reads_admitted", 0)
        if (
            isinstance(previous_reads, bool)
            or not isinstance(previous_reads, int)
            or isinstance(current_reads, bool)
            or not isinstance(current_reads, int)
            or current_reads < previous_reads
        ):
            return False
    return not (previous_status == "paused_pending" and any(
        operation_id not in previous_operations
        and entry.get("state") == "in_flight"
        for operation_id, entry in current_operations.items()
    ))


def _restart_update_is_safe(
    previous: CanaryRestartState,
    current: CanaryRestartState,
) -> bool:
    """Require a restart-capsule update to preserve normal authority and advance M4 state."""
    previous_normal = dict(previous.checkpoint)
    current_normal = dict(current.checkpoint)
    previous_normal.pop("updated_at", None)
    current_normal.pop("updated_at", None)
    if previous_normal != current_normal:
        return False
    if previous.usage_events != current.usage_events:
        return False
    if previous.companies_evaluated != current.companies_evaluated:
        return False
    if not _checkpoint_progresses(previous.contact_checkpoint, current.contact_checkpoint):
        return False
    if (
        current.contact_usage_events[: len(previous.contact_usage_events)]
        != previous.contact_usage_events
    ):
        return False
    if not current.leads_csv.startswith(previous.leads_csv):
        return False
    previous_contacts = {
        row.get("contact_id"): row.get("company_id")
        for row in previous.contacts
        if isinstance(row.get("contact_id"), str)
    }
    current_contacts = {
        row.get("contact_id"): row.get("company_id")
        for row in current.contacts
        if isinstance(row.get("contact_id"), str)
    }
    return all(
        current_contacts.get(contact_id) == company_id
        for contact_id, company_id in previous_contacts.items()
    )


def snapshot_canary_restart_state(run_dir: Path, *, run_id: str) -> None:
    """Mirror exactly the completed normal inputs needed after ephemeral runner loss."""
    if not git_journal_configured():
        return
    if run_dir.name != run_id or run_dir.is_symlink() or not run_dir.is_dir():
        raise ValueError("canary restart snapshot requires the exact run directory")
    state = CanaryRestartState.from_dict(
        {
            "checkpoint": _required_json(run_dir / "checkpoint.json"),
            "usage_events": load_jsonl(run_dir / "usage_events.jsonl"),
            "companies_evaluated": load_jsonl(
                run_dir / "companies_evaluated.jsonl"
            ),
            "contact_checkpoint": _required_json(
                run_dir / "contact_checkpoint.json"
            ),
            "contact_usage_events": load_jsonl(
                run_dir / "contact_usage_events.jsonl"
            ),
            "contacts": load_jsonl(run_dir / "contacts.jsonl"),
            "leads_csv": _leads_text(run_dir / "leads.csv"),
        },
        run_id=run_id,
    )
    durable_payload = load_canary_restart_state(run_id)
    if durable_payload is not None:
        durable = CanaryRestartState.from_dict(durable_payload, run_id=run_id)
        if durable == state:
            return
        if not _restart_update_is_safe(durable, state):
            raise RuntimeError(
                "completed normal canary state disagrees with durable restart state"
            )
    persist_canary_restart_state(run_id, state.to_dict())


def _matches_local(run_dir: Path, state: CanaryRestartState) -> bool:
    checks: tuple[tuple[str, object], ...] = (
        ("checkpoint.json", state.checkpoint),
        ("usage_events.jsonl", list(state.usage_events)),
        ("companies_evaluated.jsonl", list(state.companies_evaluated)),
        ("contact_checkpoint.json", state.contact_checkpoint),
        ("contact_usage_events.jsonl", list(state.contact_usage_events)),
        ("contacts.jsonl", list(state.contacts)),
        ("leads.csv", state.leads_csv),
    )
    for name, expected in checks:
        path = run_dir / name
        if not path.exists():
            continue
        if name == "leads.csv":
            if _leads_text(path) != expected:
                return False
        elif name.endswith(".jsonl"):
            if load_jsonl(path) != expected:
                return False
        elif read_json(path) != expected:
            return False
    return True


def restore_canary_restart_state(data_root: Path, *, run_id: str) -> bool:
    """Restore one valid durable normal snapshot before normal CLI dispatch is considered."""
    if not git_journal_configured():
        return False
    payload = load_canary_restart_state(run_id)
    if payload is None:
        if load_canary_private_state(run_id) is not None:
            raise RuntimeError(
                "canary private state lacks durable normal restart prerequisites"
            )
        return False
    state = CanaryRestartState.from_dict(payload, run_id=run_id)
    run_dir = canary_run_dir(data_root, run_id, require_existing=False)
    if not _matches_local(run_dir, state):
        raise RuntimeError(
            "runner-local normal canary state disagrees with durable restart state"
        )

    write_json_atomic(run_dir / "checkpoint.json", state.checkpoint)
    write_jsonl_atomic(run_dir / "usage_events.jsonl", state.usage_events)
    write_jsonl_atomic(
        run_dir / "companies_evaluated.jsonl",
        state.companies_evaluated,
    )
    write_json_atomic(run_dir / "contact_checkpoint.json", state.contact_checkpoint)
    write_jsonl_atomic(
        run_dir / "contact_usage_events.jsonl",
        state.contact_usage_events,
    )
    write_jsonl_atomic(run_dir / "contacts.jsonl", state.contacts)
    write_text_atomic(run_dir / "leads.csv", state.leads_csv)
    return True


__all__ = [
    "CanaryRestartState",
    "restore_canary_restart_state",
    "snapshot_canary_restart_state",
]
