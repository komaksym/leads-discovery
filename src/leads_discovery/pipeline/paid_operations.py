"""Shared durable lifecycle primitives for provider operations that may incur cost."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from leads_discovery.models import RunCheckpoint, UsageEvent
from leads_discovery.pipeline.costs import CostTracker
from leads_discovery.pipeline.state import append_usage_event, load_usage_events

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
        raw = self.checkpoint.provider_state.setdefault("operations", {})
        if not isinstance(raw, dict):
            raise ValueError("checkpoint operations must be an object")
        operations: dict[str, dict[str, Any]] = {}
        for operation_id, value in raw.items():
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError("checkpoint operation names must be nonblank strings")
            if not isinstance(value, dict):
                raise ValueError("checkpoint operation entries must be objects")
            state = value.get("state")
            if state not in _OPERATION_STATES:
                raise ValueError("checkpoint operation has an invalid state")
            operations[operation_id] = cast(dict[str, Any], value)
        self.checkpoint.provider_state["operations"] = operations
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

    def quota_used(
        self,
        provider: str,
        *,
        operation: str | None = None,
        metadata_field: str | None = None,
    ) -> float:
        """Replay a provider quota directly from the authoritative usage ledger."""
        total = 0.0
        for event in load_usage_events(self.usage_path):
            if event.provider != provider:
                continue
            if operation is not None and event.operation != operation:
                continue
            raw: object = (
                event.request_count
                if metadata_field is None
                else event.metadata.get(metadata_field, 0)
            )
            if (
                isinstance(raw, bool)
                or not isinstance(raw, (int, float))
                or not math.isfinite(raw)
                or raw < 0
            ):
                raise ValueError("provider quota usage must be a nonnegative finite number")
            total += float(raw)
        return total

    def pause(
        self,
        *,
        status: str,
        reason: str,
        company_id: str | None = None,
        stage: str | None = None,
    ) -> None:
        """Persist a lifecycle-owned paid-work pause or freeze."""
        if status not in {"paused_budget", "paused_unknown", "paused_pending"}:
            raise ValueError("paid lifecycle pause status is invalid")
        self.checkpoint.status = status
        self.checkpoint.pause_reason = reason
        self.checkpoint.pending_company_id = company_id
        self.checkpoint.pending_stage = stage
        self.persist_checkpoint()

    def admit(
        self,
        operation_id: str,
        *,
        provider: str,
        operation: str,
        ceiling: float | None,
        reservation_usd: float = 0.0,
        budget_reason: str,
        usage_unknown_reason: str,
        request_id: str | None = None,
        company_id: str | None = None,
        fields: Mapping[str, Any] | None = None,
        pending_stage: str | None = None,
        pause_on_budget: bool = True,
    ) -> bool:
        """Own budget admission and durable intent as one pre-dispatch decision."""
        committed = self.tracker.provider_estimated_spend(provider)
        if ceiling is not None and committed is None:
            self.pause(
                status="paused_unknown",
                reason=usage_unknown_reason,
                company_id=company_id,
                stage=pending_stage or operation,
            )
            return False
        if not reservation_fits(committed, ceiling, reservation_usd):
            if pause_on_budget:
                self.pause(
                    status="paused_budget",
                    reason=budget_reason,
                    company_id=company_id,
                    stage=pending_stage or operation,
                )
            return False
        self.begin(
            operation_id,
            provider=provider,
            operation=operation,
            fields=fields,
            request_id=request_id,
            company_id=company_id,
            reservation_usd=reservation_usd,
            pending_stage=pending_stage,
        )
        return True

    def admit_quota(
        self,
        operation_id: str,
        *,
        provider: str,
        operation: str,
        ceiling: float | None,
        reservation: float,
        budget_reason: str,
        quota_operation: str | None = None,
        metadata_field: str | None = None,
        company_id: str | None = None,
        fields: Mapping[str, Any] | None = None,
        pending_stage: str | None = None,
    ) -> bool:
        """Check an event-backed quota and persist intent before dispatch."""
        used = self.quota_used(
            provider,
            operation=quota_operation,
            metadata_field=metadata_field,
        )
        if not reservation_fits(used, ceiling, reservation):
            self.pause(
                status="paused_budget",
                reason=budget_reason,
                company_id=company_id,
                stage=pending_stage or operation,
            )
            return False
        self.begin(
            operation_id,
            provider=provider,
            operation=operation,
            fields=fields,
            company_id=company_id,
            pending_stage=pending_stage,
        )
        return True

    def reserve_continuation(
        self,
        operation_id: str,
        *,
        provider: str,
        ceiling: float | None,
        reservation_usd: float,
        budget_reason: str,
        usage_unknown_reason: str,
        fields: Mapping[str, Any] | None = None,
        company_id: str | None = None,
        stage: str | None = None,
    ) -> bool:
        """Own admission for the next call inside an already-authorized resumable operation."""
        operations = self.operations()
        if operation_id not in operations:
            raise ValueError("paid operation intent is missing")
        entry = operations[operation_id]
        entry.update(deepcopy(dict(fields or {})))
        committed = self.tracker.provider_estimated_spend(provider)
        if ceiling is not None and committed is None:
            entry["state"] = "pending"
            entry.pop("reservation_usd", None)
            self.pause(
                status="paused_unknown",
                reason=usage_unknown_reason,
                company_id=company_id,
                stage=stage,
            )
            return False
        if not reservation_fits(committed, ceiling, reservation_usd):
            entry["state"] = "pending"
            entry["reservation_usd"] = reservation_usd
            self.pause(
                status="paused_budget",
                reason=budget_reason,
                company_id=company_id,
                stage=stage,
            )
            return False
        entry["reservation_usd"] = reservation_usd
        self.persist_checkpoint()
        return True

    def update_operation(
        self,
        operation_id: str,
        *,
        fields: Mapping[str, Any] | None = None,
        state: str | None = None,
        clear_pending: bool = False,
    ) -> None:
        """Update lifecycle-owned persisted operation metadata without exposing storage mutation."""
        operations = self.operations()
        if operation_id not in operations:
            raise ValueError("paid operation intent is missing")
        entry = operations[operation_id]
        entry.update(deepcopy(dict(fields or {})))
        if state is not None:
            if state not in _OPERATION_STATES:
                raise ValueError("paid operation has an invalid transition state")
            entry["state"] = state
        if clear_pending:
            self.checkpoint.pending_company_id = None
            self.checkpoint.pending_stage = None
        self.persist_checkpoint()

    def freeze_if_unknown(
        self,
        *,
        replayable: Callable[[str, Mapping[str, Any]], bool] | None = None,
        reason_prefix: str = "unknown_in_flight",
    ) -> tuple[str, dict[str, Any]] | None:
        """Freeze the run when unresolved paid work cannot be proven safe to resume."""
        unknown = self.unknown_in_flight(replayable=replayable)
        if unknown is None:
            return None
        operation_id, entry = unknown
        company_id = entry.get("company_id")
        stage = entry.get("operation")
        self.pause(
            status="paused_unknown",
            reason=f"{reason_prefix}:{operation_id}",
            company_id=company_id if isinstance(company_id, str) else None,
            stage=stage if isinstance(stage, str) else None,
        )
        return unknown

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


__all__ = ["PaidOperationLifecycle", "find_unknown_in_flight", "reservation_fits"]
