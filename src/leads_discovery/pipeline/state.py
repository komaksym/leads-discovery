"""Durable bounded file-backed state helpers for resumable pipeline runs."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, cast

from leads_discovery.models import CompanyRecord, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.git_journal import sync_checkpoint_barrier

_DEFAULT_MAX_RECORD_BYTES = 256 * 1024
_DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024
_DEFAULT_MAX_RECORDS = 10_000


def _positive_limit(name: str, default: int) -> int:
    """Read one optional positive integer resource limit from the environment."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _record_limit() -> int:
    """Return the maximum serialized bytes accepted for one persisted record/line."""
    return _positive_limit("LEADS_MAX_RECORD_BYTES", _DEFAULT_MAX_RECORD_BYTES)


def _file_limit() -> int:
    """Return the maximum bytes accepted for one persisted JSON/JSONL artifact."""
    return _positive_limit("LEADS_MAX_FILE_BYTES", _DEFAULT_MAX_FILE_BYTES)


def _record_count_limit() -> int:
    """Return the maximum number of records accepted from one JSONL artifact."""
    return _positive_limit("LEADS_MAX_RECORDS", _DEFAULT_MAX_RECORDS)


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry changes on POSIX filesystems when supported."""
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_directory(path: Path) -> None:
    """Require an existing directory path whose final component is not a symlink."""
    if path.is_symlink():
        raise ValueError(f"artifact directory must not be a symlink: {path}")
    if not path.is_dir():
        raise ValueError(f"artifact parent must be a directory: {path}")


def _ensure_write_target(path: Path) -> None:
    """Reject symlinked targets or parent directories before file mutation."""
    parent = path.parent
    if parent.exists():
        _ensure_directory(parent)
    if path.is_symlink():
        raise ValueError(f"artifact path must not be a symlink: {path.name}")


def _serialized_line(payload: dict[str, Any]) -> bytes:
    """Serialize one JSONL object and enforce the configured per-record bound."""
    text = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ) + "\n"
    data = text.encode("utf-8")
    if len(data) > _record_limit():
        raise ValueError("persisted JSONL record exceeds LEADS_MAX_RECORD_BYTES")
    return data


def _ensure_file_growth(path: Path, added_bytes: int) -> None:
    """Fail before a write would exceed the configured per-artifact byte ceiling."""
    current = path.stat().st_size if path.exists() else 0
    if current + added_bytes > _file_limit():
        raise ValueError("persisted artifact would exceed LEADS_MAX_FILE_BYTES")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append/fsync one bounded JSON object without following artifact symlinks."""
    data = _serialized_line(payload)
    _ensure_write_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_directory(path.parent)
    _ensure_write_target(path)
    _ensure_file_growth(path, len(data))
    is_new = not path.exists()
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o666)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    if is_new:
        _fsync_directory(path.parent)


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Stream bounded JSONL rows, tolerating only one torn final append."""
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        raise ValueError("JSONL artifact must be a regular non-symlink file")
    size = path.stat().st_size
    if size > _file_limit():
        raise ValueError("JSONL artifact exceeds LEADS_MAX_FILE_BYTES")
    count = 0
    with path.open("rb") as handle:
        while True:
            line = handle.readline(_record_limit() + 1)
            if not line:
                break
            if len(line) > _record_limit():
                raise ValueError("JSONL record exceeds LEADS_MAX_RECORD_BYTES")
            if not line.strip():
                continue
            count += 1
            if count > _record_count_limit():
                raise ValueError("JSONL artifact exceeds LEADS_MAX_RECORDS")
            try:
                text = line.decode("utf-8")
                payload = json.loads(text)
            except (UnicodeDecodeError, json.JSONDecodeError):
                if handle.tell() == size and not line.endswith(b"\n"):
                    break
                raise ValueError(f"invalid JSONL row {count}") from None
            if not isinstance(payload, dict):
                raise ValueError(f"JSONL row {count} must be an object")
            yield cast(dict[str, Any], payload)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load JSONL objects through the bounded streaming replay parser."""
    return list(iter_jsonl(path))


def append_company_snapshot(path: Path, company: CompanyRecord) -> None:
    """Append and fsync one company snapshot so completed paid work survives interruption."""
    append_jsonl(path, company.to_dict())


def load_latest_company_records(path: Path) -> dict[str, CompanyRecord]:
    """Stream the latest persisted snapshot for every bounded company ID."""
    latest: dict[str, CompanyRecord] = {}
    for payload in iter_jsonl(path):
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


def iter_usage_events(path: Path) -> Iterator[UsageEvent]:
    """Stream and strictly validate usage events without loading the ledger at once."""
    for payload in iter_jsonl(path):
        event = UsageEvent.from_dict(payload)
        _validate_usage_event(event)
        yield event


def load_usage_events(path: Path) -> list[UsageEvent]:
    """Deserialize the bounded provider usage ledger for compatibility callers."""
    return list(iter_usage_events(path))


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


def _bounded_text(text: str) -> bytes:
    """Encode one complete atomic artifact and enforce the configured file bound."""
    data = text.encode("utf-8")
    if len(data) > _file_limit():
        raise ValueError("persisted artifact exceeds LEADS_MAX_FILE_BYTES")
    return data


def write_text_atomic(path: Path, text: str) -> None:
    """Atomically replace one bounded UTF-8 artifact without following symlinks."""
    data = _bounded_text(text)
    _ensure_write_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_directory(path.parent)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _ensure_directory(path.parent)
        _ensure_write_target(path)
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def write_jsonl_atomic(path: Path, payloads: Iterable[dict[str, Any]]) -> None:
    """Stream a complete bounded JSONL snapshot into one same-directory temporary file."""
    _ensure_write_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_directory(path.parent)
    temp_path: Path | None = None
    total = 0
    count = 0
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            for payload in payloads:
                count += 1
                if count > _record_count_limit():
                    raise ValueError("JSONL artifact exceeds LEADS_MAX_RECORDS")
                data = _serialized_line(payload)
                total += len(data)
                if total > _file_limit():
                    raise ValueError("persisted artifact exceeds LEADS_MAX_FILE_BYTES")
                handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _ensure_directory(path.parent)
        _ensure_write_target(path)
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace one bounded JSON object without following symlinks."""
    text = json.dumps(
        payload,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"
    write_text_atomic(path, text)


def read_json(path: Path) -> dict[str, Any] | None:
    """Read one bounded JSON object, or return None when it does not exist."""
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("JSON artifact must be a regular non-symlink file")
    size = path.stat().st_size
    if size > _file_limit():
        raise ValueError("JSON artifact exceeds LEADS_MAX_FILE_BYTES")
    data = path.read_bytes()
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("JSON artifact is malformed") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON artifact must contain an object")
    return cast(dict[str, Any], payload)


def write_checkpoint(path: Path, checkpoint: RunCheckpoint) -> None:
    """Durably publish paid-operation barriers before atomically replacing local checkpoint."""
    previous_payload = read_json(path) if path.exists() else None
    previous = (
        None if previous_payload is None else RunCheckpoint.from_dict(previous_payload)
    )
    sync_checkpoint_barrier(checkpoint, previous)
    write_json_atomic(path, checkpoint.to_dict())


def load_checkpoint(path: Path) -> RunCheckpoint | None:
    """Load a persisted run checkpoint, or return None when no checkpoint exists."""
    payload = read_json(path)
    return None if payload is None else RunCheckpoint.from_dict(payload)
