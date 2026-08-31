"""Focused adversarial edges for the final production-readiness review."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from leads_discovery.models import RunCheckpoint, UsageEvent
from leads_discovery.pipeline.costs import CostTracker
from leads_discovery.pipeline.m2_batch import M2BatchConfig, run_m2_batch
from leads_discovery.pipeline.paid_operations import (
    PaidOperationLifecycle,
    checkpoint_has_unknown_paid_work,
)


def _lifecycle(events: list[UsageEvent]) -> PaidOperationLifecycle:
    """Build an in-memory lifecycle for testing its public admission contract."""
    return PaidOperationLifecycle(
        checkpoint=RunCheckpoint(run_id="red-team-budget"),
        tracker=CostTracker(events),
        usage_path=Path("unused-usage.jsonl"),
        persist_checkpoint=lambda: None,
        publish_usage=lambda: None,
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
    lifecycle = _lifecycle(
        [
            UsageEvent(
                provider="exa",
                operation="search",
                estimated_cost_usd=spend,
            )
        ]
    )
    assert lifecycle.budget_allows("exa", 1.0, reservation) is expected


def test_unknown_cost_blocks_next_paid_dispatch() -> None:
    """Unknown committed spend must never be treated as zero at the dispatch gate."""
    assert not _lifecycle(
        [
            UsageEvent(
                provider="exa",
                operation="search",
                estimated_cost_usd=None,
            )
        ]
    ).budget_allows("exa", 1.0, 0.01)


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
    assert checkpoint_has_unknown_paid_work(checkpoint)


@pytest.mark.parametrize("run_id", ["../escape", "nested/run", "..", "."])
def test_run_id_cannot_escape_data_root(tmp_path: Path, run_id: str) -> None:
    """Traversal-shaped run identifiers must fail before any run path is accepted."""
    with pytest.raises(ValueError, match="run_id"):
        run_m2_batch(
            M2BatchConfig(run_id=run_id, data_root=tmp_path),
            discovery={},
            researcher=cast(Any, object()),
            extractor=cast(Any, object()),
        )
