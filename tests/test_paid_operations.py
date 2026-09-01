"""Behavioral tests for the shared paid-operation lifecycle boundary."""

from __future__ import annotations

from pathlib import Path

from leads_discovery.models import RunCheckpoint, UsageEvent
from leads_discovery.pipeline.costs import CostTracker
from leads_discovery.pipeline.paid_operations import (
    PaidAdmissionPolicy,
    PaidOperationLifecycle,
    classify_paid_error,
    reservation_fits,
)


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
        "exa:search-1",
        provider="exa",
        operation="company_search",
        fields={"attempt": 1},
        request_id="search-1",
        pending_stage="discovery",
    )
    assert lifecycle.operations()["exa:search-1"] == {
        "attempt": 1,
        "provider": "exa",
        "operation": "company_search",
        "request_id": "search-1",
        "state": "in_flight",
    }
    assert lifecycle.unknown_in_flight() is not None

    lifecycle.finish("exa:search-1", fields={"provider_request_id": "remote-1"})
    assert lifecycle.operations()["exa:search-1"] == {
        "attempt": 1,
        "provider": "exa",
        "operation": "company_search",
        "request_id": "search-1",
        "provider_request_id": "remote-1",
        "state": "completed",
    }
    assert lifecycle.unknown_in_flight() is None
    assert len(persisted) == 2


def test_lifecycle_usage_is_authoritative_for_budget_replay(tmp_path: Path) -> None:
    """Recorded usage updates both the append-only ledger and the live budget tracker."""
    lifecycle, _ = _lifecycle(tmp_path)
    lifecycle.record_usage(
        UsageEvent(provider="exa", operation="company_search", estimated_cost_usd=1.0)
    )

    assert lifecycle.tracker.provider_estimated_spend("exa") == 1.0
    assert len(lifecycle.usage_path.read_text(encoding="utf-8").splitlines()) == 1
    assert not reservation_fits(1.0, 1.0, 0.01)
    assert reservation_fits(0.99, 1.0, 0.01)


def test_lifecycle_admits_exact_budget_and_persists_intent(tmp_path: Path) -> None:
    """Exact-budget work is admitted and becomes durable before dispatch."""
    lifecycle, persisted = _lifecycle(tmp_path)

    admitted = lifecycle.admit(
        "exa:one",
        operation="company_search",
        policy=PaidAdmissionPolicy(
            provider="exa",
            ceiling=0.02,
            budget_reason="exa_budget",
            usage_unknown_reason="exa_usage_unknown",
        ),
        reservation_usd=0.02,
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
        operation="company_search",
        policy=PaidAdmissionPolicy(
            provider="exa",
            ceiling=0.02,
            budget_reason="exa_budget",
            usage_unknown_reason="exa_usage_unknown",
        ),
        reservation_usd=0.001,
    )

    assert admitted is False
    assert "exa:two" not in lifecycle.operations()
    assert lifecycle.checkpoint.status == "paused_budget"


def test_lifecycle_freezes_unknown_spend_even_without_a_ceiling(tmp_path: Path) -> None:
    """Unknown committed spend is a safety barrier even when no hard ceiling is configured."""
    lifecycle, _ = _lifecycle(tmp_path)
    lifecycle.record_usage(UsageEvent(provider="exa", operation="company_search"))

    admitted = lifecycle.admit(
        "exa:two",
        operation="company_search",
        policy=PaidAdmissionPolicy(
            provider="exa",
            ceiling=None,
            budget_reason="exa_budget",
            usage_unknown_reason="exa_usage_unknown",
        ),
        reservation_usd=0.01,
    )

    assert admitted is False
    assert "exa:two" not in lifecycle.operations()
    assert lifecycle.checkpoint.status == "paused_unknown"
    assert lifecycle.checkpoint.pause_reason == "exa_usage_unknown"
    assert not reservation_fits(None, None, 0.01)


def test_continuation_freezes_unknown_spend_even_without_a_ceiling(tmp_path: Path) -> None:
    """A resumable operation cannot dispatch its next paid call behind unknown spend."""
    lifecycle, _ = _lifecycle(tmp_path)
    lifecycle.begin("exa:research", provider="exa", operation="company_research")
    lifecycle.record_usage(UsageEvent(provider="exa", operation="company_research"))

    admitted = lifecycle.reserve_continuation(
        "exa:research",
        policy=PaidAdmissionPolicy(
            provider="exa",
            ceiling=None,
            budget_reason="exa_budget",
            usage_unknown_reason="exa_usage_unknown",
        ),
        reservation_usd=0.01,
        stage="research",
    )

    assert admitted is False
    assert lifecycle.operations()["exa:research"]["state"] == "pending"
    assert lifecycle.checkpoint.status == "paused_unknown"
    assert lifecycle.checkpoint.pause_reason == "exa_usage_unknown"


def test_paid_error_classification_is_shared_across_m2_stages() -> None:
    """Required provider stages consume one paid-error taxonomy."""
    assert classify_paid_error("transient", False) == "unknown"
    assert classify_paid_error("budget_exhausted", False) == "budget"
    assert classify_paid_error("rate_limited", True) == "retryable"
    assert classify_paid_error("authentication", False) == "permanent"
