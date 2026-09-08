"""Direct cross-domain invariants for the private durable canary journal."""

from __future__ import annotations

from pathlib import Path

import pytest
from private_journal_http import DraftReleaseJournalServer

from leads_discovery.models import RunCheckpoint
from leads_discovery.pipeline.git_journal import (
    load_canary_private_state,
    persist_canary_private_state,
    sync_checkpoint_barrier,
)


def _private_checkpoint(run_id: str, *, state: str = "pending") -> RunCheckpoint:
    return RunCheckpoint(
        run_id=run_id,
        provider_state={
            "operations": {
                "coverage:clay": {
                    "provider": "clay",
                    "operation": "work_email_routine_start",
                    "state": state,
                    "dispatch_id": "dispatch-one",
                    "input_fingerprint": "fingerprint-one",
                    "dispatch_usage_recorded": False,
                    "routine_run_id": "routine-one",
                }
            }
        },
    )


def test_remote_barrier_and_pending_identity_become_durable_in_one_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The remote write exposes the exact resume identity whenever the barrier is durable."""
    with DraftReleaseJournalServer() as journal:
        journal.configure(monkeypatch)
        checkpoint = _private_checkpoint("atomic-transition")

        sync_checkpoint_barrier(checkpoint, None)

        durable = load_canary_private_state("atomic-transition")
        assert durable is not None
        assert durable["checkpoint"] == checkpoint.to_dict()
        assert durable["usage_events"] == []
        assert journal.assets
        ciphertext = b"".join(data for _name, data in journal.assets.values())
        assert b"routine-one" not in ciphertext
        assert b"dispatch-one" not in ciphertext


def test_latest_authority_does_not_depend_on_release_asset_id_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay authority must use journal ordering, not undocumented GitHub asset-ID ordering."""
    with DraftReleaseJournalServer() as journal:
        journal.configure(monkeypatch)
        run_id = "explicit-order"
        first = {"checkpoint": {"marker": "first"}, "usage_events": []}
        second = {"checkpoint": {"marker": "second"}, "usage_events": []}

        persist_canary_private_state(run_id, first)
        persist_canary_private_state(run_id, second)

        asset_ids = sorted(journal._assets)
        assert len(asset_ids) == 2
        older = journal._assets[asset_ids[0]]
        newer = journal._assets[asset_ids[1]]
        journal._assets = {
            asset_ids[0]: newer,
            asset_ids[1]: older,
        }

        assert load_canary_private_state(run_id) == second


def test_repeated_new_inflight_operation_is_blocked_by_remote_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing local state cannot make the same paid operation look fresh."""
    with DraftReleaseJournalServer() as journal:
        journal.configure(monkeypatch)
        checkpoint = RunCheckpoint(
            run_id="remote-barrier",
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


def test_published_state_release_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A journal that crossed the private draft boundary is no longer trusted."""
    with DraftReleaseJournalServer() as journal:
        journal.configure(monkeypatch)
        run_id = "published-release"
        persist_canary_private_state(
            run_id,
            {
                "checkpoint": _private_checkpoint(run_id).to_dict(),
                "usage_events": [],
            },
        )
        assert journal.releases
        journal.releases[0]["draft"] = False

        with pytest.raises(RuntimeError, match="unpublished draft"):
            load_canary_private_state(run_id)


def test_private_journal_configuration_does_not_require_a_repository_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh ephemeral runners can restore state before any Git ref mutation."""
    with DraftReleaseJournalServer() as journal:
        journal.configure(monkeypatch)
        monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path / "not-a-checkout"))
        run_id = "no-git-checkout"
        persist_canary_private_state(
            run_id,
            {
                "checkpoint": _private_checkpoint(run_id).to_dict(),
                "usage_events": [],
            },
        )
        assert load_canary_private_state(run_id) is not None
