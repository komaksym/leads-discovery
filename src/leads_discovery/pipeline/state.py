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
from leads_discovery.pipeline.git_journal import (
    git_journal_configured,
    load_canary_private_state,
    persist_canary_private_state,
    sync_checkpoint_barrier,
)

_DEFAULT_MAX_RECORD_BYTES = 256 * 1024
_DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024
_DEFAULT_MAX_RUN_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_RECORDS = 10_000
_CANARY_PAID_CHECKPOINT = "canary_paid_checkpoint.json"
_CANARY_PAID_USAGE = "canary_paid_usage_events.jsonl"


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


def _run_limit() -> int:
    """Return the maximum total bytes allowed in one run directory."""
    return _positive_limit("LEADS_MAX_RUN_BYTES", _DEFAULT_MAX_RUN_BYTES)


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


def _directory_bytes(parent: Path, *, exclude: Path | None = None) -> int:
    """Count regular run files while rejecting symlinks in persisted state."""
    if not parent.exists():
        return 0
    _ensure_directory(parent)
    total = 0
    for child in parent.iterdir():
        if exclude is not None and child == exclude:
            continue
        if child.is_symlink():
            raise ValueError("run directory must not contain symlinks")
        if child.is_file():
            total += child.stat().st_size
    return total


def _ensure_existing_run_size(parent: Path) -> None:
    """Reject persisted state whose aggregate run bytes exceed the configured ceiling."""
    if _directory_bytes(parent) > _run_limit():
        raise ValueError("persisted run exceeds LEADS_MAX_RUN_BYTES")


def _ensure_run_size(path: Path, final_size: int) -> None:
    """Reject a write before its final run-directory size would exceed the ceiling."""
    if _directory_bytes(path.parent, exclude=path) + final_size > _run_limit():
        raise ValueError("persisted run would exceed LEADS_MAX_RUN_BYTES")


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
    current = path.stat().st_size if path.exists() else 0
    _ensure_run_size(path, current + len(data))
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
    _ensure_existing_run_size(path.parent)
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
    _ensure_run_size(path, len(data))
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
    base_run_bytes = _directory_bytes(path.parent, exclude=path)
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
                if base_run_bytes + total > _run_limit():
                    raise ValueError("persisted run would exceed LEADS_MAX_RUN_BYTES")
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


def _read_json_file(path: Path) -> dict[str, Any]:
    """Read one existing bounded JSON object without triggering remote restoration."""
    _ensure_existing_run_size(path.parent)
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


def _validated_remote_canary_state(
    checkpoint_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Load and validate the encrypted private restart capsule before local use."""
    run_id = checkpoint_path.parent.name
    remote = load_canary_private_state(run_id)
    if remote is None:
        return None
    if set(remote) != {"checkpoint", "usage_events"}:
        raise ValueError("canary private Git journal state shape is invalid")
    checkpoint_payload = remote.get("checkpoint")
    usage_payloads = remote.get("usage_events")
    if not isinstance(checkpoint_payload, dict) or not isinstance(usage_payloads, list):
        raise ValueError("canary private Git journal state is invalid")
    checkpoint = RunCheckpoint.from_dict(checkpoint_payload)
    if checkpoint.run_id != run_id:
        raise ValueError("canary private Git journal run_id mismatch")
    validated_usage: list[dict[str, Any]] = []
    for payload in usage_payloads:
        if not isinstance(payload, dict):
            raise ValueError("canary private Git journal usage row is invalid")
        event = UsageEvent.from_dict(payload)
        _validate_usage_event(event)
        validated_usage.append(event.to_dict())
    return checkpoint.to_dict(), validated_usage


def _restore_or_validate_canary_private_state(checkpoint_path: Path) -> None:
    """Restore a lost runner-local private state or reject disagreement with durable state."""
    if checkpoint_path.name != _CANARY_PAID_CHECKPOINT:
        return
    configured = git_journal_configured()
    remote = _validated_remote_canary_state(checkpoint_path)
    usage_path = checkpoint_path.parent / _CANARY_PAID_USAGE
    if remote is None:
        if configured and (checkpoint_path.exists() or usage_path.exists()):
            raise RuntimeError("canary private local state lacks a durable Git journal capsule")
        return
    remote_checkpoint, remote_usage = remote
    if checkpoint_path.exists():
        local_checkpoint = _read_json_file(checkpoint_path)
        local_usage = load_jsonl(usage_path)
        if local_checkpoint != remote_checkpoint or local_usage != remote_usage:
            raise RuntimeError("canary private local state disagrees with durable Git journal")
        return
    if usage_path.exists():
        raise RuntimeError("canary private local usage exists without its durable checkpoint")
    write_json_atomic(checkpoint_path, remote_checkpoint)
    write_jsonl_atomic(usage_path, remote_usage)


def read_json(path: Path) -> dict[str, Any] | None:
    """Read one bounded JSON object, restoring configured canary-private restart state."""
    _restore_or_validate_canary_private_state(path)
    if not path.exists():
        return None
    return _read_json_file(path)


def _persist_canary_private_state(path: Path, checkpoint: RunCheckpoint) -> None:
    """Mirror exact private checkpoint+usage authority after each local checkpoint transition."""
    if path.name != _CANARY_PAID_CHECKPOINT:
        return
    if path.parent.name != checkpoint.run_id:
        raise ValueError("canary private checkpoint path must match its run_id")
    usage_payloads = load_jsonl(path.parent / _CANARY_PAID_USAGE)
    persist_canary_private_state(
        checkpoint.run_id,
        {
            "checkpoint": checkpoint.to_dict(),
            "usage_events": usage_payloads,
        },
    )


def _snapshot_normal_canary_before_first_private_barrier(
    path: Path,
    checkpoint: RunCheckpoint,
    previous: RunCheckpoint | None,
) -> None:
    """Persist bounded normal restart authority before the first private paid barrier."""
    if (
        previous is not None
        or path.name != _CANARY_PAID_CHECKPOINT
        or not git_journal_configured()
    ):
        return
    if path.parent.name != checkpoint.run_id:
        raise ValueError("canary private checkpoint path must match its run_id")
    from leads_discovery.pipeline.canary_restart import snapshot_canary_restart_state

    snapshot_canary_restart_state(path.parent, run_id=checkpoint.run_id)


def write_checkpoint(path: Path, checkpoint: RunCheckpoint) -> None:
    """Durably publish paid-operation barriers before atomically replacing local checkpoint."""
    previous_payload = _read_json_file(path) if path.exists() else None
    previous = (
        None if previous_payload is None else RunCheckpoint.from_dict(previous_payload)
    )
    _snapshot_normal_canary_before_first_private_barrier(path, checkpoint, previous)
    sync_checkpoint_barrier(checkpoint, previous)
    write_json_atomic(path, checkpoint.to_dict())
    _persist_canary_private_state(path, checkpoint)


def load_checkpoint(path: Path) -> RunCheckpoint | None:
    """Load a persisted run checkpoint, or return None when no checkpoint exists."""
    payload = read_json(path)
    return None if payload is None else RunCheckpoint.from_dict(payload)
