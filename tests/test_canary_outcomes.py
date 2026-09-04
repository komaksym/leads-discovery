"""Deterministic canary outcome/report contracts; never calls live providers."""

from __future__ import annotations

import json
from pathlib import Path

from leads_discovery.contacts.models import ContactRecord, VerificationStatus
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


def _company() -> CompanyRecord:
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


def _contact(status: VerificationStatus | None = None) -> ContactRecord:
    contact = ContactRecord(
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
    contact.work_email = "alice@acme.com"
    contact.email_source = "clay"
    contact.email_verification_status = status
    return contact


def _write_normal(run_dir: Path, run_id: str, *, company: bool = True) -> None:
    operations: dict[str, dict[str, object]] = {
        "discovery:one": {
            "state": "completed",
            "provider": "exa",
            "operation": "company_search",
        }
    }
    events = [UsageEvent(provider="exa", operation="company_search")]
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
        events.extend(
            [
                UsageEvent(provider="exa", operation="company_research"),
                UsageEvent(provider="deepseek", operation="structured_extraction"),
            ]
        )
    write_json_atomic(
        run_dir / "checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            status="completed",
            provider_state={"operations": operations},
        ).to_dict(),
    )
    for event in events:
        append_jsonl(run_dir / "usage_events.jsonl", event.to_dict())
    write_jsonl_atomic(
        run_dir / "companies_evaluated.jsonl",
        [_company().to_dict()] if company else [],
    )


def _write_contacts(
    run_dir: Path,
    run_id: str,
    *,
    status: VerificationStatus | None = None,
    people_ids: list[str] | None = None,
    clay: bool = True,
    instantly: bool = False,
) -> ContactRecord | None:
    ids = ["con_acme"] if people_ids is None else people_ids
    contact = _contact(status) if ids else None
    write_jsonl_atomic(
        run_dir / "contacts.jsonl",
        [] if contact is None else [contact.to_dict()],
    )
    row = ""
    if contact is not None:
        row = (
            f"cmp_acme,con_acme,{contact.work_email or ''},"
            f"{contact.email_verification_status or ''},{contact.email_source or ''}\n"
        )
    write_text_atomic(
        run_dir / "leads.csv",
        "company_id,contact_id,work_email,email_verification_status,email_source\n" + row,
    )
    operations: dict[str, dict[str, object]] = {
        "exa:cmp_acme": {"state": "completed", "contact_ids": ids}
    }
    events = [UsageEvent(provider="exa", operation="people_search")]
    if contact is not None and clay:
        operations["clay:batch"] = {
            "state": "completed",
            "routine_run_id": "routine-one",
            "contact_ids": [contact.contact_id],
        }
        events.extend(
            [
                UsageEvent(provider="clay", operation="work_email_routine_start"),
                UsageEvent(provider="clay", operation="work_email_routine_results"),
            ]
        )
    if contact is not None and instantly:
        operations[f"instantly:{contact.contact_id}"] = {
            "state": "completed",
            "email": contact.work_email,
            "status": status,
        }
        events.append(UsageEvent(provider="instantly", operation="email_verification_create"))
    write_json_atomic(
        run_dir / "contact_checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            status="completed",
            provider_state={"operations": operations},
        ).to_dict(),
    )
    for event in events:
        append_jsonl(run_dir / "contact_usage_events.jsonl", event.to_dict())
    return contact


def _provider(report: CanaryCoverageReport, name: str) -> IntegrationCoverage:
    return next(item for item in report.providers if item.provider == name)


def test_no_company_is_inconclusive_and_empty_outputs_do_not_turn_green(tmp_path: Path) -> None:
    run_id = "no-company"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_normal(run_dir, run_id, company=False)
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

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    assert report.pipeline_outcome == "inconclusive"
    assert report.overall_outcome == "inconclusive"
    assert report.safety_flags == ()
    assert (_provider(report, "exa_discovery").integration_outcome) == "success"
    assert _provider(report, "exa_discovery").business_outcome == "no_company"


def test_exa_people_zero_result_is_success_with_downstream_inconclusive(tmp_path: Path) -> None:
    run_id = "people-zero"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_normal(run_dir, run_id)
    _write_contacts(run_dir, run_id, people_ids=[])

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    assert (_provider(report, "exa_people").integration_outcome) == "success"
    assert _provider(report, "exa_people").business_outcome == "no_qualifying_contact"
    assert _provider(report, "clay").integration_outcome == "inconclusive"
    assert _provider(report, "apollo").integration_outcome == "inconclusive"
    assert _provider(report, "instantly").integration_outcome == "inconclusive"
    assert report.overall_outcome == "inconclusive"


def test_instantly_invalid_is_provider_success_but_pipeline_inconclusive(tmp_path: Path) -> None:
    run_id = "instantly-invalid"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_normal(run_dir, run_id)
    _write_contacts(run_dir, run_id, status="invalid", instantly=True)

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    instantly = _provider(report, "instantly")
    assert (instantly.integration_outcome, instantly.business_outcome) == (
        "success",
        "invalid",
    )
    assert report.pipeline_outcome == "inconclusive"


def test_verified_pipeline_with_shadow_apollo_is_sanitized_and_rebuildable(
    tmp_path: Path,
) -> None:
    run_id = "verified-shadow-apollo"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_normal(run_dir, run_id)
    contact = _write_contacts(run_dir, run_id, status="verified", instantly=True)
    assert contact is not None

    private = CanaryPaidOperations.open(run_dir, run_id=run_id)
    input_value = contact.to_dict()
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
        fields={"business_outcome": "no_email", "credits_used": 1.0},
    )
    private.complete()
    usage_before = (run_dir / "canary_paid_usage_events.jsonl").read_text(encoding="utf-8")

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
    assert (run_dir / "canary_paid_usage_events.jsonl").read_text(encoding="utf-8") == usage_before
    assert read_json(run_dir / "canary_coverage_report.json") == first.to_dict()


def test_unknown_paid_normal_work_is_failure(tmp_path: Path) -> None:
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


def test_clay_requires_authoritative_start_and_results_usage(tmp_path: Path) -> None:
    run_id = "clay-missing-start"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_normal(run_dir, run_id)
    contact = _write_contacts(run_dir, run_id)
    assert contact is not None
    (run_dir / "contact_usage_events.jsonl").unlink()
    for event in (
        UsageEvent(provider="exa", operation="people_search"),
        UsageEvent(provider="clay", operation="work_email_routine_results"),
    ):
        append_jsonl(run_dir / "contact_usage_events.jsonl", event.to_dict())

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    assert _provider(report, "clay").integration_outcome == "failure"
    assert report.pipeline_outcome == "failure"
    assert report.overall_outcome == "failure"
