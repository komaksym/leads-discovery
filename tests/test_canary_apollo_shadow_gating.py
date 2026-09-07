"""Regression contracts for Apollo canary shadow-dispatch eligibility."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import pytest
import test_canary_outcomes as outcome_helpers
import test_canary_provider_coverage as coverage_helpers

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.providers import ClayResults
from leads_discovery.contacts.selection import select_contacts
from leads_discovery.models import CompanyRecord, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.canary_outcomes import build_canary_coverage_report
from leads_discovery.pipeline.canary_paid_operations import CanaryPaidOperations
from leads_discovery.pipeline.canary_provider_coverage import run_provider_coverage
from leads_discovery.pipeline.state import (
    read_json,
    write_json_atomic,
    write_jsonl_atomic,
    write_text_atomic,
)


class _CoverageClayNoEmail(coverage_helpers._CoverageClay):
    """Complete coverage-only Clay with a valid parsed no-email outcome."""

    def results(self, routine_run_id: str) -> ClayResults:
        self.result_ids.append(routine_run_id)
        assert self.contact_id is not None
        return ClayResults(
            status="complete",
            items=[{"id": self.contact_id}],
            usage_event=coverage_helpers._event("clay", "work_email_routine_results"),
        )


def _normal_contact_with_email(
    decision: str,
    source: Literal["clay", "apollo"],
) -> tuple[CompanyRecord, ContactRecord]:
    """Build the selected canonical contact produced by normal M4."""
    company = coverage_helpers._company(decision)
    contact = select_contacts(company, [coverage_helpers._person_result()], limit=1)[0]
    contact.work_email = f"{source}.owner@acme.com"
    contact.email_source = source
    contact.email_verification_status = "verified"
    return company, contact


def _normal_operations_with_email(
    company: CompanyRecord,
    contact: ContactRecord,
    *,
    include_apollo: bool,
) -> dict[str, Any]:
    """Persist normal Exa/Clay/Instantly evidence and optional Apollo fallback evidence."""
    operations: dict[str, Any] = {
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
    if include_apollo:
        operations[f"apollo:{contact.contact_id}"] = {
            "state": "completed",
            "credits_used": 1.0,
        }
    return operations


def _normal_usage(*, include_apollo: bool) -> tuple[UsageEvent, ...]:
    """Build matching normal authoritative usage for one selected contact."""
    events = [
        coverage_helpers._event("exa", "people_search", estimated_cost_usd=0.001),
        coverage_helpers._event(
            "clay",
            "work_email_routine_start",
            metadata={"submitted_contacts": 1},
        ),
        coverage_helpers._event("clay", "work_email_routine_results"),
    ]
    if include_apollo:
        events.append(
            coverage_helpers._event(
                "apollo",
                "people_enrichment",
                metadata={"credits_used": 1.0},
            )
        )
    events.append(coverage_helpers._event("instantly", "email_verification_create"))
    return tuple(events)


@pytest.mark.parametrize("decision", ["rejected", "uncertain"])
def test_nonaccepted_coverage_keeps_exa_people_legal_but_apollo_zero_dispatch(
    tmp_path: Path,
    decision: str,
) -> None:
    """Coverage-only Exa/Clay may run, but they never manufacture Apollo eligibility."""
    company = coverage_helpers._company(decision)
    run_dir = tmp_path / f"coverage-{decision}"
    coverage_helpers._write_normal_state(run_dir, company)
    exa = coverage_helpers._CoverageExa()
    clay = coverage_helpers._CoverageClay()
    instantly = coverage_helpers._CoverageInstantly()

    first = run_provider_coverage(
        run_dir,
        run_id=run_dir.name,
        exa=exa,
        clay=clay,
        apollo=coverage_helpers._BombApollo(),
        instantly=instantly,
    )
    assert first.status == "pending"
    assert [item.company_id for item in exa.companies] == [company.company_id]
    assert len(clay.starts) == 1

    second = run_provider_coverage(
        run_dir,
        run_id=run_dir.name,
        exa=exa,
        clay=clay,
        apollo=coverage_helpers._BombApollo(),
        instantly=instantly,
    )
    assert second.status == "completed"
    assert clay.result_ids == ["shadow-clay-run"]
    assert instantly.created == ["alice.owner@acme.com"]

    checkpoint = read_json(run_dir / "canary_paid_checkpoint.json")
    assert checkpoint is not None
    operations = checkpoint["provider_state"]["operations"]
    assert "coverage:exa_people" in operations
    assert "coverage:clay" in operations
    assert "coverage:apollo" not in operations
    assert "coverage:instantly" in operations


def test_shadow_clay_no_email_does_not_unlock_apollo(tmp_path: Path) -> None:
    """A coverage-only Clay no-email result cannot become an Apollo fallback trigger."""
    company = coverage_helpers._company("rejected")
    run_dir = tmp_path / "shadow-clay-no-email"
    coverage_helpers._write_normal_state(run_dir, company)
    clay = _CoverageClayNoEmail()

    first = run_provider_coverage(
        run_dir,
        run_id=run_dir.name,
        exa=coverage_helpers._CoverageExa(),
        clay=clay,
        apollo=coverage_helpers._BombApollo(),
        instantly=coverage_helpers._BombInstantly(),
    )
    assert first.status == "pending"

    second = run_provider_coverage(
        run_dir,
        run_id=run_dir.name,
        exa=coverage_helpers._CoverageExa(),
        clay=clay,
        apollo=coverage_helpers._BombApollo(),
        instantly=coverage_helpers._BombInstantly(),
    )
    assert second.status == "completed"

    checkpoint = read_json(run_dir / "canary_paid_checkpoint.json")
    assert checkpoint is not None
    operations = checkpoint["provider_state"]["operations"]
    assert operations["coverage:clay"]["business_outcome"] == "no_email"
    assert "coverage:apollo" not in operations
    assert "coverage:instantly" not in operations


def test_normal_clay_usable_email_shadows_apollo_once_then_rerun_is_zero_dispatch(
    tmp_path: Path,
) -> None:
    """Normal Clay success is the one legal reason to spend the unused Apollo shadow slot."""
    company, contact = _normal_contact_with_email("accepted", "clay")
    run_dir = tmp_path / "normal-clay-email"
    coverage_helpers._write_normal_state(
        run_dir,
        company,
        contacts=(contact,),
        contact_operations=_normal_operations_with_email(
            company,
            contact,
            include_apollo=False,
        ),
        contact_usage=_normal_usage(include_apollo=False),
    )
    apollo = coverage_helpers._CoverageApollo()

    first = run_provider_coverage(
        run_dir,
        run_id=run_dir.name,
        exa=coverage_helpers._BombExa(),
        clay=coverage_helpers._BombClay(),
        apollo=apollo,
        instantly=coverage_helpers._BombInstantly(),
    )
    assert first.status == "completed"
    assert len(apollo.contacts) == 1
    assert apollo.contacts[0].contact_id == contact.contact_id

    second = run_provider_coverage(
        run_dir,
        run_id=run_dir.name,
        exa=coverage_helpers._BombExa(),
        clay=coverage_helpers._BombClay(),
        apollo=apollo,
        instantly=coverage_helpers._BombInstantly(),
    )
    assert second.status == "completed"
    assert len(apollo.contacts) == 1


def test_normal_apollo_fallback_consumes_shared_slot_and_suppresses_shadow(
    tmp_path: Path,
) -> None:
    """Normal Clay no-email -> normal Apollo fallback is reused, never duplicated privately."""
    company, contact = _normal_contact_with_email("accepted", "apollo")
    run_dir = tmp_path / "normal-apollo-fallback"
    coverage_helpers._write_normal_state(
        run_dir,
        company,
        contacts=(contact,),
        contact_operations=_normal_operations_with_email(
            company,
            contact,
            include_apollo=True,
        ),
        contact_usage=_normal_usage(include_apollo=True),
    )

    first = run_provider_coverage(
        run_dir,
        run_id=run_dir.name,
        exa=coverage_helpers._BombExa(),
        clay=coverage_helpers._BombClay(),
        apollo=coverage_helpers._BombApollo(),
        instantly=coverage_helpers._BombInstantly(),
    )
    assert first.status == "completed"

    checkpoint = read_json(run_dir / "canary_paid_checkpoint.json")
    assert checkpoint is not None
    assert "coverage:apollo" not in checkpoint["provider_state"]["operations"]

    second = run_provider_coverage(
        run_dir,
        run_id=run_dir.name,
        exa=coverage_helpers._BombExa(),
        clay=coverage_helpers._BombClay(),
        apollo=coverage_helpers._BombApollo(),
        instantly=coverage_helpers._BombInstantly(),
    )
    assert second.status == "completed"


def _write_coverage_only_clay_email(run_dir: Path, run_id: str, decision: str) -> None:
    """Persist legal nonaccepted Exa/Clay/Instantly coverage with no Apollo operation."""
    outcome_helpers._write_normal(run_dir, run_id)
    company = outcome_helpers._company()
    company.final_decision = decision
    write_jsonl_atomic(run_dir / "companies_evaluated.jsonl", [company.to_dict()])
    write_json_atomic(
        run_dir / "contact_checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            status="completed",
            provider_state={"operations": {}},
        ).to_dict(),
    )
    write_jsonl_atomic(run_dir / "contacts.jsonl", [])
    write_text_atomic(
        run_dir / "leads.csv",
        "company_id,contact_id,work_email,email_verification_status,email_source\n",
    )

    assert company.domain is not None
    contact = ContactRecord(
        contact_id="con_shadow",
        company_id=company.company_id,
        company_name=company.name,
        company_domain=company.domain,
        company_final_score=company.final_score,
        full_name="Alice Example",
        title="Owner",
        decision_rank=1,
        decision_reason="seniority",
        linkedin_url="https://linkedin.com/in/alice-example",
        profile_url="https://example.invalid/alice",
    )
    private = CanaryPaidOperations.open(run_dir, run_id=run_id)

    company_input = company.to_dict()
    private.begin("coverage:exa_people", "exa_people_search", input_value=company_input)
    private.record_usage(
        "coverage:exa_people",
        "exa_people_search",
        input_value=company_input,
        event=UsageEvent(
            provider="exa",
            operation="people_search",
            estimated_cost_usd=0.001,
        ),
    )
    private.finish(
        "coverage:exa_people",
        input_value=company_input,
        fields={
            "business_outcome": "contact_selected",
            "selected_contact": contact.to_dict(),
        },
    )

    contact_input = contact.to_dict()
    private.begin("coverage:clay", "clay_start", input_value=contact_input)
    private.record_usage(
        "coverage:clay",
        "clay_start",
        input_value=contact_input,
        event=UsageEvent(provider="clay", operation="work_email_routine_start"),
    )
    private.finish(
        "coverage:clay",
        input_value=contact_input,
        state="pending",
        fields={"routine_run_id": "shadow-clay", "business_outcome": "pending"},
    )
    private.reserve_async_read(
        "coverage:clay",
        "clay_status_read",
        input_value=contact_input,
    )
    private.record_usage(
        "coverage:clay",
        "clay_status_read",
        input_value=contact_input,
        event=UsageEvent(provider="clay", operation="work_email_routine_results"),
    )
    private.finish(
        "coverage:clay",
        input_value=contact_input,
        fields={
            "routine_run_id": "shadow-clay",
            "work_email": "alice.owner@acme.com",
            "business_outcome": "email",
        },
    )

    private.begin(
        "coverage:instantly",
        "instantly_create",
        input_value="alice.owner@acme.com",
    )
    private.record_usage(
        "coverage:instantly",
        "instantly_create",
        input_value="alice.owner@acme.com",
        event=UsageEvent(provider="instantly", operation="email_verification_create"),
    )
    private.finish(
        "coverage:instantly",
        input_value="alice.owner@acme.com",
        fields={
            "email": "alice.owner@acme.com",
            "verification_status": "verified",
            "business_outcome": "verified",
        },
    )
    private.complete()


@pytest.mark.parametrize("decision", ["rejected", "uncertain"])
def test_absent_apollo_is_inconclusive_without_normal_clay_prerequisite(
    tmp_path: Path,
    decision: str,
) -> None:
    """Coverage-only Clay success cannot turn missing Apollo into a readiness failure."""
    run_id = f"report-{decision}"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_coverage_only_clay_email(run_dir, run_id, decision)

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    apollo = outcome_helpers._provider(report, "apollo")
    instantly = outcome_helpers._provider(report, "instantly")
    assert (apollo.integration_outcome, apollo.business_outcome) == (
        "inconclusive",
        "not_exercised",
    )
    assert instantly.integration_outcome == "success"
    assert report.pipeline_outcome == "inconclusive"
    assert report.overall_outcome == "inconclusive"


def test_absent_apollo_is_failure_when_normal_clay_supplied_usable_email(
    tmp_path: Path,
) -> None:
    """A normal canonical Clay email legally requires successful Apollo shadow coverage."""
    run_id = "normal-clay-requires-apollo"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    outcome_helpers._write_normal(run_dir, run_id)
    outcome_helpers._write_contacts(run_dir, run_id, status="verified", instantly=True)

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    apollo = outcome_helpers._provider(report, "apollo")
    assert (apollo.integration_outcome, apollo.business_outcome) == (
        "failure",
        "not_exercised",
    )
    assert report.overall_outcome == "failure"
