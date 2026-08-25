"""Focused adversarial edges for the final production-readiness review."""

from __future__ import annotations

from pathlib import Path

import pytest

from leads_discovery.models import RunCheckpoint, UsageEvent
from leads_discovery.pipeline.costs import CostTracker
from leads_discovery.pipeline.m2_batch import (
    M2BatchConfig,
    _provider_budget_allows,
    _unknown_in_flight,
    _validate_config,
)


@pytest.mark.parametrize(
    ("spend", "reservation", "expected"),
    [
        (0.98, 0.01, True),
        (0.99, 0.01, True),
        (0.990001, 0.01, False),
    ],
)
def test_budget_gate_handles_below_exact_and_above_limit(
    spend: float,
    reservation: float,
    expected: bool,
) -> None:
    """Committed spend plus worst-case reservation must fit the hard ceiling."""
    tracker = CostTracker(
        [
            UsageEvent(
                provider="exa",
                operation="search",
                estimated_cost_usd=spend,
            )
        ]
    )
    assert _provider_budget_allows(tracker, "exa", 1.0, reservation) is expected


def test_unknown_cost_blocks_next_paid_dispatch() -> None:
    """Unknown committed spend must never be treated as zero at the dispatch gate."""
    tracker = CostTracker(
        [
            UsageEvent(
                provider="exa",
                operation="search",
                estimated_cost_usd=None,
            )
        ]
    )
    assert not _provider_budget_allows(tracker, "exa", 1.0, 0.01)


@pytest.mark.parametrize("provider", ["exa", "deepseek"])
def test_unresolved_paid_operation_is_global_replay_barrier(provider: str) -> None:
    """An unresolved synchronous paid operation must stop later paid stages globally."""
    checkpoint = RunCheckpoint(
        run_id="red-team-global-barrier",
        provider_state={
            "operations": {
                f"{provider}:lost": {
                    "provider": provider,
                    "operation": "paid_call",
                    "state": "in_flight",
                }
            }
        },
    )
    unresolved = _unknown_in_flight(checkpoint)
    assert unresolved is not None
    assert unresolved[1]["provider"] == provider


def test_apify_missing_run_id_is_barrier_but_persisted_id_is_resumable() -> None:
    """A lost Actor identity freezes replay while a persisted ID can be resumed safely."""
    checkpoint = RunCheckpoint(
        run_id="red-team-apify",
        provider_state={
            "operations": {
                "apify:maps": {
                    "provider": "apify",
                    "operation": "google_maps_search",
                    "state": "in_flight",
                }
            }
        },
    )
    assert _unknown_in_flight(checkpoint) is not None
    checkpoint.provider_state["operations"]["apify:maps"]["run_id"] = "run-123"
    assert _unknown_in_flight(checkpoint) is None


@pytest.mark.parametrize("run_id", ["../escape", "nested/run", "..", "."])
def test_run_id_cannot_escape_data_root(tmp_path: Path, run_id: str) -> None:
    """Traversal-shaped run identifiers must fail before any run path is accepted."""
    with pytest.raises(ValueError, match="run_id"):
        _validate_config(M2BatchConfig(run_id=run_id, data_root=tmp_path))
