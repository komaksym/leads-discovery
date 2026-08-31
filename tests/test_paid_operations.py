"""Behavioral tests for the shared paid-operation lifecycle boundary."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from leads_discovery.models import RunCheckpoint, UsageEvent
from leads_discovery.pipeline.costs import CostTracker
from leads_discovery.pipeline.paid_operations import PaidOperationLifecycle, reservation_fits


def _lifecycle(
    tmp_path: Path,
    events: Iterable[UsageEvent] = (),
) -> tuple[PaidOperationLifecycle, list[int]]:
    """Build a lifecycle with observable durable callback invocations."""
    checkpoint = RunCheckpoint(run_id="paid")
    replayed = list(events)
    tracker = CostTracker(replayed)
    persisted: list[int] = []
    lifecycle = PaidOperationLifecycle(
        checkpoint=checkpoint,
        tracker=tracker,
        usage_path=tmp_path / "usage.jsonl",
        persist_checkpoint=lambda: persisted.append(1),
        publish_usage=lambda: None,
        usage_events=replayed,
    )
    return lifecycle, persisted


def test_lifecycle_persists_intent_then_known_completion(tmp_path: Path) -> None:
    """A paid operation has a durable in-flight barrier before completion is recorded."""
    lifecycle, persisted = _lifecycle(tmp_path)

    lifecycle.begin(
        "apollo:contact-1",
        provider="apollo",
        operation="people_enrichment",
        fields={"credits_reserved": 1.0},
        company_id="company-1",
        pending_stage="people_enrichment",
    )
    assert lifecycle.operations()["apollo:contact-1"] == {
        "credits_reserved": 1.0,
        "provider": "apollo",
        "operation": "people_enrichment",
        "company_id": "company-1",
        "state": "in_flight",
    }
    assert lifecycle.unknown_in_flight() is not None

    lifecycle.finish("apollo:contact-1", fields={"credits_used": 1.0}, replace=True)
    assert lifecycle.operations()["apollo:contact-1"] == {
        "credits_used": 1.0,
        "state": "completed",
    }
    assert lifecycle.unknown_in_flight() is None
    assert len(persisted) == 2


def test_lifecycle_usage_is_authoritative_for_budget_replay(tmp_path: Path) -> None:
    """Recorded usage updates both the append-only ledger and the live budget tracker."""
    lifecycle, _ = _lifecycle(tmp_path)
    lifecycle.record_usage(
        UsageEvent(provider="apollo", operation="people_enrichment", estimated_cost_usd=1.0)
    )

    assert lifecycle.tracker.provider_estimated_spend("apollo") == 1.0
    assert len(lifecycle.usage_path.read_text(encoding="utf-8").splitlines()) == 1
    assert not reservation_fits(1.0, 1.0, 0.01)
    assert reservation_fits(0.99, 1.0, 0.01)


def test_lifecycle_replays_provider_quotas_and_excludes_instantly_gets(
    tmp_path: Path,
) -> None:
    """Quota admission counts Apollo credits and Instantly creates from the ledger only."""
    events = [
        UsageEvent(
            provider="apollo",
            operation="people_enrichment",
            metadata={"credits_used": 2.0},
        ),
        UsageEvent(
            provider="instantly",
            operation="email_verification_create",
            metadata={"credits_used": 1.0},
        ),
        UsageEvent(
            provider="instantly",
            operation="email_verification_get",
            metadata={"credits_used": 0.0},
        ),
    ]
    lifecycle, _ = _lifecycle(tmp_path, events)

    assert lifecycle.quota_used("apollo") == 2.0
    assert not lifecycle.quota_allows("apollo", 2.0, 1.0)
    assert lifecycle.quota_used(
        "instantly", operation="email_verification_create"
    ) == 1.0
    assert lifecycle.quota_allows(
        "instantly",
        2.0,
        1.0,
        operation="email_verification_create",
    )

    lifecycle.record_usage(
        UsageEvent(
            provider="instantly",
            operation="email_verification_get",
            metadata={"credits_used": 0.0},
        )
    )
    assert lifecycle.quota_used("instantly", operation="email_verification_create") == 1.0
