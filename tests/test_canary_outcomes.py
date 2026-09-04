"""Deterministic canary evidence-model tests with no live provider calls."""

from __future__ import annotations

import json
from pathlib import Path

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.models import CompanyRecord, EvidenceItem, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.canary_outcomes import (
    CanaryCoverageReport,
    IntegrationCoverage,
    build_canary_coverage_report,
)
from leads_discovery.pipeline.canary_paid_operations import CanaryPaidOperations
from leads_discovery.pipeline.state import (
    append_jsonl,
    read_json,
    write_json_atomic,
    write_jsonl_atomic,
    write_text_atomic,
)


def _accepted_company() -> CompanyRecord:
    """Build one currently accepted evaluated company with real research evidence."""
    return CompanyRecord(
        company_id="cmp_acme",
        name="Acme Valve",
        normalized_name="acme valve",
        domain="acme.com",
        normalized_domain="acme.com",
        country="US",
        evidence=[
            EvidenceItem(
                evidence_id="ev_1",
                url="https://acme.com/about",
                excerpt="Industrial valve distributor.",
                provider="exa",
            )
        ],
        final_decision="accepted",
        stage_status={
            "research": "completed",
            "extraction": "completed",
            "decision": "completed",
        },
    )


def _m2_checkpoint(run_id: str, *, company: bool = True) -> RunCheckpoint:
    """Build completed normal M1-M3 authority for the fixed canary."""
    operations: dict[str, dict[str, object]] = {
        "discovery:one": {
            "state": "completed",
            "provider": "exa",
            "operation": "company_search",
        }
    }
    if company:
        operations.update(
            {
                "research:cmp_acme": {
                    "state": "completed",
                    "provider": "exa",
                    "operation": "company_research",
                },
                "extraction:cmp_acme": {
                    "state": "completed",
                    "provider": "deepseek",
                    "operation": "structured_extraction",
                },
            }
        )
    return RunCheckpoint(
        run_id=run_id,
        status="completed",
        provider_state={
            "operations": operations,
            "stages": {"evaluation": "completed", "m3_pipeline": "completed"},
        },
    )


def _write_m2(run_dir: Path, run_id: str, *, company: bool = True) -> None:
    """Persist deterministic normal M1-M3 checkpoints, usage, and evaluated output."""
    write_json_atomic(
        run_dir / "checkpoint.json",
        _m2_checkpoint(run_id, company=company).to_dict(),
    )
    append_jsonl(
        run_dir / "usage_events.jsonl",
        UsageEvent(provider="exa", operation="company_search").to_dict(),
    )
    if company:
        append_jsonl(
            run_dir / "usage_events.jsonl",
            UsageEvent(provider="exa", operation="company_research").to_dict(),
        )
        append_jsonl(
            run_dir / "usage_events.jsonl",
            UsageEvent(provider="deepseek", operation="structured_extraction").to_dict(),
        )
        write_jsonl_atomic(run_dir / "companies_evaluated.jsonl", [_accepted_company().to_dict()])
    else:
        write_jsonl_atomic(run_dir / "companies_evaluated.jsonl", [])


def _contact() -> ContactRecord:
    """Build one canonical selected contact containing PII that the report must omit."""
    return ContactRecord(
        contact_id="con_acme",
        company_id="cmp_acme",
        company_name="Acme Valve",
        company_domain="acme.com",
        company_final_score=0.9,
        full_name="Alice Example",
        title="VP Sales",
        decision_rank=1,
        decision_reason="seniority",
        linkedin_url="https://linkedin.com/in/alice-example",
        profile_url="https://example.invalid/alice",
    )


def _write_empty_public_outputs(run_dir: Path) -> None:
    """Persist the real M4 publication shapes with no canonical contacts."""
    write_jsonl_atomic(run_dir / "contacts.jsonl", [])
    write_text_atomic(
        run_dir / "leads.csv",
        "company_id,contact_id,work_email,email_verification_status,email_source\n",
    )


def _provider(report: CanaryCoverageReport, name: str) -> IntegrationCoverage:
    """Return one named provider row from a typed report."""
    return next(item for item in report.providers if item.provider == name)


def test_no_company_is_inconclusive_without_turning_empty_files_green(tmp_path: Path) -> None:
    """A valid empty business result is not a pipeline failure and cannot be a success."""
    run_id = "no-company"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_m2(run_dir, run_id, company=False)
    write_json_atomic(
        run_dir / "contact_checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            status="completed",
            provider_state={"operations": {}},
        ).to_dict(),
    )
    _write_empty_public_outputs(run_dir)

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    assert report.pipeline_outcome == "inconclusive"
    assert report.overall_outcome == "inconclusive"
    assert report.safety_flags == ()
    discovery = _provider(report, "exa_discovery")
    assert discovery.integration_outcome == "success"
    assert discovery.business_outcome == "no_company"


def test_provider_business_scarcity_stays_successful_integration(tmp_path: Path) -> None:
    """Exa zero-result and Apollo no-match remain parsed provider successes, not failures."""
    run_id = "business-scarcity"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_m2(run_dir, run_id)
    contact = _contact()
    write_jsonl_atomic(run_dir / "contacts.jsonl", [contact.to_dict()])
    write_text_atomic(
        run_dir / "leads.csv",
        "company_id,contact_id,work_email,email_verification_status,email_source\n"
        "cmp_acme,con_acme,,,\n",
    )
    write_json_atomic(
        run_dir / "contact_checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            status="completed",
            provider_state={
                "operations": {
                    "exa:cmp_acme": {"state": "completed", "contact_ids": ["con_acme"]},
                    "clay:batch": {
                        "state": "completed",
                        "routine_run_id": "routine-one",
                        "contact_ids": ["con_acme"],
                    },
                    "apollo:con_acme": {"state": "completed", "credits_used": 1.0},
                }
            },
        ).to_dict(),
    )
    for event in (
        UsageEvent(provider="exa", operation="people_search"),
        UsageEvent(provider="clay", operation="work_email_routine_start"),
        UsageEvent(provider="clay", operation="work_email_routine_results"),
        UsageEvent(
            provider="apollo",
            operation="people_enrichment",
            metadata={"matched": False, "credits_used": 1.0},
        ),
    ):
        append_jsonl(run_dir / "contact_usage_events.jsonl", event.to_dict())

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    assert report.pipeline_outcome == "inconclusive"
    exa = _provider(report, "exa_people")
    clay = _provider(report, "clay")
    apollo = _provider(report, "apollo")
    assert (exa.integration_outcome, exa.business_outcome) == ("success", "contact_selected")
    assert (clay.integration_outcome, clay.business_outcome) == ("success", "no_email")
    assert (apollo.integration_outcome, apollo.business_outcome) == ("success", "no_match")


def test_exa_people_zero_result_is_success_with_downstream_inconclusive(tmp_path: Path) -> None:
    """A parsed zero-result People response is coverage, but it creates no fake contact."""
    run_id = "people-zero"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_m2(run_dir, run_id)
    write_json_atomic(
        run_dir / "contact_checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            status="completed",
            provider_state={
                "operations": {
                    "exa:cmp_acme": {"state": "completed", "contact_ids": []},
                }
            },
        ).to_dict(),
    )
    append_jsonl(
        run_dir / "contact_usage_events.jsonl",
        UsageEvent(provider="exa", operation="people_search").to_dict(),
    )
    _write_empty_public_outputs(run_dir)

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    exa = _provider(report, "exa_people")
    assert (exa.integration_outcome, exa.business_outcome) == (
        "success",
        "no_qualifying_contact",
    )
    assert _provider(report, "clay").integration_outcome == "inconclusive"
    assert _provider(report, "apollo").integration_outcome == "inconclusive"
    assert _provider(report, "instantly").integration_outcome == "inconclusive"
    assert report.overall_outcome == "inconclusive"


def test_verified_pipeline_plus_shadow_apollo_yields_sanitized_rebuildable_success(
    tmp_path: Path,
) -> None:
    """A private Apollo no-match can complete coverage without changing canonical lead truth."""
    run_id = "verified-shadow-apollo"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_m2(run_dir, run_id)
    contact = _contact()
    contact.work_email = "alice@acme.com"
    contact.email_source = "clay"
    contact.email_verification_status = "verified"
    write_jsonl_atomic(run_dir / "contacts.jsonl", [contact.to_dict()])
    write_text_atomic(
        run_dir / "leads.csv",
        "company_id,contact_id,work_email,email_verification_status,email_source\n"
        "cmp_acme,con_acme,alice@acme.com,verified,clay\n",
    )
    write_json_atomic(
        run_dir / "contact_checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            status="completed",
            provider_state={
                "operations": {
                    "exa:cmp_acme": {"state": "completed", "contact_ids": ["con_acme"]},
                    "clay:batch": {
                        "state": "completed",
                        "routine_run_id": "routine-one",
                        "contact_ids": ["con_acme"],
                    },
                    "instantly:con_acme": {
                        "state": "completed",
                        "email": "alice@acme.com",
                        "status": "verified",
                    },
                }
            },
        ).to_dict(),
    )
    for event in (
        UsageEvent(provider="exa", operation="people_search"),
        UsageEvent(provider="clay", operation="work_email_routine_start"),
        UsageEvent(provider="clay", operation="work_email_routine_results"),
        UsageEvent(provider="instantly", operation="email_verification_create"),
    ):
        append_jsonl(run_dir / "contact_usage_events.jsonl", event.to_dict())

    private = CanaryPaidOperations.open(run_dir, run_id=run_id)
    input_value = {"contact_id": contact.contact_id}
    private.begin("coverage:apollo", "apollo_enrichment", input_value=input_value)
    private.record_usage(
        "coverage:apollo",
        "apollo_enrichment",
        input_value=input_value,
        event=UsageEvent(
            provider="apollo",
            operation="people_enrichment",
            metadata={"matched": False, "credits_used": 1.0},
        ),
    )
    private.finish(
        "coverage:apollo",
        input_value=input_value,
        fields={"business_outcome": "no_match"},
    )
    private.complete()
    private_usage_before = (run_dir / "canary_paid_usage_events.jsonl").read_text(encoding="utf-8")

    first = build_canary_coverage_report(tmp_path, run_id=run_id)
    serialized = json.dumps(first.to_dict(), sort_keys=True)
    assert first.pipeline_outcome == "success"
    assert first.overall_outcome == "success"
    apollo = _provider(first, "apollo")
    assert apollo.source == "coverage_only"
    assert (apollo.integration_outcome, apollo.business_outcome) == ("success", "no_match")
    for forbidden in (
        "Acme Valve",
        "Alice Example",
        "alice@acme.com",
        "linkedin.com/in/alice-example",
        "example.invalid/alice",
        "con_acme",
    ):
        assert forbidden not in serialized

    write_text_atomic(run_dir / "canary_coverage_report.json", "{}\n")
    second = build_canary_coverage_report(tmp_path, run_id=run_id)
    assert second.to_dict() == first.to_dict()
    assert (
        run_dir / "canary_paid_usage_events.jsonl"
    ).read_text(encoding="utf-8") == private_usage_before
    assert read_json(run_dir / "canary_coverage_report.json") == first.to_dict()


def test_unknown_paid_normal_work_is_failure_not_inconclusive(tmp_path: Path) -> None:
    """Potentially billed unknown state fails both normal pipeline and overall readiness."""
    run_id = "unknown-paid"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    write_json_atomic(
        run_dir / "checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            status="paused_unknown",
            pause_reason="unknown_in_flight:discovery:one",
            provider_state={
                "operations": {
                    "discovery:one": {
                        "state": "in_flight",
                        "provider": "exa",
                        "operation": "company_search",
                    }
                }
            },
        ).to_dict(),
    )

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    assert report.pipeline_outcome == "failure"
    assert report.overall_outcome == "failure"
    assert "normal_paid_outcome_unresolved" in report.safety_flags
