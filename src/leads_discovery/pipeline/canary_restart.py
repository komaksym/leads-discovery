"""Typed canary restart adapter for bounded production-derived normal prerequisites."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.models import CompanyRecord, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.git_journal import (
    git_journal_configured,
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

_RUN_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_LEADS_BYTES: Final[int] = 256 * 1024


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
        if normal.status != "completed" or contact.status != "completed":
            raise ValueError("canary normal restart prerequisites must be completed")

        def rows(name: str) -> tuple[dict[str, Any], ...]:
            value = payload[name]
            if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
                raise ValueError(f"canary normal restart {name} is invalid")
            return tuple(value)

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


def _run_dir(data_root: Path, run_id: str, *, require_existing: bool) -> Path:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    expanded = data_root.expanduser()
    if expanded.is_symlink():
        raise ValueError("data_root must not be a symlink")
    root = expanded.resolve()
    if require_existing and not root.is_dir():
        raise ValueError("canary data_root must exist")
    if not require_existing:
        root.mkdir(parents=True, exist_ok=True)
    candidate = root / run_id
    if candidate.is_symlink():
        raise ValueError("canary run directory must not be a symlink")
    run_dir = candidate.resolve()
    if run_dir.parent != root:
        raise ValueError("canary run directory must remain directly beneath data_root")
    if require_existing:
        if not run_dir.is_dir():
            raise ValueError("canary run directory must exist")
    else:
        run_dir.mkdir(exist_ok=True)
    return run_dir


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
            "companies_evaluated": load_jsonl(run_dir / "companies_evaluated.jsonl"),
            "contact_checkpoint": _required_json(run_dir / "contact_checkpoint.json"),
            "contact_usage_events": load_jsonl(run_dir / "contact_usage_events.jsonl"),
            "contacts": load_jsonl(run_dir / "contacts.jsonl"),
            "leads_csv": _leads_text(run_dir / "leads.csv"),
        },
        run_id=run_id,
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
        return False
    state = CanaryRestartState.from_dict(payload, run_id=run_id)
    run_dir = _run_dir(data_root, run_id, require_existing=False)
    if not _matches_local(run_dir, state):
        raise RuntimeError("runner-local normal canary state disagrees with durable restart state")

    write_json_atomic(run_dir / "checkpoint.json", state.checkpoint)
    write_jsonl_atomic(run_dir / "usage_events.jsonl", state.usage_events)
    write_jsonl_atomic(run_dir / "companies_evaluated.jsonl", state.companies_evaluated)
    write_json_atomic(run_dir / "contact_checkpoint.json", state.contact_checkpoint)
    write_jsonl_atomic(run_dir / "contact_usage_events.jsonl", state.contact_usage_events)
    write_jsonl_atomic(run_dir / "contacts.jsonl", state.contacts)
    write_text_atomic(run_dir / "leads.csv", state.leads_csv)
    return True


__all__ = [
    "CanaryRestartState",
    "restore_canary_restart_state",
    "snapshot_canary_restart_state",
]
