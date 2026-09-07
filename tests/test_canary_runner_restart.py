"""Highest-seam regressions for canary-private state across runner loss."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import test_canary_provider_coverage as coverage_helpers

from leads_discovery import production_canary
from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.providers import (
    ClayResults,
    ClayStartResult,
    VerificationResult,
)
from leads_discovery.contacts.selection import select_contacts
from leads_discovery.pipeline.canary_provider_coverage import run_provider_coverage
from leads_discovery.pipeline.git_journal import persist_canary_private_state
from private_journal_http import DraftReleaseJournalServer

_LEADS_HEADER = "company_id,contact_id,work_email,email_verification_status,email_source\n"


def _normal_run_dir(root: Path, run_id: str) -> Path:
    """Persist the resolved normal state needed to enter shadow composition."""
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    run_dir = data / run_id
    coverage_helpers._write_normal_state(run_dir, coverage_helpers._company("rejected"))
    (run_dir / "leads.csv").write_text(_LEADS_HEADER, encoding="utf-8")
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


def test_production_canary_fresh_runner_restores_before_normal_cli_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Top-level canary restores completed normal authority before any normal redispatch."""
    with DraftReleaseJournalServer() as journal:
        journal.configure(monkeypatch)
        run_id = "restart-production-canary"
        first_root = tmp_path / "runner-a"
        first_run_dir = _normal_run_dir(first_root, run_id)
        company = coverage_helpers._company("rejected")
        contact = select_contacts(company, [coverage_helpers._person_result()], limit=1)[0]

        first = run_provider_coverage(
            first_run_dir,
            run_id=run_id,
            exa=coverage_helpers._CoverageExa(),
            clay=_RestartableClay(contact.contact_id),
            apollo=coverage_helpers._BombApollo(),
            instantly=coverage_helpers._BombInstantly(),
        )
        assert first.status == "pending"

        second_root = tmp_path / "runner-b"
        data_root = second_root / "data"

        def forbid_normal_cli(_argv: object = None) -> int:
            raise AssertionError(
                "normal run/enrich must not be re-dispatched from durable restart state"
            )

        def fake_coverage(root: Path, *, run_id: str) -> SimpleNamespace:
            restored = root / run_id
            assert (restored / "checkpoint.json").is_file()
            assert (restored / "contact_checkpoint.json").is_file()
            assert (restored / "companies_evaluated.jsonl").is_file()
            assert (restored / "contacts.jsonl").is_file()
            assert (restored / "leads.csv").is_file()
            return SimpleNamespace(status="completed")

        def fake_report(_root: Path, *, run_id: str) -> SimpleNamespace:
            assert run_id == "restart-production-canary"
            return SimpleNamespace(overall_outcome="inconclusive")

        monkeypatch.setattr(production_canary, "cli_main", forbid_normal_cli)
        monkeypatch.setattr(production_canary, "run_live_provider_coverage", fake_coverage)
        monkeypatch.setattr(production_canary, "build_canary_coverage_report", fake_report)

        assert (
            production_canary.main(
                ["--run-id", run_id, "--data-root", str(data_root)]
            )
            == 2
        )


def test_runner_loss_after_pending_shadow_clay_resumes_same_routine_without_new_paid_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh runner polls the original Clay identity and never starts replacement work."""
    with DraftReleaseJournalServer() as journal:
        journal.configure(monkeypatch)
        run_id = "restart-shadow-clay"
        first_run_dir = _normal_run_dir(tmp_path / "runner-a", run_id)
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
        assert journal.releases and all(release["draft"] is True for release in journal.releases)

        second_run_dir = _normal_run_dir(tmp_path / "runner-b", run_id)
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
    """A fresh runner resumes pending verification by GET and never repeats POST."""
    with DraftReleaseJournalServer() as journal:
        journal.configure(monkeypatch)
        run_id = "restart-shadow-instantly"
        first_run_dir = _normal_run_dir(tmp_path / "runner-a", run_id)
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

        second_run_dir = _normal_run_dir(tmp_path / "runner-b", run_id)
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


def test_private_state_without_completed_normal_restart_fails_before_normal_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incomplete durable authority fails closed rather than rebuilding paid normal work."""
    with DraftReleaseJournalServer() as journal:
        journal.configure(monkeypatch)
        run_id = "restart-missing-normal"
        persist_canary_private_state(
            run_id,
            {
                "checkpoint": {
                    "run_id": run_id,
                    "status": "running",
                    "provider_state": {"operations": {}},
                },
                "usage_events": [],
            },
        )

        def forbid_normal_cli(_argv: object = None) -> int:
            raise AssertionError("normal paid work must remain blocked")

        monkeypatch.setattr(production_canary, "cli_main", forbid_normal_cli)

        with pytest.raises(RuntimeError, match="durable normal restart prerequisites"):
            production_canary.main(
                ["--run-id", run_id, "--data-root", str(tmp_path / "data")]
            )
