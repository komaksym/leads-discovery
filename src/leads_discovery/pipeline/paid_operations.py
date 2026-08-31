"""Shared durable lifecycle primitives for provider operations that may incur cost."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from leads_discovery.models import RunCheckpoint, UsageEvent
from leads_discovery.pipeline.costs import CostTracker
from leads_discovery.pipeline.state import append_usage_event

_OPERATION_STATES = frozenset({"in_flight", "completed", "failed", "pending"})


def reservation_fits(
    committed: float | None,
    ceiling: float | None,
    reservation: float = 0.0,
) -> bool:
    """Return whether known committed usage plus a next reservation fits a hard ceiling."""
    if ceiling is None:
        return True
    if committed is None:
        return False
    return committed + reservation <= ceiling + 1e-12


def find_unknown_in_flight(
    operations: Mapping[str, Mapping[str, Any]],
    *,
    replayable: Callable[[str, Mapping[str, Any]], bool] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Return the first in-flight operation that is not explicitly safe to resume."""
    for operation_id, value in sorted(operations.items()):
        entry = dict(value)
        if entry.get("state") != "in_flight":
            continue
        if replayable is not None and replayable(operation_id, entry):
            continue
        return operation_id, cast(dict[str, Any], value)
    return None


def checkpoint_has_unknown_paid_work(checkpoint: RunCheckpoint) -> bool:
    """Fail closed when a run checkpoint records an unresolved paid outcome."""
    if checkpoint.status == "paused_unknown":
        return True
    try:
        operations = _operation_map(checkpoint)
    except ValueError:
        return True
    return find_unknown_in_flight(operations) is not None


def _operation_map(checkpoint: RunCheckpoint) -> dict[str, dict[str, Any]]:
    """Return the mutable operation mapping after validating its persisted container shape."""
    raw = checkpoint.provider_state.setdefault("operations", {})
    if not isinstance(raw, dict):
        raise ValueError("checkpoint operations must be an object")
    operations: dict[str, dict[str, Any]] = {}
    for operation_id, value in raw.items():
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("checkpoint operation names must be nonblank strings")
        if not isinstance(value, dict):
            raise ValueError("checkpoint operation entries must be objects")
        operations[operation_id] = cast(dict[str, Any], value)
    checkpoint.provider_state["operations"] = operations
    return operations


@dataclass(slots=True)
class PaidOperationLifecycle:
    """Own intent, usage, transition, and replay-barrier mechanics for paid operations."""

    checkpoint: RunCheckpoint
    tracker: CostTracker
    usage_path: Path
    persist_checkpoint: Callable[[], None]
    publish_usage: Callable[[], None]

    def operations(self) -> dict[str, dict[str, Any]]:
        """Return the shared mutable operation map after validating its basic shape."""
        operations = _operation_map(self.checkpoint)
        for value in operations.values():
            state = value.get("state")
            if state not in _OPERATION_STATES:
                raise ValueError("checkpoint operation has an invalid state")
        return operations

    def budget_allows(
        self,
        provider: str,
        ceiling: float | None,
        reservation: float = 0.0,
    ) -> bool:
        """Require replayed provider spend plus the next worst-case reservation to fit."""
        return reservation_fits(
            self.tracker.provider_estimated_spend(provider), ceiling, reservation
        )

    def quota_allows(
        self,
        committed: float,
        ceiling: float | None,
        reservation: float = 0.0,
    ) -> bool:
        """Apply one replayed non-dollar quota admission through the shared lifecycle."""
        return reservation_fits(committed, ceiling, reservation)

    def begin(
        self,
        operation_id: str,
        *,
        provider: str | None = None,
        operation: str | None = None,
        fields: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        company_id: str | None = None,
        reservation_usd: float | None = None,
        pending_stage: str | None = None,
        include_identity: bool = True,
    ) -> None:
        """Persist a complete paid-operation intent before dispatching to a provider."""
        entry = deepcopy(dict(fields or {}))
        if include_identity:
            if provider is None or operation is None:
                raise ValueError("provider and operation are required for identified operations")
            entry["provider"] = provider
            entry["operation"] = operation
            if request_id is not None:
                entry["request_id"] = request_id
            if company_id is not None:
                entry["company_id"] = company_id
        if reservation_usd is not None:
            entry["reservation_usd"] = reservation_usd
        entry["state"] = "in_flight"
        self.operations()[operation_id] = entry
        self.checkpoint.status = "running"
        self.checkpoint.pause_reason = None
        self.checkpoint.pending_company_id = company_id
        self.checkpoint.pending_stage = pending_stage
        self.persist_checkpoint()

    def finish(
        self,
        operation_id: str,
        *,
        state: str = "completed",
        fields: Mapping[str, Any] | None = None,
        error_kind: str | None = None,
        replace: bool = False,
    ) -> None:
        """Persist a known operation outcome after its usage and outputs are durable."""
        if state not in _OPERATION_STATES:
            raise ValueError("paid operation has an invalid transition state")
        operations = self.operations()
        if operation_id not in operations:
            raise ValueError("paid operation intent is missing")
        if replace:
            entry = deepcopy(dict(fields or {}))
            entry["state"] = state
            operations[operation_id] = entry
        else:
            entry = operations[operation_id]
            entry.update(deepcopy(dict(fields or {})))
            entry["state"] = state
            entry.pop("reservation_usd", None)
        if error_kind is not None:
            entry["error_kind"] = error_kind
        self.checkpoint.pending_company_id = None
        self.checkpoint.pending_stage = None
        self.persist_checkpoint()

    def record_usage(self, event: UsageEvent) -> None:
        """Append one authoritative usage event and refresh its derived summary."""
        append_usage_event(self.usage_path, event)
        self.tracker.record(event)
        self.publish_usage()

    def unknown_in_flight(
        self,
        *,
        replayable: Callable[[str, Mapping[str, Any]], bool] | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        """Return the first in-flight operation whose outcome cannot be safely replayed."""
        return find_unknown_in_flight(self.operations(), replayable=replayable)


__all__ = [
    "PaidOperationLifecycle",
    "checkpoint_has_unknown_paid_work",
    "find_unknown_in_flight",
    "reservation_fits",
]
