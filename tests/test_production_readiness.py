"""Focused production-readiness tests for paid recovery and bounded execution."""

# ruff: noqa: F401

from __future__ import annotations

import pytest

from leads_discovery.models import RunCheckpoint
from leads_discovery.pipeline.git_journal import sync_checkpoint_barrier
from private_journal_http import DraftReleaseJournalServer
from production_readiness_core import (
    test_atomic_write_rejects_symlink_target,
    test_canary_limits_are_not_cli_inputs,
    test_deepseek_malformed_2xx_is_terminal_without_replay,
    test_deepseek_schema_invalid_2xx_is_terminal,
    test_jsonl_snapshot_honors_record_and_run_bounds,
    test_m4_exa_people_budget_reserves_worst_case_before_dispatch,
    test_m4_rejects_symlinked_data_root,
    test_oversized_provider_response_is_rejected,
    test_oversized_replay_file_is_rejected_before_iteration,
    test_paid_workflow_is_manual_only_and_publishes_only_approved_outputs,
    test_replay_is_incremental_and_oversized_record_fails_at_that_record,
    test_replay_record_count_is_bounded,
    test_total_run_disk_limit_stops_before_second_write,
    test_unsupported_hard_negative_becomes_unknown,
)


def test_ambiguous_paid_operation_cannot_redispatch_after_remote_restart_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable private in-flight barrier blocks the same paid operation after local loss."""
    with DraftReleaseJournalServer() as journal:
        journal.configure(monkeypatch)
        checkpoint = RunCheckpoint(
            run_id="restart",
            provider_state={
                "operations": {
                    "discovery:one": {
                        "provider": "exa",
                        "operation": "company_search",
                        "state": "in_flight",
                    }
                }
            },
        )
        sync_checkpoint_barrier(checkpoint, None)
        with pytest.raises(RuntimeError, match="durable non-retryable"):
            sync_checkpoint_barrier(checkpoint, None)
