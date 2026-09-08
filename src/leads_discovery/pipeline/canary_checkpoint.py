"""Canary-specific checkpoint orchestration over the generic state primitives."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from leads_discovery.models import RunCheckpoint, UsageEvent
from leads_discovery.pipeline.git_journal import (
    git_journal_configured,
    load_canary_private_state,
    sync_checkpoint_barrier,
)
from leads_discovery.pipeline.state import (
    _validate_usage_event,
    load_jsonl,
    read_json,
    write_json_atomic,
    write_jsonl_atomic,
)

_CANARY_PAID_CHECKPOINT = "canary_paid_checkpoint.json"
_CANARY_PAID_USAGE = "canary_paid_usage_events.jsonl"
_CONTACT_CHECKPOINT = "contact_checkpoint.json"


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


def read_canary_checkpoint(path: Path) -> dict[str, Any] | None:
    """Read a canary checkpoint, restoring private state when its local copy is absent."""
    if path.name != _CANARY_PAID_CHECKPOINT:
        return read_json(path)
    configured = git_journal_configured()
    remote = _validated_remote_canary_state(path)
    usage_path = path.parent / _CANARY_PAID_USAGE
    if remote is None:
        if configured and (path.exists() or usage_path.exists()):
            raise RuntimeError("canary private local state lacks a durable Git journal capsule")
        return read_json(path)
    remote_checkpoint, remote_usage = remote
    if path.exists():
        local_checkpoint = read_json(path)
        local_usage = load_jsonl(usage_path)
        if local_checkpoint != remote_checkpoint or local_usage != remote_usage:
            raise RuntimeError("canary private local state disagrees with durable Git journal")
        return local_checkpoint
    if usage_path.exists():
        raise RuntimeError("canary private local usage exists without its durable checkpoint")
    write_json_atomic(path, remote_checkpoint)
    write_jsonl_atomic(usage_path, remote_usage)
    return remote_checkpoint


def _snapshot_normal_canary_before_private_barrier(
    path: Path,
    checkpoint: RunCheckpoint,
    previous: RunCheckpoint | None,
) -> None:
    """Persist normal restart authority before the first private checkpoint barrier."""
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


def _snapshot_pending_normal_m4(path: Path, checkpoint: RunCheckpoint) -> None:
    """Refresh restart authority after normal M4 durably records resumable async work."""
    if (
        path.name != _CONTACT_CHECKPOINT
        or checkpoint.status != "paused_pending"
        or not git_journal_configured()
    ):
        return
    if path.parent.name != checkpoint.run_id:
        raise ValueError("canary contact checkpoint path must match its run_id")
    from leads_discovery.pipeline.canary_restart import snapshot_canary_restart_state

    snapshot_canary_restart_state(path.parent, run_id=checkpoint.run_id)


def write_checkpoint(path: Path, checkpoint: RunCheckpoint) -> None:
    """Persist a canary checkpoint after its remote barrier and usage snapshot are durable."""
    previous_payload = read_json(path) if path.exists() else None
    previous = (
        None if previous_payload is None else RunCheckpoint.from_dict(previous_payload)
    )
    _snapshot_normal_canary_before_private_barrier(path, checkpoint, previous)
    private_usage = (
        load_jsonl(path.parent / _CANARY_PAID_USAGE)
        if path.name == _CANARY_PAID_CHECKPOINT
        else None
    )
    sync_checkpoint_barrier(
        checkpoint,
        previous,
        private_usage=private_usage,
    )
    write_json_atomic(path, checkpoint.to_dict())
    _snapshot_pending_normal_m4(path, checkpoint)


__all__ = ["read_canary_checkpoint", "write_checkpoint"]
