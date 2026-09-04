"""Focused contracts for production-derived canary-only provider coverage."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from leads_discovery import production_canary
from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.providers import (
    ApolloResult,
    ClayResults,
    ClayStartResult,
    ExaPeopleResult,
    VerificationResult,
)
from leads_discovery.contacts.selection import select_contacts
from leads_discovery.models import CompanyRecord, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.state import append_jsonl, write_checkpoint, write_jsonl_atomic


def _coverage_module() -> ModuleType:
    """Load the feature module inside tests so the RED phase fails at test execution."""
    return importlib.import_module("leads_discovery.pipeline.canary_provider_coverage")


def _company(decision: str) -> CompanyRecord:
    """Build the exact one-company evaluated canary input."""
    company = CompanyRecord(
        company_id="cmp_acme",
        name="Acme Valve",
        normalized_name="acme valve",
        domain="acme.com",
        normalized_domain="acme.com",
        country="US",
    )
    company.final_decision = decision
    company.final_score = 9.0
    company.stage_status["decision"] = "completed"
    return company


def _person_result() -> dict[str, Any]:
    """Return one Exa row that the production selector accepts deterministically."""
    return {
        "id": "exa-person-1",
        "url": "https://www.linkedin.com/in/alice-owner",
        "entities": [
            {
                "type": "person",
                "id": "person-1",
                "properties": {
                    "name": "Alice Owner",
                    "workHistory": [
                        {
                            "title": "Owner",
                            "company": {"name": "Acme Valve"},
                            "dates": {"from": "2020-01", "to": None},
                        }
                    ],
                },
            }
        ],
    }


def _event(
    provider: str,
    operation: str,
    *,
    estimated_cost_usd: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> UsageEvent:
    """Build one authoritative known-usage event for a provider dispatch."""
    return UsageEvent(
        provider=provider,
        operation=operation,
        request_count=1,
        estimated_cost_usd=estimated_cost_usd,
        metadata=metadata or {},
    )


def _write_normal_state(
    run_dir: Path,
    company: CompanyRecord,
    *,
    contacts: tuple[ContactRecord, ...] = (),
    contact_operations: dict[str, Any] | None = None,
    contact_usage: tuple[UsageEvent, ...] = (),
) -> None:
    """Persist one fully resolved normal run/enrich state for coverage composition."""
    run_dir.mkdir()
    write_jsonl_atomic(run_dir / "companies_evaluated.jsonl", [company.to_dict()])
    write_jsonl_atomic(run_dir / "contacts.jsonl", [item.to_dict() for item in contacts])
    write_checkpoint(
        run_dir / "checkpoint.json",
        RunCheckpoint(run_id=run_dir.name, status="completed", provider_state={"operations": {}}),
    )
    write_checkpoint(
        run_dir / "contact_checkpoint.json",
        RunCheckpoint(
            run_id=run_dir.name,
            status="completed",
            provider_state={"operations": contact_operations or {}},
        ),
    )
    for event in contact_usage:
        append_jsonl(run_dir / "contact_usage_events.jsonl", event.to_dict())


class _CoverageExa:
    """Return one production-parseable person result and record the company input."""

    def __init__(self) -> None:
        self.companies: list[CompanyRecord] = []

    def search(self, company: CompanyRecord) -> ExaPeopleResult:
        self.companies.append(CompanyRecord.from_dict(company.to_dict()))
        return ExaPeopleResult(
            results=[_person_result()],
            usage_event=_event("exa", "people_search", estimated_cost_usd=0.001),
        )


class _CoverageClay:
    """Start once, then return a production-normalizable Clay work email."""

    def __init__(self) -> None:
        self.starts: list[list[ContactRecord]] = []
        self.result_ids: list[str] = []
        self.contact_id: str | None = None

    def start(self, contacts: list[ContactRecord]) -> ClayStartResult:
        copied = [ContactRecord.from_dict(item.to_dict()) for item in contacts]
        self.starts.append(copied)
        self.contact_id = copied[0].contact_id
        return ClayStartResult(
            routine_run_id="shadow-clay-run",
            usage_event=_event(
                "clay",
                "work_email_routine_start",
                metadata={"submitted_contacts": len(copied)},
            ),
        )

    def results(self, routine_run_id: str) -> ClayResults:
        self.result_ids.append(routine_run_id)
        assert self.contact_id is not None
        return ClayResults(
            status="complete",
            items=[{"id": self.contact_id, "work_email": " Alice.Owner@Acme.com "}],
            usage_event=_event("clay", "work_email_routine_results"),
        )


class _CoverageApollo:
    """Return a different work email so Clay precedence remains observable."""

    def __init__(self) -> None:
        self.contacts: list[ContactRecord] = []

    def enrich(self, contact: ContactRecord) -> ApolloResult:
        self.contacts.append(ContactRecord.from_dict(contact.to_dict()))
        return ApolloResult(
            work_email="apollo.owner@acme.com",
            credits_used=1.0,
            usage_event=_event(
                "apollo",
                "people_enrichment",
                metadata={"credits_used": 1.0},
            ),
        )


class _CoverageInstantly:
    """Record verification lineage and complete synchronously."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.read: list[str] = []

    def create(self, email: str) -> VerificationResult:
        self.created.append(email)
        return VerificationResult(
            status="verified",
            credits_used=1.0,
            usage_event=_event("instantly", "email_verification_create"),
        )

    def get(self, email: str) -> VerificationResult:
        self.read.append(email)
        raise AssertionError("completed verification must not be polled")


class _BombExa:
    def search(self, _company: CompanyRecord) -> ExaPeopleResult:
        raise AssertionError("Exa People must not be dispatched")


class _BombClay:
    def start(self, _contacts: list[ContactRecord]) -> ClayStartResult:
        raise AssertionError("Clay must not be dispatched")

    def results(self, _routine_run_id: str) -> ClayResults:
        raise AssertionError("Clay must not be polled")


class _BombApollo:
    def enrich(self, _contact: ContactRecord) -> ApolloResult:
        raise AssertionError("Apollo must not be dispatched")


class _BombInstantly:
    def create(self, _email: str) -> VerificationResult:
        raise AssertionError("Instantly must not be dispatched")

    def get(self, _email: str) -> VerificationResult:
        raise AssertionError("Instantly must not be polled")


def test_rejected_company_coverage_uses_production_lineage_without_mutating_contacts(
    tmp_path: Path,
) -> None:
    """Shadow work may prove providers but cannot rescue or write canonical M4 state."""
    module = _coverage_module()
    run = module.run_provider_coverage
    company = _company("rejected")
    run_dir = tmp_path / "canary-shadow"
    _write_normal_state(run_dir, company)
    canonical_before = (run_dir / "contacts.jsonl").read_text(encoding="utf-8")
    expected = select_contacts(company, [_person_result()], limit=1)[0]

    exa = _CoverageExa()
    clay = _CoverageClay()
    apollo = _CoverageApollo()
    instantly = _CoverageInstantly()

    first = run(
        run_dir,
        run_id=run_dir.name,
        exa=exa,
        clay=clay,
        apollo=apollo,
        instantly=instantly,
    )
    assert first.status == "pending"
    assert [item.company_id for item in exa.companies] == [company.company_id]
    assert len(clay.starts) == 1
    assert clay.starts[0][0].to_dict() == expected.to_dict()
    assert apollo.contacts == []
    assert instantly.created == []
    assert (run_dir / "contacts.jsonl").read_text(encoding="utf-8") == canonical_before

    second = run(
        run_dir,
        run_id=run_dir.name,
        exa=exa,
        clay=clay,
        apollo=apollo,
        instantly=instantly,
    )
    assert second.status == "completed"
    assert clay.result_ids == ["shadow-clay-run"]
    assert len(apollo.contacts) == 1
    assert apollo.contacts[0].to_dict() == expected.to_dict()
    assert instantly.created == ["alice.owner@acme.com"]
    assert (run_dir / "contacts.jsonl").read_text(encoding="utf-8") == canonical_before


def test_normal_clay_success_only_shadows_apollo_once_and_replay_is_zero_dispatch(
    tmp_path: Path,
) -> None:
    """Normal provider evidence suppresses duplicates while skipped Apollo gets one shared slot."""
    module = _coverage_module()
    run = module.run_provider_coverage
    company = _company("accepted")
    contact = select_contacts(company, [_person_result()], limit=1)[0]
    contact.work_email = "alice.owner@acme.com"
    contact.email_source = "clay"
    contact.email_verification_status = "verified"
    operations = {
        f"exa:{company.company_id}": {
            "state": "completed",
            "contact_ids": [contact.contact_id],
        },
        "clay:batch": {
            "state": "completed",
            "routine_run_id": "normal-clay-run",
            "contact_ids": [contact.contact_id],
        },
        f"instantly:{contact.contact_id}": {
            "state": "completed",
            "email": contact.work_email,
            "status": "verified",
        },
    }
    usage = (
        _event("exa", "people_search", estimated_cost_usd=0.001),
        _event(
            "clay",
            "work_email_routine_start",
            metadata={"submitted_contacts": 1},
        ),
        _event("clay", "work_email_routine_results"),
        _event("instantly", "email_verification_create"),
    )
    run_dir = tmp_path / "canary-normal"
    _write_normal_state(
        run_dir,
        company,
        contacts=(contact,),
        contact_operations=operations,
        contact_usage=usage,
    )
    canonical_before = (run_dir / "contacts.jsonl").read_text(encoding="utf-8")
    apollo = _CoverageApollo()

    first = run(
        run_dir,
        run_id=run_dir.name,
        exa=_BombExa(),
        clay=_BombClay(),
        apollo=apollo,
        instantly=_BombInstantly(),
    )
    assert first.status == "completed"
    assert len(apollo.contacts) == 1
    assert apollo.contacts[0].to_dict() == contact.to_dict()
    assert (run_dir / "contacts.jsonl").read_text(encoding="utf-8") == canonical_before

    second = run(
        run_dir,
        run_id=run_dir.name,
        exa=_BombExa(),
        clay=_BombClay(),
        apollo=apollo,
        instantly=_BombInstantly(),
    )
    assert second.status == "completed"
    assert len(apollo.contacts) == 1
    assert (run_dir / "contacts.jsonl").read_text(encoding="utf-8") == canonical_before


def test_normal_exa_zero_selection_is_not_repeated_and_downstream_gets_no_input(
    tmp_path: Path,
) -> None:
    """A valid normal zero-contact result is durable coverage, not a reason to search again."""
    module = _coverage_module()
    run = module.run_provider_coverage
    company = _company("accepted")
    run_dir = tmp_path / "canary-zero"
    _write_normal_state(
        run_dir,
        company,
        contact_operations={
            f"exa:{company.company_id}": {"state": "completed", "contact_ids": []}
        },
        contact_usage=(_event("exa", "people_search", estimated_cost_usd=0.001),),
    )

    summary = run(
        run_dir,
        run_id=run_dir.name,
        exa=_BombExa(),
        clay=_BombClay(),
        apollo=_BombApollo(),
        instantly=_BombInstantly(),
    )
    assert summary.status == "completed"
    assert (run_dir / "contacts.jsonl").read_text(encoding="utf-8") == ""


def test_production_canary_runs_coverage_only_after_normal_run_and_enrich(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Normal work precedes shadow coverage, and the report is derived last."""
    events: list[str] = []

    def fake_cli(argv: list[str] | None = None) -> int:
        assert argv is not None
        events.append(argv[0])
        return 0

    def fake_coverage(_data_root: Path, *, run_id: str) -> SimpleNamespace:
        assert run_id == "ordered"
        events.append("coverage")
        return SimpleNamespace(status="completed")

    def fake_report(_data_root: Path | str, *, run_id: str) -> SimpleNamespace:
        assert run_id == "ordered"
        events.append("report")
        return SimpleNamespace(overall_outcome="success")

    monkeypatch.setattr(production_canary, "cli_main", fake_cli)
    monkeypatch.setattr(production_canary, "run_live_provider_coverage", fake_coverage)
    monkeypatch.setattr(production_canary, "build_canary_coverage_report", fake_report)

    assert production_canary.main(
        ["--run-id", "ordered", "--data-root", str(tmp_path)]
    ) == 0
    assert events == ["run", "enrich", "coverage", "report"]
