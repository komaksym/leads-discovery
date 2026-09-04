"""Cross-domain contracts for canary-private paid-operation state."""

from __future__ import annotations

from pathlib import Path

from leads_discovery.models import RunCheckpoint, UsageEvent
from leads_discovery.pipeline.canary_paid_operations import CanaryPaidOperations
from leads_discovery.pipeline.state import append_usage_event, write_checkpoint


def _completed_normal_checkpoints(run_dir: Path, run_id: str) -> None:
    """Persist resolved normal M2/M4 checkpoints for one canary run."""
    write_checkpoint(run_dir / "checkpoint.json", RunCheckpoint(run_id=run_id, status="completed"))
    write_checkpoint(
        run_dir / "contact_checkpoint.json",
        RunCheckpoint(run_id=run_id, status="completed"),
    )


def test_canary_private_quota_replays_normal_usage_without_copying_it(tmp_path: Path) -> None:
    """A normal Apollo credit consumes the one-credit canary total but stays in M4's ledger."""
    run_id = "shared-quota"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _completed_normal_checkpoints(run_dir, run_id)
    append_usage_event(
        run_dir / "contact_usage_events.jsonl",
        UsageEvent(
            provider="apollo",
            operation="people_enrichment",
            metadata={"credits_used": 1.0},
        ),
    )

    state = CanaryPaidOperations.open(run_dir, run_id=run_id)

    assert not state.resource_allows("apollo_enrichment")
    assert not state.usage_path.exists()
