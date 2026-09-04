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
from leads_discovery.pipeline.paid_operations import PaidOperationLifecycle
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
_RESERVED_OPERATION_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "state",
        "provider",
        "operation",
        "resource",
        "dispatch_resource",
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


def _normal_checkpoint_blocks(payload: dict[str, Any], run_id: str) -> bool:
    """Fail closed when one normal checkpoint contains unfinished paid work."""
    checkpoint = RunCheckpoint.from_dict(payload)
    if checkpoint.run_id != run_id:
        raise ValueError("normal checkpoint run_id mismatch")
    if checkpoint.status in {"paused_unknown", "paused_pending"}:
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
        """Open the private canary operation domain without copying normal usage."""
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
        """Require both normal paid-operation domains to be safe before shadow dispatch."""
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
        stored = entry.get("input_fingerprint")
        if not isinstance(stored, str) or stored != _input_fingerprint(input_value):
            raise ValueError("canary paid operation input fingerprint mismatch")
        resource = entry.get("resource")
        if not isinstance(resource, str) or resource not in _RESOURCE_QUOTAS:
            raise ValueError("canary paid operation resource is invalid")
        quota = _RESOURCE_QUOTAS[resource]
        if entry.get("provider") != quota.provider or entry.get("operation") != quota.operation:
            raise ValueError("canary paid operation identity is invalid")
        dispatch_resource = entry.get("dispatch_resource")
        if not isinstance(dispatch_resource, str) or not _usage_resource_allowed(
            resource, dispatch_resource
        ):
            raise ValueError("canary paid dispatch resource is invalid")
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
        lifecycle.finish(
            operation_id,
            state="in_flight",
            fields={
                "dispatch_resource": resource,
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
        """Persist known provider usage before allowing the operation to leave in-flight."""
        lifecycle = self._lifecycle()
        entry = self._operation_for_input(lifecycle, operation_id, input_value)
        if entry.get("state") != "in_flight":
            raise RuntimeError("canary paid usage requires an in-flight operation")
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
        lifecycle.record_usage(event)
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


__all__ = ["CanaryPaidOperations", "OutcomeState", "ResourceName"]
