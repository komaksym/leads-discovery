"""Shared durable lifecycle primitives for provider operations that may incur cost."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from leads_discovery.models import RunCheckpoint, UsageEvent
from leads_discovery.pipeline.costs import CostTracker
from leads_discovery.pipeline.state import append_usage_event

_OPERATION_STATES = frozenset({"in_flight", "completed", "failed", "pending"})
QuotaUnit = Literal["credits", "requests"]


def _finite_nonnegative(value: object, *, field_name: str) -> float:
    """Validate one persisted quota amount without accepting malformed numbers."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{field_name} must be a finite nonnegative number")
    return float(value)


def _event_quota_amount(
    event: UsageEvent,
    provider: str,
    *,
    operation: str | None,
    unit: QuotaUnit,
) -> float:
    """Return one event's contribution to a provider quota dimension."""
    if event.provider != provider:
        return 0.0
    if operation is not None and event.operation != operation:
        return 0.0
    if unit == "requests":
        return _finite_nonnegative(event.request_count, field_name="request_count")

    raw = event.metadata.get("credits_used")
    if raw is None and provider == "apollo":
        raw = event.metadata.get("credits_reserved", 1.0)
    if raw is None:
        return 0.0
    return _finite_nonnegative(raw, field_name=f"{provider} credits")


def replay_quota_totals(events: Iterable[UsageEvent]) -> tuple[float, int, float]:
    """Replay Apollo credits and Instantly create/credit totals from usage events."""
    apollo = 0.0
    instantly_create_calls = 0.0
    instantly_credits = 0.0
    for event in events:
        apollo += _event_quota_amount(
            event,
            "apollo",
            operation=None,
            unit="credits",
        )
        instantly_create_calls += _event_quota_amount(
            event,
            "instantly",
            operation="email_verification_create",
            unit="requests",
        )
        instantly_credits += _event_quota_amount(
            event,
            "instantly",
            operation=None,
            unit="credits",
        )
    return apollo, int(instantly_create_calls), instantly_credits


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
    usage_events: Iterable[UsageEvent] = ()
    _usage_events: list[UsageEvent] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Own a defensive replay snapshot of the authoritative usage ledger."""
        self._usage_events = [
            UsageEvent.from_dict(event.to_dict()) for event in self.usage_events
        ]

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
        provider: str,
        ceiling: float | None,
        reservation: float = 0.0,
        *,
        operation: str | None = None,
        unit: QuotaUnit | None = None,
    ) -> bool:
        """Apply admission against replayed provider quota owned by this lifecycle."""
        return reservation_fits(
            self.quota_used(provider, operation=operation, unit=unit),
            ceiling,
            reservation,
        )

    def quota_used(
        self,
        provider: str,
        *,
        operation: str | None = None,
        unit: QuotaUnit | None = None,
    ) -> float:
        """Return committed quota from replayed and newly recorded usage events."""
        if unit is None:
            unit = "credits" if provider == "apollo" else "requests"
        if unit not in {"credits", "requests"}:
            raise ValueError("quota unit must be credits or requests")
        return sum(
            _event_quota_amount(
                event,
                provider,
                operation=operation,
                unit=unit,
            )
            for event in self._usage_events
        )

    @classmethod
    def replay_quota_totals(
        cls, events: Iterable[UsageEvent]
    ) -> tuple[float, int, float]:
        """Replay the stable M4 quota summary through the lifecycle boundary."""
        return replay_quota_totals(events)

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
        stored = UsageEvent.from_dict(event.to_dict())
        append_usage_event(self.usage_path, stored)
        self.tracker.record(stored)
        self._usage_events.append(stored)
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
    "replay_quota_totals",
    "reservation_fits",
]
