"""Behavioral tests for the shared paid-operation lifecycle boundary."""

from __future__ import annotations

from pathlib import Path

from leads_discovery.models import RunCheckpoint, UsageEvent
from leads_discovery.pipeline.costs import CostTracker
from leads_discovery.pipeline.paid_operations import PaidOperationLifecycle, reservation_fits


def _lifecycle(tmp_path: Path) -> tuple[PaidOperationLifecycle, list[int]]:
    """Build a lifecycle with observable durable callback invocations."""
    checkpoint = RunCheckpoint(run_id="paid")
    tracker = CostTracker()
    persisted: list[int] = []
    lifecycle = PaidOperationLifecycle(
        checkpoint=checkpoint,
        tracker=tracker,
        usage_path=tmp_path / "usage.jsonl",
        persist_checkpoint=lambda: persisted.append(1),
        publish_usage=lambda: None,
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



def test_lifecycle_admits_exact_budget_and_persists_intent(tmp_path: Path) -> None:
    """Exact-budget work is admitted and becomes durable before dispatch."""
    lifecycle, persisted = _lifecycle(tmp_path)

    admitted = lifecycle.admit(
        "exa:one",
        provider="exa",
        operation="company_search",
        ceiling=0.02,
        reservation_usd=0.02,
        budget_reason="exa_budget",
        usage_unknown_reason="exa_usage_unknown",
    )

    assert admitted is True
    assert lifecycle.operations()["exa:one"]["state"] == "in_flight"
    assert persisted


def test_lifecycle_rejects_known_over_budget_before_intent(tmp_path: Path) -> None:
    """Known above-budget work is rejected without creating dispatch intent."""
    lifecycle, _ = _lifecycle(tmp_path)
    lifecycle.record_usage(
        UsageEvent(provider="exa", operation="company_search", estimated_cost_usd=0.02)
    )

    admitted = lifecycle.admit(
        "exa:two",
        provider="exa",
        operation="company_search",
        ceiling=0.02,
        reservation_usd=0.001,
        budget_reason="exa_budget",
        usage_unknown_reason="exa_usage_unknown",
    )

    assert admitted is False
    assert "exa:two" not in lifecycle.operations()
    assert lifecycle.checkpoint.status == "paused_budget"


def test_lifecycle_freezes_when_prior_provider_spend_is_unknown(tmp_path: Path) -> None:
    """Incomplete authoritative usage cannot authorize another paid dispatch."""
    lifecycle, _ = _lifecycle(tmp_path)
    lifecycle.record_usage(UsageEvent(provider="exa", operation="company_search"))

    admitted = lifecycle.admit(
        "exa:two",
        provider="exa",
        operation="company_search",
        ceiling=1.0,
        reservation_usd=0.01,
        budget_reason="exa_budget",
        usage_unknown_reason="exa_usage_unknown",
    )

    assert admitted is False
    assert "exa:two" not in lifecycle.operations()
    assert lifecycle.checkpoint.status == "paused_unknown"
    assert lifecycle.checkpoint.pause_reason == "exa_usage_unknown"
