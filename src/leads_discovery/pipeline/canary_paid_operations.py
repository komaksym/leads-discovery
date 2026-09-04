"""Canary-private paid-operation state layered on the shared lifecycle."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from leads_discovery.models import RunCheckpoint, UsageEvent
from leads_discovery.pipeline.costs import CostTracker
from leads_discovery.pipeline.paid_operations import PaidOperationLifecycle, transition_checkpoint
from leads_discovery.pipeline.state import load_usage_events, read_json, write_checkpoint

ResourceName = Literal[
    "exa_people_search",
    "clay_start",
    "clay_status_read",
    "apollo_enrichment",
    "instantly_create",
    "instantly_status_read",
]
OutcomeState = Literal["completed", "pending", "failed"]

_TIMESTAMP_KEYS: Final[frozenset[str]] = frozenset(
    {"created_at", "updated_at", "retrieved_at", "recorded_at"}
)
_VALID_STATES: Final[frozenset[str]] = frozenset(
    {"in_flight", "completed", "failed", "pending"}
)
_PRIVATE_CHECKPOINT_STATUSES: Final[frozenset[str]] = frozenset(
    {"running", "completed"}
)
_DISPATCH_ID_METADATA_KEY: Final[str] = "canary_private_dispatch_id"
_RESERVED_OPERATION_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "state",
        "provider",
        "operation",
        "resource",
        "dispatch_resource",
        "dispatch_sequence",
        "dispatch_id",
        "input_fingerprint",
        "dispatch_usage_recorded",
    }
)


@dataclass(frozen=True, slots=True)
class _ResourceQuota:
    """Describe one immutable fixed-canary quota dimension."""

    provider: str
    operation: str
    ceiling: float
    reservation: float
    unit: Literal["credits", "requests"]
    budget_ceiling: float | None = None
    budget_reservation: float = 0.0


_RESOURCE_QUOTAS: Final[dict[str, _ResourceQuota]] = {
    "exa_people_search": _ResourceQuota(
        "exa",
        "people_search",
        1.0,
        1.0,
        "requests",
        budget_ceiling=0.02,
        budget_reservation=0.017,
    ),
    "clay_start": _ResourceQuota(
        "clay", "work_email_routine_start", 1.0, 1.0, "requests"
    ),
    "clay_status_read": _ResourceQuota(
        "clay", "work_email_routine_results", 3.0, 1.0, "requests"
    ),
    "apollo_enrichment": _ResourceQuota(
        "apollo", "people_enrichment", 1.0, 1.0, "credits"
    ),
    "instantly_create": _ResourceQuota(
        "instantly", "email_verification_create", 1.0, 1.0, "requests"
    ),
    "instantly_status_read": _ResourceQuota(
        "instantly", "email_verification_get", 3.0, 1.0, "requests"
    ),
}
_ASYNC_READ_RESOURCE: Final[dict[str, str]] = {
    "clay_start": "clay_status_read",
    "instantly_create": "instantly_status_read",
}


def _canonical_input(value: object) -> object:
    """Return a deterministic JSON-safe input view without volatile timestamps."""
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("canary paid input keys must be strings")
            if key not in _TIMESTAMP_KEYS:
                result[key] = _canonical_input(nested)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_input(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canary paid input numbers must be finite")
        return value
    raise ValueError("canary paid input must be JSON-safe")


def _input_fingerprint(value: object) -> str:
    """Hash production-derived input without persisting the input itself."""
    encoded = json.dumps(
        _canonical_input(value),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: object) -> bool:
    """Return whether one persisted identity is a canonical lowercase SHA-256 hex digest."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _dispatch_identity(
    run_id: str,
    operation_id: str,
    resource: str,
    sequence: int,
) -> str:
    """Derive one durable identity for a logical provider dispatch."""
    encoded = json.dumps(
        [run_id, operation_id, resource, sequence],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normal_checkpoint_blocks(payload: dict[str, Any], run_id: str) -> bool:
    """Fail closed unless one normal paid-work domain is terminal and resolved."""
    checkpoint = RunCheckpoint.from_dict(payload)
    if checkpoint.run_id != run_id:
        raise ValueError("normal checkpoint run_id mismatch")
    if checkpoint.status != "completed":
        return True
    if not isinstance(checkpoint.provider_state, dict):
        return True
    raw = checkpoint.provider_state.get("operations", {})
    if not isinstance(raw, dict):
        return True
    for operation_id, value in raw.items():
        if not isinstance(operation_id, str) or not operation_id or not isinstance(value, dict):
            return True
        state = value.get("state")
        if state not in _VALID_STATES or state in {"in_flight", "pending"}:
            return True
    return False


def _usage_resource_allowed(initial_resource: str, usage_resource: str) -> bool:
    """Allow primary usage or a bounded status read for the same async operation."""
    return usage_resource == initial_resource or _ASYNC_READ_RESOURCE.get(
        initial_resource
    ) == usage_resource


def _validate_private_operation(
    run_id: str,
    operation_id: object,
    value: object,
) -> tuple[str, int, str]:
    """Validate one persisted private operation and return its dispatch identity parts."""
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("canary paid operation names must be nonblank strings")
    if not isinstance(value, dict):
        raise ValueError("canary paid operation entries must be objects")
    state = value.get("state")
    if state not in _VALID_STATES:
        raise ValueError("canary paid operation has an invalid state")
    resource = value.get("resource")
    if not isinstance(resource, str) or resource not in _RESOURCE_QUOTAS:
        raise ValueError("canary paid operation resource is invalid")
    quota = _RESOURCE_QUOTAS[resource]
    if value.get("provider") != quota.provider or value.get("operation") != quota.operation:
        raise ValueError("canary paid operation identity is invalid")
    fingerprint = value.get("input_fingerprint")
    if not _is_sha256(fingerprint):
        raise ValueError("canary paid operation input fingerprint is invalid")
    sequence = value.get("dispatch_sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("canary paid dispatch sequence is invalid")
    expected_resource = resource if sequence == 0 else _ASYNC_READ_RESOURCE.get(resource)
    dispatch_resource = value.get("dispatch_resource")
    if expected_resource is None or dispatch_resource != expected_resource:
        raise ValueError("canary paid dispatch resource is invalid")
    dispatch_id = value.get("dispatch_id")
    expected_id = _dispatch_identity(run_id, operation_id, dispatch_resource, sequence)
    if (
        not isinstance(dispatch_id, str)
        or not _is_sha256(dispatch_id)
        or dispatch_id != expected_id
    ):
        raise ValueError("canary paid dispatch identity is invalid")
    usage_recorded = value.get("dispatch_usage_recorded")
    if not isinstance(usage_recorded, bool):
        raise ValueError("canary paid usage-recorded flag is invalid")
    if state in {"completed", "failed", "pending"} and not usage_recorded:
        raise ValueError("canary paid resolved operation is missing authoritative usage")
    if state == "pending" and resource not in _ASYNC_READ_RESOURCE:
        raise ValueError("canary paid pending state is invalid for this operation")
    return resource, sequence, dispatch_id


def _usage_dispatch_id(event: UsageEvent) -> str:
    """Return one validated private dispatch identity from authoritative usage."""
    dispatch_id = event.metadata.get(_DISPATCH_ID_METADATA_KEY)
    if not isinstance(dispatch_id, str) or not _is_sha256(dispatch_id):
        raise ValueError("canary paid usage dispatch identity is invalid")
    return dispatch_id


def _validate_private_replay(
    checkpoint: RunCheckpoint,
    usage_events: list[UsageEvent],
    *,
    run_id: str,
) -> None:
    """Fail closed on malformed private checkpoint or authoritative usage replay."""
    if checkpoint.status not in _PRIVATE_CHECKPOINT_STATUSES:
        raise ValueError("canary paid checkpoint status is invalid")
    if (
        checkpoint.pause_reason is not None
        or checkpoint.pending_company_id is not None
        or checkpoint.pending_stage is not None
    ):
        raise ValueError("canary paid checkpoint has unexpected pause or pending state")
    if not isinstance(checkpoint.provider_state, dict):
        raise ValueError("canary paid provider state must be an object")
    raw_operations = checkpoint.provider_state.get("operations")
    if not isinstance(raw_operations, dict):
        raise ValueError("canary paid operations must be an object")

    validated: list[tuple[str, dict[str, Any], str, int, str]] = []
    expected_usage: dict[str, _ResourceQuota] = {}
    unresolved = 0
    for operation_id, value in raw_operations.items():
        resource, sequence, dispatch_id = _validate_private_operation(
            run_id,
            operation_id,
            value,
        )
        if not isinstance(operation_id, str) or not isinstance(value, dict):
            raise AssertionError("validated operation shape changed unexpectedly")
        validated.append((operation_id, value, resource, sequence, dispatch_id))
        if value["state"] in {"in_flight", "pending"}:
            unresolved += 1
        for dispatch_sequence in range(sequence + 1):
            dispatch_resource = (
                resource
                if dispatch_sequence == 0
                else _ASYNC_READ_RESOURCE.get(resource)
            )
            if dispatch_resource is None:
                raise ValueError("canary paid dispatch history is invalid")
            historical_id = _dispatch_identity(
                run_id,
                operation_id,
                dispatch_resource,
                dispatch_sequence,
            )
            if historical_id in expected_usage:
                raise ValueError("canary paid dispatch identity collision")
            expected_usage[historical_id] = _RESOURCE_QUOTAS[dispatch_resource]

    if unresolved > 1:
        raise ValueError("canary paid replay contains concurrent unresolved work")
    if checkpoint.status == "completed" and unresolved:
        raise ValueError("canary paid completed checkpoint has unresolved work")

    usage_counts: dict[str, int] = {}
    for event in usage_events:
        dispatch_id = _usage_dispatch_id(event)
        quota = expected_usage.get(dispatch_id)
        if quota is None:
            raise ValueError("canary paid usage has no matching operation dispatch")
        if event.provider != quota.provider or event.operation != quota.operation:
            raise ValueError("canary paid usage does not match its dispatch identity")
        count = usage_counts.get(dispatch_id, 0) + 1
        if count > 1:
            raise ValueError("canary paid usage contains a duplicate dispatch identity")
        usage_counts[dispatch_id] = count

    for operation_id, value, resource, sequence, dispatch_id in validated:
        for dispatch_sequence in range(sequence):
            dispatch_resource = (
                resource
                if dispatch_sequence == 0
                else _ASYNC_READ_RESOURCE.get(resource)
            )
            if dispatch_resource is None:
                raise ValueError("canary paid dispatch history is invalid")
            historical_id = _dispatch_identity(
                run_id,
                operation_id,
                dispatch_resource,
                dispatch_sequence,
            )
            if usage_counts.get(historical_id, 0) != 1:
                raise ValueError("canary paid prior dispatch is missing authoritative usage")
        current_count = usage_counts.get(dispatch_id, 0)
        if value["dispatch_usage_recorded"] is True and current_count != 1:
            raise ValueError("canary paid recorded dispatch is missing authoritative usage")
        if value["dispatch_usage_recorded"] is False and value["state"] != "in_flight":
            raise ValueError("canary paid unresolved usage flag has an invalid operation state")


@dataclass(slots=True)
class CanaryPaidOperations:
    """Own private coverage-only state while admitting against shared canary totals."""

    run_dir: Path
    run_id: str
    checkpoint: RunCheckpoint
    checkpoint_path: Path
    usage_path: Path

    @classmethod
    def open(cls, run_dir: Path, *, run_id: str) -> CanaryPaidOperations:
        """Open and validate the private canary operation domain without copying normal usage."""
        checkpoint_path = run_dir / "canary_paid_checkpoint.json"
        usage_path = run_dir / "canary_paid_usage_events.jsonl"
        payload = read_json(checkpoint_path)
        checkpoint = (
            RunCheckpoint(run_id=run_id, provider_state={"operations": {}})
            if payload is None
            else RunCheckpoint.from_dict(payload)
        )
        if checkpoint.run_id != run_id:
            raise ValueError("canary paid checkpoint run_id mismatch")
        private_usage = load_usage_events(usage_path)
        _validate_private_replay(checkpoint, private_usage, run_id=run_id)
        return cls(
            run_dir=run_dir,
            run_id=run_id,
            checkpoint=checkpoint,
            checkpoint_path=checkpoint_path,
            usage_path=usage_path,
        )

    def _combined_usage(self) -> list[UsageEvent]:
        """Replay normal M4 authority plus coverage-only authoritative usage."""
        return [
            *load_usage_events(self.run_dir / "contact_usage_events.jsonl"),
            *load_usage_events(self.usage_path),
        ]

    def _lifecycle(self) -> PaidOperationLifecycle:
        """Build the shared lifecycle with combined replay and private-only writes."""
        events = self._combined_usage()
        return PaidOperationLifecycle(
            checkpoint=self.checkpoint,
            tracker=CostTracker(events),
            usage_path=self.usage_path,
            persist_checkpoint=lambda: write_checkpoint(self.checkpoint_path, self.checkpoint),
            publish_usage=lambda: None,
            usage_events=events,
        )

    def _assert_normal_paid_work_resolved(self) -> None:
        """Require both normal paid-operation domains to be terminal before shadow dispatch."""
        for name in ("checkpoint.json", "contact_checkpoint.json"):
            payload = read_json(self.run_dir / name)
            if payload is None or _normal_checkpoint_blocks(payload, self.run_id):
                raise RuntimeError("normal paid work is unresolved")

    def _assert_private_dispatch_available(self, lifecycle: PaidOperationLifecycle) -> None:
        """Reject fresh work after completion or while any private outcome is unfinished."""
        if self.checkpoint.status == "completed":
            raise RuntimeError("canary paid work is already completed")
        if lifecycle.unknown_in_flight() is not None:
            raise RuntimeError("canary paid work has an unresolved outcome")
        if any(entry.get("state") == "pending" for entry in lifecycle.operations().values()):
            raise RuntimeError("canary paid work has pending provider work")

    def _operation_for_input(
        self,
        lifecycle: PaidOperationLifecycle,
        operation_id: str,
        input_value: object,
    ) -> dict[str, Any]:
        """Return one private operation only when its persisted input binding is intact."""
        operations = lifecycle.operations()
        entry = operations.get(operation_id)
        if entry is None:
            raise ValueError("canary paid operation intent is missing")
        _validate_private_operation(self.run_id, operation_id, entry)
        stored = entry.get("input_fingerprint")
        if stored != _input_fingerprint(input_value):
            raise ValueError("canary paid operation input fingerprint mismatch")
        return entry

    def _resource_allows(
        self,
        lifecycle: PaidOperationLifecycle,
        resource: ResourceName,
    ) -> bool:
        """Apply fixed shared quota and provider-budget admission to one replay snapshot."""
        quota = _RESOURCE_QUOTAS[resource]
        if not lifecycle.quota_allows(
            quota.provider,
            quota.ceiling,
            quota.reservation,
            operation=quota.operation,
            unit=quota.unit,
        ):
            return False
        return lifecycle.budget_allows(
            quota.provider,
            quota.budget_ceiling,
            quota.budget_reservation,
        )

    def resource_allows(self, resource: ResourceName) -> bool:
        """Admit one coverage operation against normal plus prior private usage."""
        return self._resource_allows(self._lifecycle(), resource)

    def begin(
        self,
        operation_id: str,
        resource: ResourceName,
        *,
        input_value: object,
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist fingerprinted private intent only after shared safety admission."""
        self._assert_normal_paid_work_resolved()
        lifecycle = self._lifecycle()
        self._assert_private_dispatch_available(lifecycle)
        if not operation_id:
            raise ValueError("canary paid operation id must be nonblank")
        if operation_id in lifecycle.operations():
            raise RuntimeError("canary paid operation already exists")
        if not self._resource_allows(lifecycle, resource):
            raise RuntimeError("canary paid resource allowance is exhausted")
        quota = _RESOURCE_QUOTAS[resource]
        entry_fields = deepcopy(dict(fields or {}))
        if _RESERVED_OPERATION_FIELDS.intersection(entry_fields):
            raise ValueError("reserved canary paid operation field")
        entry_fields["resource"] = resource
        entry_fields["dispatch_resource"] = resource
        entry_fields["dispatch_sequence"] = 0
        entry_fields["dispatch_id"] = _dispatch_identity(
            self.run_id,
            operation_id,
            resource,
            0,
        )
        entry_fields["input_fingerprint"] = _input_fingerprint(input_value)
        entry_fields["dispatch_usage_recorded"] = False
        lifecycle.begin(
            operation_id,
            provider=quota.provider,
            operation=quota.operation,
            fields=entry_fields,
            reservation_usd=(
                quota.budget_reservation if quota.budget_ceiling is not None else None
            ),
        )

    def operation(
        self,
        operation_id: str,
        *,
        input_value: object,
    ) -> dict[str, Any] | None:
        """Reconstruct one persisted operation after validating its input binding."""
        lifecycle = self._lifecycle()
        if operation_id not in lifecycle.operations():
            return None
        return deepcopy(self._operation_for_input(lifecycle, operation_id, input_value))

    def reserve_async_read(
        self,
        operation_id: str,
        resource: ResourceName,
        *,
        input_value: object,
    ) -> None:
        """Persist one bounded status-read intent on the original async operation identity."""
        self._assert_normal_paid_work_resolved()
        lifecycle = self._lifecycle()
        entry = self._operation_for_input(lifecycle, operation_id, input_value)
        if entry.get("state") != "pending":
            raise RuntimeError("canary async read requires a pending operation")
        initial_resource = entry["resource"]
        if not isinstance(initial_resource, str) or _ASYNC_READ_RESOURCE.get(
            initial_resource
        ) != resource:
            raise ValueError("canary async read resource does not match operation")
        for other_id, other in lifecycle.operations().items():
            if other_id != operation_id and other.get("state") in {"in_flight", "pending"}:
                raise RuntimeError("canary paid work has an unresolved outcome")
        if not self._resource_allows(lifecycle, resource):
            raise RuntimeError("canary paid resource allowance is exhausted")
        sequence = entry["dispatch_sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValueError("canary paid dispatch sequence is invalid")
        next_sequence = sequence + 1
        lifecycle.finish(
            operation_id,
            state="in_flight",
            fields={
                "dispatch_resource": resource,
                "dispatch_sequence": next_sequence,
                "dispatch_id": _dispatch_identity(
                    self.run_id,
                    operation_id,
                    resource,
                    next_sequence,
                ),
                "dispatch_usage_recorded": False,
            },
        )

    def record_usage(
        self,
        operation_id: str,
        resource: ResourceName,
        *,
        input_value: object,
        event: UsageEvent,
    ) -> None:
        """Persist known provider usage idempotently before clearing the replay barrier."""
        lifecycle = self._lifecycle()
        entry = self._operation_for_input(lifecycle, operation_id, input_value)
        if entry.get("state") != "in_flight":
            raise RuntimeError("canary paid usage requires an in-flight operation")
        if entry["dispatch_usage_recorded"] is True:
            raise RuntimeError("canary paid usage is already recorded for this dispatch")
        initial_resource = entry["resource"]
        if not isinstance(initial_resource, str) or not _usage_resource_allowed(
            initial_resource, resource
        ):
            raise ValueError("canary paid usage resource does not match operation")
        if entry.get("dispatch_resource") != resource:
            raise ValueError("canary paid usage does not match active dispatch")
        quota = _RESOURCE_QUOTAS[resource]
        if entry.get("provider") != quota.provider:
            raise ValueError("canary paid usage provider does not match operation")
        if event.provider != quota.provider or event.operation != quota.operation:
            raise ValueError("canary paid usage event does not match resource")
        dispatch_id = entry.get("dispatch_id")
        if not isinstance(dispatch_id, str):
            raise ValueError("canary paid dispatch identity is invalid")

        matching_events = [
            stored
            for stored in load_usage_events(self.usage_path)
            if stored.metadata.get(_DISPATCH_ID_METADATA_KEY) == dispatch_id
        ]
        if len(matching_events) > 1:
            raise ValueError("canary paid usage contains a duplicate dispatch identity")
        if matching_events:
            stored = matching_events[0]
            if stored.provider != quota.provider or stored.operation != quota.operation:
                raise ValueError("canary paid usage does not match its dispatch identity")
        else:
            stored = UsageEvent.from_dict(event.to_dict())
            supplied_dispatch_id = stored.metadata.get(_DISPATCH_ID_METADATA_KEY)
            if supplied_dispatch_id is not None and supplied_dispatch_id != dispatch_id:
                raise ValueError("reserved canary paid usage dispatch identity")
            stored.metadata[_DISPATCH_ID_METADATA_KEY] = dispatch_id
            lifecycle.record_usage(stored)
        lifecycle.finish(
            operation_id,
            state="in_flight",
            fields={"dispatch_usage_recorded": True},
        )

    def finish(
        self,
        operation_id: str,
        *,
        input_value: object,
        state: OutcomeState = "completed",
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist a known result only after its authoritative usage is durable."""
        lifecycle = self._lifecycle()
        entry = self._operation_for_input(lifecycle, operation_id, input_value)
        if entry.get("state") != "in_flight":
            raise RuntimeError("canary paid finish requires an in-flight operation")
        if entry.get("dispatch_usage_recorded") is not True:
            raise RuntimeError("canary paid usage must be recorded before completion")
        result_fields = deepcopy(dict(fields or {}))
        if _RESERVED_OPERATION_FIELDS.intersection(result_fields):
            raise ValueError("reserved canary paid operation field")
        lifecycle.finish(operation_id, state=state, fields=result_fields)

    def complete(self) -> None:
        """Persist terminal private state only after every paid outcome is resolved."""
        lifecycle = self._lifecycle()
        if lifecycle.unknown_in_flight() is not None:
            raise RuntimeError("canary paid work has an unresolved outcome")
        if any(entry.get("state") == "pending" for entry in lifecycle.operations().values()):
            raise RuntimeError("canary paid work has pending provider work")
        transition_checkpoint(
            self.checkpoint,
            lambda: write_checkpoint(self.checkpoint_path, self.checkpoint),
            status="completed",
            reason=None,
        )


__all__ = ["CanaryPaidOperations", "OutcomeState", "ResourceName"]
