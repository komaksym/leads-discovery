"""Highest-seam regressions for canary-private state across runner loss."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import test_canary_provider_coverage as coverage_helpers

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.providers import (
    ClayResults,
    ClayStartResult,
    VerificationResult,
)
from leads_discovery.contacts.selection import select_contacts
from leads_discovery.pipeline.canary_provider_coverage import run_provider_coverage

_JOURNAL_BRANCH = "canary-operation-journal"
_JOURNAL_KEY = "test-canary-journal-key-32-bytes-minimum"


def _git(cwd: Path, *args: str) -> None:
    """Run one local Git command for the runner-loss fixture."""
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _configure_identity(work: Path) -> None:
    """Configure commit-tree identity used by the production Git journal."""
    _git(work, "config", "user.name", "Test Bot")
    _git(work, "config", "user.email", "test@example.invalid")


def _initial_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    """Create the durable journal remote and the first ephemeral runner checkout."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    work = tmp_path / "runner-a"
    work.mkdir()
    _git(work, "init")
    _configure_identity(work)
    (work / "seed").write_text("journal\n", encoding="utf-8")
    _git(work, "add", "seed")
    _git(work, "commit", "-m", "seed")
    _git(work, "branch", "-M", _JOURNAL_BRANCH)
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-u", "origin", _JOURNAL_BRANCH)
    _configure_journal_env(monkeypatch, work)
    return remote, work


def _fresh_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote: Path,
    name: str,
) -> Path:
    """Clone a fresh checkout with no prior runner-local canary files."""
    work = tmp_path / name
    subprocess.run(
        ["git", "clone", "--branch", _JOURNAL_BRANCH, str(remote), str(work)],
        check=True,
        capture_output=True,
        text=True,
    )
    _configure_identity(work)
    _configure_journal_env(monkeypatch, work)
    return work


def _configure_journal_env(
    monkeypatch: pytest.MonkeyPatch,
    work: Path,
) -> None:
    """Point the production journal at the fixture's non-publication branch."""
    monkeypatch.setenv("GITHUB_WORKSPACE", str(work))
    monkeypatch.setenv("LEADS_GIT_JOURNAL_BRANCH", _JOURNAL_BRANCH)
    monkeypatch.setenv("LEADS_GIT_JOURNAL_REMOTE", "origin")
    monkeypatch.setenv("LEADS_GIT_JOURNAL_KEY", _JOURNAL_KEY)


def _normal_run_dir(work: Path, run_id: str) -> Path:
    """Recreate only the resolved normal state present before shadow composition."""
    data = work / "data"
    data.mkdir()
    run_dir = data / run_id
    coverage_helpers._write_normal_state(run_dir, coverage_helpers._company("rejected"))
    return run_dir


class _RestartableClay:
    """Make Clay polling independent of process-local state from the original start."""

    def __init__(self, contact_id: str) -> None:
        self.contact_id = contact_id
        self.starts: list[list[ContactRecord]] = []
        self.result_ids: list[str] = []

    def start(self, contacts: list[ContactRecord]) -> ClayStartResult:
        self.starts.append([ContactRecord.from_dict(item.to_dict()) for item in contacts])
        return ClayStartResult(
            routine_run_id="shadow-clay-run",
            usage_event=coverage_helpers._event(
                "clay",
                "work_email_routine_start",
                metadata={"submitted_contacts": len(contacts)},
            ),
        )

    def results(self, routine_run_id: str) -> ClayResults:
        self.result_ids.append(routine_run_id)
        return ClayResults(
            status="complete",
            items=[{"id": self.contact_id, "work_email": " Alice.Owner@Acme.com "}],
            usage_event=coverage_helpers._event("clay", "work_email_routine_results"),
        )


class _PendingInstantly:
    """Create one pending verification, then resolve it only through GET."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.read: list[str] = []

    def create(self, email: str) -> VerificationResult:
        self.created.append(email)
        return VerificationResult(
            status="pending",
            credits_used=1.0,
            usage_event=coverage_helpers._event("instantly", "email_verification_create"),
        )

    def get(self, email: str) -> VerificationResult:
        self.read.append(email)
        return VerificationResult(
            status="verified",
            credits_used=0.0,
            usage_event=coverage_helpers._event("instantly", "email_verification_get"),
        )


class _ResumeOnlyInstantly(_PendingInstantly):
    """Fail if a fresh runner replaces the already-created verification."""

    def create(self, email: str) -> VerificationResult:
        raise AssertionError(f"Instantly create must not be replaced for {email}")


def test_runner_loss_after_pending_shadow_clay_resumes_same_routine_without_new_paid_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh runner restores private Exa/Clay state and polls the original Clay run."""
    remote, first_work = _initial_workspace(tmp_path, monkeypatch)
    run_id = "restart-shadow-clay"
    first_run_dir = _normal_run_dir(first_work, run_id)
    company = coverage_helpers._company("rejected")
    contact = select_contacts(company, [coverage_helpers._person_result()], limit=1)[0]
    exa = coverage_helpers._CoverageExa()
    first_clay = _RestartableClay(contact.contact_id)

    first = run_provider_coverage(
        first_run_dir,
        run_id=run_id,
        exa=exa,
        clay=first_clay,
        apollo=coverage_helpers._BombApollo(),
        instantly=coverage_helpers._BombInstantly(),
    )
    assert first.status == "pending"
    assert len(exa.companies) == 1
    assert len(first_clay.starts) == 1

    second_work = _fresh_workspace(tmp_path, monkeypatch, remote, "runner-b")
    second_run_dir = _normal_run_dir(second_work, run_id)
    resumed_clay = _RestartableClay(contact.contact_id)
    instantly = coverage_helpers._CoverageInstantly()

    resumed = run_provider_coverage(
        second_run_dir,
        run_id=run_id,
        exa=coverage_helpers._BombExa(),
        clay=resumed_clay,
        apollo=coverage_helpers._BombApollo(),
        instantly=instantly,
    )

    assert resumed.status == "completed"
    assert resumed_clay.starts == []
    assert resumed_clay.result_ids == ["shadow-clay-run"]
    assert instantly.created == ["alice.owner@acme.com"]


def test_runner_loss_after_pending_shadow_instantly_resumes_get_without_new_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh runner restores the private email lineage and resumes the original verification."""
    remote, first_work = _initial_workspace(tmp_path, monkeypatch)
    run_id = "restart-shadow-instantly"
    first_run_dir = _normal_run_dir(first_work, run_id)
    company = coverage_helpers._company("rejected")
    contact = select_contacts(company, [coverage_helpers._person_result()], limit=1)[0]
    clay = _RestartableClay(contact.contact_id)
    instantly = _PendingInstantly()

    first = run_provider_coverage(
        first_run_dir,
        run_id=run_id,
        exa=coverage_helpers._CoverageExa(),
        clay=clay,
        apollo=coverage_helpers._BombApollo(),
        instantly=instantly,
    )
    assert first.status == "pending"

    second = run_provider_coverage(
        first_run_dir,
        run_id=run_id,
        exa=coverage_helpers._BombExa(),
        clay=clay,
        apollo=coverage_helpers._BombApollo(),
        instantly=instantly,
    )
    assert second.status == "pending"
    assert len(clay.starts) == 1
    assert clay.result_ids == ["shadow-clay-run"]
    assert instantly.created == ["alice.owner@acme.com"]

    second_work = _fresh_workspace(tmp_path, monkeypatch, remote, "runner-b")
    second_run_dir = _normal_run_dir(second_work, run_id)
    resumed_instantly = _ResumeOnlyInstantly()

    resumed = run_provider_coverage(
        second_run_dir,
        run_id=run_id,
        exa=coverage_helpers._BombExa(),
        clay=coverage_helpers._BombClay(),
        apollo=coverage_helpers._BombApollo(),
        instantly=resumed_instantly,
    )

    assert resumed.status == "completed"
    assert resumed_instantly.read == ["alice.owner@acme.com"]
