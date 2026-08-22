from __future__ import annotations

from leads_discovery.models import UsageEvent
from leads_discovery.pipeline.costs import CostTracker


def test_cost_tracker_aggregates_usage_by_provider_and_total() -> None:
    """Usage totals must preserve provider-level and run-level accounting."""
    tracker = CostTracker()
    tracker.record(
        UsageEvent(
            provider="exa",
            operation="search",
            request_count=2,
            input_tokens=100,
            output_tokens=20,
            estimated_cost_usd=0.04,
        )
    )
    tracker.record(
        UsageEvent(
            provider="deepseek",
            operation="extract",
            request_count=1,
            input_tokens=500,
            output_tokens=100,
            estimated_cost_usd=0.01,
        )
    )

    summary = tracker.summary()

    assert summary["providers"]["exa"]["request_count"] == 2
    assert summary["providers"]["deepseek"]["input_tokens"] == 500
    assert summary["total"]["request_count"] == 3
    assert summary["total"]["input_tokens"] == 600
    assert summary["total"]["output_tokens"] == 120
    assert summary["total"]["estimated_cost_usd"] == 0.05


def test_cost_tracker_keeps_unreported_exact_cost_unknown() -> None:
    """Missing provider-reported exact cost must remain unknown rather than become zero."""
    tracker = CostTracker()
    tracker.record(UsageEvent(provider="exa", operation="search", estimated_cost_usd=0.02))

    summary = tracker.summary()

    assert summary["providers"]["exa"]["exact_cost_usd"] is None
    assert summary["total"]["exact_cost_usd"] is None
