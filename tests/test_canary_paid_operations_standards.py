"""Standards regressions for canary-private paid-operation safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from leads_discovery.models import RunCheckpoint, UsageEvent
from leads_discovery.pipeline import canary_paid_operations as canary_paid_module
from leads_discovery.pipeline.canary_paid_operations import CanaryPaidOperations
from leads_discovery.pipeline.state import read_json, write_checkpoint


def _completed_normal_checkpoints(run_dir: Path, run_id: str) -> None:
    """Persist terminal normal M2/M4 checkpoints for one canary run."""
    write_checkpoint(
        run_dir / "checkpoint.json",
        RunCheckpoint(run_id=run_id, status="completed"),
    )
    write_checkpoint(
        run_dir / "contact_checkpoint.json",
        RunCheckpoint(run_id=run_id, status="completed"),
    )


def test_running_normal_domain_blocks_private_dispatch_without_current_operation(
    tmp_path: Path,
) -> None:
    """Coverage cannot interleave with a normal domain that can still dispatch paid work."""
    run_id = "normal-running"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    write_checkpoint(
        run_dir / "checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            status="running",
            provider_state={"operations": {}},
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


def test_open_rejects_unknown_private_checkpoint_status(tmp_path: Path) -> None:
    """Private replay must fail closed on a top-level state the domain never writes."""
    run_id = "private-status"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    write_checkpoint(
        run_dir / "canary_paid_checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            status="mystery",
            provider_state={"operations": {}},
        ),
    )

    with pytest.raises(ValueError, match="status"):
        CanaryPaidOperations.open(run_dir, run_id=run_id)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_fingerprint",
        "invalid_dispatch_resource",
        "completed_with_in_flight",
        "resolved_without_usage",
    ],
)
def test_open_rejects_malformed_private_operation_replay(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Every persisted private operation must satisfy the domain's durable invariants."""
    run_id = f"private-malformed-{mutation}"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _completed_normal_checkpoints(run_dir, run_id)
    state = CanaryPaidOperations.open(run_dir, run_id=run_id)
    state.begin(
        "coverage:apollo",
        "apollo_enrichment",
        input_value={"contact_id": "contact-1"},
    )

    payload = read_json(state.checkpoint_path)
    assert payload is not None
    operation = payload["provider_state"]["operations"]["coverage:apollo"]
    if mutation == "missing_fingerprint":
        operation.pop("input_fingerprint")
    elif mutation == "invalid_dispatch_resource":
        operation["dispatch_resource"] = "clay_start"
    elif mutation == "completed_with_in_flight":
        payload["status"] = "completed"
    elif mutation == "resolved_without_usage":
        operation["state"] = "completed"
        operation["dispatch_usage_recorded"] = False
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(mutation)
    write_checkpoint(state.checkpoint_path, RunCheckpoint.from_dict(payload))

    with pytest.raises(ValueError, match="canary paid"):
        CanaryPaidOperations.open(run_dir, run_id=run_id)


def test_record_usage_retry_after_append_crash_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An append→crash→retry records one logical provider usage event, not two."""
    run_id = "usage-crash-idempotency"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _completed_normal_checkpoints(run_dir, run_id)
    operation_id = "coverage:apollo"
    input_value = {"contact_id": "contact-1"}
    event = UsageEvent(
        provider="apollo",
        operation="people_enrichment",
        metadata={"credits_used": 1.0},
    )
    state = CanaryPaidOperations.open(run_dir, run_id=run_id)
    state.begin(operation_id, "apollo_enrichment", input_value=input_value)

    def crash_checkpoint_write(path: Path, checkpoint: RunCheckpoint) -> None:
        del path, checkpoint
        raise OSError("simulated crash after authoritative usage append")

    with monkeypatch.context() as crash:
        crash.setattr(canary_paid_module, "write_checkpoint", crash_checkpoint_write)
        with pytest.raises(OSError, match="simulated crash"):
            state.record_usage(
                operation_id,
                "apollo_enrichment",
                input_value=input_value,
                event=event,
            )

    checkpoint_after_crash = read_json(state.checkpoint_path)
    assert checkpoint_after_crash is not None
    assert (
        checkpoint_after_crash["provider_state"]["operations"][operation_id][
            "dispatch_usage_recorded"
        ]
        is False
    )
    assert len(state.usage_path.read_text(encoding="utf-8").splitlines()) == 1

    reopened = CanaryPaidOperations.open(run_dir, run_id=run_id)
    reopened.record_usage(
        operation_id,
        "apollo_enrichment",
        input_value=input_value,
        event=event,
    )

    assert len(reopened.usage_path.read_text(encoding="utf-8").splitlines()) == 1
    operation = reopened.operation(operation_id, input_value=input_value)
    assert operation is not None
    assert operation["dispatch_usage_recorded"] is True
