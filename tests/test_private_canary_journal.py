"""Direct cross-domain invariants for the private durable canary journal."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from private_journal_http import DraftReleaseJournalServer

from leads_discovery.models import RunCheckpoint
from leads_discovery.pipeline import git_journal as git_journal_module
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


def test_renamed_private_authority_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renamed journal asset must not make durable paid authority disappear."""
    with DraftReleaseJournalServer() as journal:
        journal.configure(monkeypatch)
        run_id = "renamed-authority"
        persist_canary_private_state(
            run_id,
            {"checkpoint": {"marker": "durable"}, "usage_events": []},
        )
        asset_id = next(iter(journal._assets))
        release_id, _name, data = journal._assets[asset_id]
        journal._assets[asset_id] = (release_id, "renamed.bin", data)

        with pytest.raises(RuntimeError, match="asset name is invalid"):
            load_canary_private_state(run_id)


def test_deleted_latest_private_authority_cannot_roll_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting the latest asset must not make an older paid authority current again."""
    with DraftReleaseJournalServer() as journal:
        journal.configure(monkeypatch)
        run_id = "deleted-latest"
        persist_canary_private_state(
            run_id,
            {"checkpoint": {"marker": "first"}, "usage_events": []},
        )
        persist_canary_private_state(
            run_id,
            {"checkpoint": {"marker": "second"}, "usage_events": []},
        )
        latest_id = next(
            asset_id
            for asset_id, (_release_id, name, _data) in journal._assets.items()
            if "-002-" in name
        )
        del journal._assets[latest_id]

        with pytest.raises(RuntimeError, match="journal head"):
            load_canary_private_state(run_id)


def test_oversized_private_asset_is_rejected_before_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixed storage bound must be enforced from asset metadata before buffering bytes."""
    with DraftReleaseJournalServer() as journal:
        journal.configure(monkeypatch)
        run_id = "oversized-authority"
        persist_canary_private_state(
            run_id,
            {"checkpoint": {"marker": "durable"}, "usage_events": []},
        )
        asset_id = next(iter(journal._assets))
        release_id, name, _data = journal._assets[asset_id]
        journal._assets[asset_id] = (
            release_id,
            name,
            b"x" * (git_journal_module._MAX_STATE + 1025),
        )
        real_request = git_journal_module._request
        downloaded: list[str] = []

        def request_spy(
            config: Any,
            method: str,
            url: str,
            **kwargs: Any,
        ) -> Any:
            if kwargs.get("binary") is True:
                downloaded.append(url)
            return real_request(config, method, url, **kwargs)

        monkeypatch.setattr(git_journal_module, "_request", request_spy)

        with pytest.raises(RuntimeError, match="asset size exceeds"):
            load_canary_private_state(run_id)
        assert downloaded == []


def test_same_revision_race_cannot_let_both_writers_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent writers must not both return from a conflicting next-revision upload."""
    with DraftReleaseJournalServer() as journal:
        journal.configure(monkeypatch)
        run_id = "concurrent-revision"
        persist_canary_private_state(
            run_id,
            {"checkpoint": {"marker": "seed"}, "usage_events": []},
        )
        real_request = git_journal_module._request
        upload_barrier = threading.Barrier(2)

        def synchronized_request(
            config: Any,
            method: str,
            url: str,
            **kwargs: Any,
        ) -> Any:
            if method == "POST" and "/uploads/" in url:
                upload_barrier.wait(timeout=5)
            return real_request(config, method, url, **kwargs)

        monkeypatch.setattr(git_journal_module, "_request", synchronized_request)

        def write(marker: str) -> None:
            persist_canary_private_state(
                run_id,
                {"checkpoint": {"marker": marker}, "usage_events": []},
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(write, marker) for marker in ("left", "right")]
        errors = [future.exception() for future in futures]

        assert sum(error is None for error in errors) <= 1
        with pytest.raises(RuntimeError, match="duplicate revisions"):
            load_canary_private_state(run_id)


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
