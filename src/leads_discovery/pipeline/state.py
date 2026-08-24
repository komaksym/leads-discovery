"""Durable file-backed state helpers for resumable pipeline runs."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from leads_discovery.models import CompanyRecord, RunCheckpoint, UsageEvent


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry changes on POSIX filesystems when supported."""
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_write_target(path: Path) -> None:
    """Reject a pre-existing symlink so artifact writes cannot escape through it."""
    if path.is_symlink():
        raise ValueError(f"artifact path must not be a symlink: {path.name}")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append and fsync one JSON object without following a pre-existing artifact symlink."""
    _ensure_write_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_write_target(path)
    is_new = not path.exists()
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o666)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    if is_new:
        _fsync_directory(path.parent)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load JSONL objects while tolerating only a torn final append."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                break
            raise
        if not isinstance(payload, dict):
            raise ValueError(f"JSONL row {index + 1} must be an object")
        rows.append(cast(dict[str, Any], payload))
    return rows


def append_company_snapshot(path: Path, company: CompanyRecord) -> None:
    """Append and fsync one company snapshot so completed paid work survives interruption."""
    append_jsonl(path, company.to_dict())


def load_latest_company_records(path: Path) -> dict[str, CompanyRecord]:
    """Load the latest persisted snapshot for every company ID."""
    latest: dict[str, CompanyRecord] = {}
    for payload in load_jsonl(path):
        company = CompanyRecord.from_dict(payload)
        latest[company.company_id] = company
    return latest


def stage_completed(path: Path, company_id: str, stage: str) -> bool:
    """Return whether the latest persisted company snapshot marks a stage completed."""
    company = load_latest_company_records(path).get(company_id)
    return company is not None and company.stage_status.get(stage) == "completed"


def append_usage_event(path: Path, event: UsageEvent) -> None:
    """Append and fsync one provider usage event to the replayable ledger."""
    append_jsonl(path, event.to_dict())


def load_usage_events(path: Path) -> list[UsageEvent]:
    """Strictly deserialize persisted provider usage events from the append-only ledger."""
    events: list[UsageEvent] = []
    for payload in load_jsonl(path):
        event = UsageEvent.from_dict(payload)
        _validate_usage_event(event)
        events.append(event)
    return events


def _validate_usage_event(event: UsageEvent) -> None:
    """Reject malformed persisted usage values instead of coercing corrupted budget state."""
    if not isinstance(event.provider, str) or not event.provider:
        raise ValueError("usage provider must be a nonempty string")
    if not isinstance(event.operation, str) or not event.operation:
        raise ValueError("usage operation must be a nonempty string")
    for name, count_value in (
        ("request_count", event.request_count),
        ("input_tokens", event.input_tokens),
        ("output_tokens", event.output_tokens),
    ):
        if (
            isinstance(count_value, bool)
            or not isinstance(count_value, int)
            or count_value < 0
        ):
            raise ValueError(f"usage {name} must be a nonnegative integer")
    for name, cost_value in (
        ("estimated_cost_usd", event.estimated_cost_usd),
        ("exact_cost_usd", event.exact_cost_usd),
    ):
        if cost_value is not None and (
            isinstance(cost_value, bool)
            or not isinstance(cost_value, (int, float))
            or not math.isfinite(cost_value)
            or cost_value < 0
        ):
            raise ValueError(f"usage {name} must be a nonnegative number or null")
    if not isinstance(event.metadata, dict):
        raise ValueError("usage metadata must be an object")
    if not isinstance(event.recorded_at, str) or not event.recorded_at:
        raise ValueError("usage recorded_at must be a nonempty string")


def write_text_atomic(path: Path, text: str) -> None:
    """Atomically replace one UTF-8 text artifact without following symlinks."""
    _ensure_write_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _ensure_write_target(path)
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def write_jsonl_atomic(path: Path, payloads: Iterable[dict[str, Any]]) -> None:
    """Atomically replace one complete JSONL artifact without following symlinks."""
    lines = [
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        for payload in payloads
    ]
    text = "" if not lines else "\n".join(lines) + "\n"
    write_text_atomic(path, text)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace JSON without following or mutating a pre-existing artifact symlink."""
    _ensure_write_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _ensure_write_target(path)
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON artifact as a dictionary, or return None when it does not exist."""
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON artifact must contain an object")
    return cast(dict[str, Any], payload)


def write_checkpoint(path: Path, checkpoint: RunCheckpoint) -> None:
    """Atomically replace the run checkpoint with a fully written JSON document."""
    write_json_atomic(path, checkpoint.to_dict())


def load_checkpoint(path: Path) -> RunCheckpoint | None:
    """Load a persisted run checkpoint, or return None when no checkpoint exists."""
    payload = read_json(path)
    return None if payload is None else RunCheckpoint.from_dict(payload)
