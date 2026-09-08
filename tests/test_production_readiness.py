"""Focused production-readiness tests for paid recovery and bounded execution."""

# ruff: noqa: F401, F403, I001

from __future__ import annotations

import pytest

from leads_discovery.models import RunCheckpoint
from leads_discovery.pipeline.git_journal import sync_checkpoint_barrier
from private_journal_http import DraftReleaseJournalServer
from production_readiness_core import *  # noqa: F401,F403


def test_ambiguous_paid_operation_cannot_redispatch_after_remote_restart_barrier(  # type: ignore[no-redef]
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
