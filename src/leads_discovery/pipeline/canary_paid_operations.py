"""Canary-private paid-operation state layered on the shared lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

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


@dataclass(frozen=True, slots=True)
class _ResourceQuota:
    """Describe one immutable fixed-canary quota dimension."""

    provider: str
    operation: str
    ceiling: float
    reservation: float
    unit: Literal["credits", "requests"]


_RESOURCE_QUOTAS: Final[dict[ResourceName, _ResourceQuota]] = {
    "exa_people_search": _ResourceQuota("exa", "people_search", 1.0, 1.0, "requests"),
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

    def resource_allows(self, resource: ResourceName) -> bool:
        """Admit one coverage operation against normal plus prior private usage."""
        quota = _RESOURCE_QUOTAS[resource]
        return self._lifecycle().quota_allows(
            quota.provider,
            quota.ceiling,
            quota.reservation,
            operation=quota.operation,
            unit=quota.unit,
        )


__all__ = ["CanaryPaidOperations", "ResourceName"]
