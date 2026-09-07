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
    source: Literal["clay", "apollo"],
) -> tuple[CompanyRecord, ContactRecord]:
    """Build one production-selected canonical contact with provider email evidence."""
    company = coverage_helpers._company("accepted")
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
def test_nonaccepted_shadow_clay_email_never_unlocks_apollo(
    tmp_path: Path,
    decision: str,
) -> None:
    """Rejected/uncertain shadow Clay may feed Instantly but never authorizes Apollo."""
    company = coverage_helpers._company(decision)
    run_dir = tmp_path / f"coverage-{decision}"
    coverage_helpers._write_normal_state(run_dir, company)
    exa = coverage_helpers._CoverageExa()
    clay = coverage_helpers._CoverageClay()
    apollo = coverage_helpers._CoverageApollo()
    instantly = coverage_helpers._CoverageInstantly()

    first = run_provider_coverage(
        run_dir,
        run_id=run_dir.name,
        exa=exa,
        clay=clay,
        apollo=apollo,
        instantly=instantly,
    )
    assert first.status == "pending"

    second = run_provider_coverage(
        run_dir,
        run_id=run_dir.name,
        exa=exa,
        clay=clay,
        apollo=apollo,
        instantly=instantly,
    )
    assert second.status == "completed"
    assert [item.company_id for item in exa.companies] == [company.company_id]
    assert len(clay.starts) == 1
    assert clay.result_ids == ["shadow-clay-run"]
    assert apollo.contacts == []
    assert instantly.created == ["alice.owner@acme.com"]

    checkpoint = read_json(run_dir / "canary_paid_checkpoint.json")
    assert checkpoint is not None
    operations = checkpoint["provider_state"]["operations"]
    assert "coverage:exa_people" in operations
    assert "coverage:clay" in operations
    assert "coverage:apollo" not in operations
    assert "coverage:instantly" in operations


def test_shadow_clay_no_email_does_not_unlock_apollo(tmp_path: Path) -> None:
    """A completed Clay no-email result cannot authorize a new paid Apollo dispatch."""
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
    """Normal Clay success authorizes exactly one unused Apollo shadow slot."""
    company, contact = _normal_contact_with_email("clay")
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


def test_normal_apollo_evidence_is_reused_without_shadow_dispatch(tmp_path: Path) -> None:
    """Existing normal Apollo evidence consumes the shared slot even when Clay has no email."""
    company, contact = _normal_contact_with_email("apollo")
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


def _write_private_exa_and_clay(
    run_dir: Path,
    run_id: str,
    *,
    clay_email: str | None,
    include_legacy_apollo: bool = False,
) -> None:
    """Persist legal rejected-company coverage through one terminal Clay outcome."""
    outcome_helpers._write_normal(run_dir, run_id)
    company = outcome_helpers._company()
    company.final_decision = "rejected"
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
            "work_email": clay_email,
            "business_outcome": "email" if clay_email is not None else "no_email",
        },
    )

    if include_legacy_apollo:
        private.begin("coverage:apollo", "apollo_enrichment", input_value=contact_input)
        private.record_usage(
            "coverage:apollo",
            "apollo_enrichment",
            input_value=contact_input,
            event=UsageEvent(
                provider="apollo",
                operation="people_enrichment",
                metadata={"matched": False, "credits_used": 1.0},
            ),
        )
        private.finish(
            "coverage:apollo",
            input_value=contact_input,
            fields={
                "credits_used": 1.0,
                "work_email": None,
                "business_outcome": "no_email",
            },
        )
    private.complete()


def test_shadow_clay_no_email_without_normal_apollo_reports_inconclusive(
    tmp_path: Path,
) -> None:
    """A valid Clay no-email outcome is a missing Apollo prerequisite, not a failure."""
    run_id = "report-clay-no-email"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_private_exa_and_clay(run_dir, run_id, clay_email=None)

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    apollo = outcome_helpers._provider(report, "apollo")
    instantly = outcome_helpers._provider(report, "instantly")
    assert (apollo.integration_outcome, apollo.business_outcome) == (
        "inconclusive",
        "not_exercised",
    )
    assert instantly.integration_outcome == "inconclusive"
    assert report.pipeline_outcome == "inconclusive"
    assert report.overall_outcome == "inconclusive"


def test_shadow_clay_usable_email_does_not_make_apollo_required(tmp_path: Path) -> None:
    """Coverage-only Clay success never creates an Apollo coverage prerequisite."""
    run_id = "report-clay-email"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_private_exa_and_clay(run_dir, run_id, clay_email="alice.owner@acme.com")

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    apollo = outcome_helpers._provider(report, "apollo")
    instantly = outcome_helpers._provider(report, "instantly")
    assert (apollo.integration_outcome, apollo.business_outcome) == (
        "inconclusive",
        "not_exercised",
    )
    assert instantly.integration_outcome == "failure"
    assert report.overall_outcome == "failure"


def test_legacy_private_apollo_without_normal_clay_skip_is_not_reused(
    tmp_path: Path,
) -> None:
    """Replay fails closed on private Apollo created without the normal-Clay skip prerequisite."""
    run_id = "legacy-private-apollo-replay"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_private_exa_and_clay(
        run_dir,
        run_id,
        clay_email="alice.owner@acme.com",
        include_legacy_apollo=True,
    )

    with pytest.raises(ValueError, match="private Apollo coverage lacks normal Clay skip prerequisite"):
        run_provider_coverage(
            run_dir,
            run_id=run_id,
            exa=coverage_helpers._BombExa(),
            clay=coverage_helpers._BombClay(),
            apollo=coverage_helpers._BombApollo(),
            instantly=coverage_helpers._CoverageInstantly(),
        )


def test_legacy_private_apollo_without_normal_clay_skip_reports_failure(
    tmp_path: Path,
) -> None:
    """Legacy private Apollo evidence cannot make readiness green without its prerequisite."""
    run_id = "legacy-private-apollo-report"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_private_exa_and_clay(
        run_dir,
        run_id,
        clay_email="alice.owner@acme.com",
        include_legacy_apollo=True,
    )

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    apollo = outcome_helpers._provider(report, "apollo")
    assert apollo.source == "coverage_only"
    assert (apollo.integration_outcome, apollo.business_outcome) == (
        "failure",
        "invalid_evidence",
    )
    assert report.overall_outcome == "failure"
