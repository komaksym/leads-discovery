"""Cross-domain contracts for canary-private paid-operation state."""

from __future__ import annotations

from pathlib import Path

import pytest

from leads_discovery.models import RunCheckpoint, UsageEvent
from leads_discovery.pipeline.canary_paid_operations import CanaryPaidOperations
from leads_discovery.pipeline.state import append_usage_event, read_json, write_checkpoint


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


def test_unresolved_normal_paid_work_blocks_private_dispatch(tmp_path: Path) -> None:
    """Coverage cannot stack new potentially billed work on unresolved normal work."""
    run_id = "normal-unknown"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    write_checkpoint(
        run_dir / "checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            status="paused_unknown",
            provider_state={"operations": {"deepseek:1": {"state": "in_flight"}}},
        ),
    )
    write_checkpoint(
        run_dir / "contact_checkpoint.json",
        RunCheckpoint(run_id=run_id, status="completed"),
    )
    state = CanaryPaidOperations.open(run_dir, run_id=run_id)

    with pytest.raises(RuntimeError, match="normal paid work"):
        state.begin(
            "coverage:apollo",
            "apollo_enrichment",
            input_value={"contact_id": "contact-1"},
        )

    assert not state.checkpoint_path.exists()


def test_private_begin_persists_fingerprinted_intent_before_dispatch(tmp_path: Path) -> None:
    """Private intent is durable and bound to production-derived input without storing it raw."""
    run_id = "private-intent"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _completed_normal_checkpoints(run_dir, run_id)
    state = CanaryPaidOperations.open(run_dir, run_id=run_id)

    state.begin(
        "coverage:exa-people",
        "exa_people_search",
        input_value={"company_id": "company-sensitive", "updated_at": "volatile"},
    )

    payload = read_json(state.checkpoint_path)
    assert payload is not None
    operation = payload["provider_state"]["operations"]["coverage:exa-people"]
    assert operation["state"] == "in_flight"
    assert operation["provider"] == "exa"
    assert operation["operation"] == "people_search"
    assert len(operation["input_fingerprint"]) == 64
    assert "company-sensitive" not in state.checkpoint_path.read_text(encoding="utf-8")


def test_private_usage_precedes_completion_and_replays_on_reopen(tmp_path: Path) -> None:
    """Known coverage usage is authoritative before a completed result becomes replayable."""
    run_id = "private-complete"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _completed_normal_checkpoints(run_dir, run_id)
    input_value = {"contact_id": "contact-1"}
    operation_id = "coverage:apollo"
    state = CanaryPaidOperations.open(run_dir, run_id=run_id)
    state.begin(operation_id, "apollo_enrichment", input_value=input_value)

    with pytest.raises(RuntimeError, match="usage"):
        state.finish(operation_id, input_value=input_value)

    state.record_usage(
        operation_id,
        "apollo_enrichment",
        input_value=input_value,
        event=UsageEvent(
            provider="apollo",
            operation="people_enrichment",
            metadata={"credits_used": 1.0},
        ),
    )
    state.finish(operation_id, input_value=input_value)

    reopened = CanaryPaidOperations.open(run_dir, run_id=run_id)
    operation = reopened.operation(operation_id, input_value=input_value)
    assert operation is not None
    assert operation["state"] == "completed"
    assert not reopened.resource_allows("apollo_enrichment")
    assert len(reopened.usage_path.read_text(encoding="utf-8").splitlines()) == 1


def test_private_operation_input_fingerprint_mismatch_fails_closed(tmp_path: Path) -> None:
    """A persisted operation cannot be replayed against semantically different input."""
    run_id = "fingerprint-mismatch"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _completed_normal_checkpoints(run_dir, run_id)
    state = CanaryPaidOperations.open(run_dir, run_id=run_id)
    state.begin(
        "coverage:clay",
        "clay_start",
        input_value={"contact_ids": ["contact-1"], "updated_at": "first"},
    )

    with pytest.raises(ValueError, match="fingerprint"):
        state.operation(
            "coverage:clay",
            input_value={"contact_ids": ["contact-2"], "updated_at": "second"},
        )
