"""Cross-domain contracts for canary-private paid-operation state."""

from __future__ import annotations

from pathlib import Path

import pytest

from leads_discovery.models import RunCheckpoint, UsageEvent
from leads_discovery.pipeline.canary_paid_operations import CanaryPaidOperations, ResourceName
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
    usage_event = UsageEvent(
        provider="apollo",
        operation="people_enrichment",
        metadata={"credits_used": 1.0},
    )
    state = CanaryPaidOperations.open(run_dir, run_id=run_id)
    state.begin(operation_id, "apollo_enrichment", input_value=input_value)

    with pytest.raises(RuntimeError, match="usage"):
        state.finish(operation_id, input_value=input_value)

    state.record_usage(
        operation_id,
        "apollo_enrichment",
        input_value=input_value,
        event=usage_event,
    )
    with pytest.raises(RuntimeError, match="already"):
        state.record_usage(
            operation_id,
            "apollo_enrichment",
            input_value=input_value,
            event=usage_event,
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


@pytest.mark.parametrize(
    ("start_resource", "read_resource", "provider", "start_operation", "read_operation"),
    [
        (
            "clay_start",
            "clay_status_read",
            "clay",
            "work_email_routine_start",
            "work_email_routine_results",
        ),
        (
            "instantly_create",
            "instantly_status_read",
            "instantly",
            "email_verification_create",
            "email_verification_get",
        ),
    ],
)
def test_async_reads_reuse_operation_identity_and_stop_at_three(
    tmp_path: Path,
    start_resource: ResourceName,
    read_resource: ResourceName,
    provider: str,
    start_operation: str,
    read_operation: str,
) -> None:
    """Clay and Instantly resume the original operation with at most three paid reads."""
    run_id = f"async-{provider}"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _completed_normal_checkpoints(run_dir, run_id)
    operation_id = f"coverage:{provider}"
    input_value = {"contact_ids": ["contact-1"]}
    state = CanaryPaidOperations.open(run_dir, run_id=run_id)
    state.begin(operation_id, start_resource, input_value=input_value)
    state.record_usage(
        operation_id,
        start_resource,
        input_value=input_value,
        event=UsageEvent(provider=provider, operation=start_operation),
    )
    state.finish(
        operation_id,
        input_value=input_value,
        state="pending",
        fields={"provider_job_id": "job-1"},
    )

    for index in range(3):
        state.reserve_async_read(operation_id, read_resource, input_value=input_value)
        operation = state.operation(operation_id, input_value=input_value)
        assert operation is not None
        assert operation["state"] == "in_flight"
        assert operation["operation"] == start_operation
        assert set(state.checkpoint.provider_state["operations"]) == {operation_id}
        state.record_usage(
            operation_id,
            read_resource,
            input_value=input_value,
            event=UsageEvent(provider=provider, operation=read_operation),
        )
        state.finish(operation_id, input_value=input_value, state="pending")
        assert index + 1 == len(
            [
                line
                for line in state.usage_path.read_text(encoding="utf-8").splitlines()
                if read_operation in line
            ]
        )

    with pytest.raises(RuntimeError, match="allowance"):
        state.reserve_async_read(operation_id, read_resource, input_value=input_value)


def test_unresolved_private_async_read_freezes_future_paid_work(tmp_path: Path) -> None:
    """A crash after async-read intent leaves one unknown outcome and forbids redispatch."""
    run_id = "async-unknown"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _completed_normal_checkpoints(run_dir, run_id)
    operation_id = "coverage:clay"
    input_value = {"contact_ids": ["contact-1"]}
    state = CanaryPaidOperations.open(run_dir, run_id=run_id)
    state.begin(operation_id, "clay_start", input_value=input_value)
    state.record_usage(
        operation_id,
        "clay_start",
        input_value=input_value,
        event=UsageEvent(provider="clay", operation="work_email_routine_start"),
    )
    state.finish(operation_id, input_value=input_value, state="pending")
    state.reserve_async_read(operation_id, "clay_status_read", input_value=input_value)

    reopened = CanaryPaidOperations.open(run_dir, run_id=run_id)
    with pytest.raises(RuntimeError, match="pending"):
        reopened.reserve_async_read(
            operation_id,
            "clay_status_read",
            input_value=input_value,
        )
    with pytest.raises(RuntimeError, match="unresolved"):
        reopened.begin(
            "coverage:apollo",
            "apollo_enrichment",
            input_value={"contact_id": "contact-2"},
        )


def test_completed_private_state_replays_and_refuses_fresh_dispatch(tmp_path: Path) -> None:
    """A completed canary paid domain is replayable but can never spend again on rerun."""
    run_id = "completed-rerun"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _completed_normal_checkpoints(run_dir, run_id)
    operation_id = "coverage:apollo"
    input_value = {"contact_id": "contact-1"}
    state = CanaryPaidOperations.open(run_dir, run_id=run_id)
    state.begin(operation_id, "apollo_enrichment", input_value=input_value)
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
    state.complete()

    reopened = CanaryPaidOperations.open(run_dir, run_id=run_id)
    operation = reopened.operation(operation_id, input_value=input_value)
    assert reopened.checkpoint.status == "completed"
    assert operation is not None
    assert operation["state"] == "completed"
    with pytest.raises(RuntimeError, match="completed"):
        reopened.begin(
            "coverage:exa-people",
            "exa_people_search",
            input_value={"company_id": "company-1"},
        )
