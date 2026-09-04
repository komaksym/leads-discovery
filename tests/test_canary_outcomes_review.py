"""Regression coverage for issue #49 review findings; never calls live providers."""

from __future__ import annotations

from pathlib import Path

import pytest

from leads_discovery.contacts.models import ContactRecord, VerificationStatus
from leads_discovery.models import CompanyRecord, EvidenceItem, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.canary_outcomes import (
    CanaryCoverageReport,
    build_canary_coverage_report,
)
from leads_discovery.pipeline.state import (
    append_jsonl,
    write_json_atomic,
    write_jsonl_atomic,
    write_text_atomic,
)


def _accepted_company() -> CompanyRecord:
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


def _contact(*, status: VerificationStatus | None = None) -> ContactRecord:
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


def _write_m1_m3(run_dir: Path, run_id: str) -> None:
    write_json_atomic(
        run_dir / "checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            status="completed",
            provider_state={
                "operations": {
                    "discovery:one": {
                        "state": "completed",
                        "provider": "exa",
                        "operation": "company_search",
                    },
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
            },
        ).to_dict(),
    )
    for event in (
        UsageEvent(provider="exa", operation="company_search"),
        UsageEvent(provider="exa", operation="company_research"),
        UsageEvent(provider="deepseek", operation="structured_extraction"),
    ):
        append_jsonl(run_dir / "usage_events.jsonl", event.to_dict())
    write_jsonl_atomic(
        run_dir / "companies_evaluated.jsonl",
        [_accepted_company().to_dict()],
    )


def _write_contact_artifacts(
    run_dir: Path,
    run_id: str,
    *,
    instant_status: VerificationStatus | None = None,
    checkpoint_status: str = "completed",
    clay_state: str = "completed",
    malformed_instantly: bool = False,
) -> None:
    contact = _contact(status=instant_status)
    write_jsonl_atomic(run_dir / "contacts.jsonl", [contact.to_dict()])
    verification = instant_status or ""
    write_text_atomic(
        run_dir / "leads.csv",
        "company_id,contact_id,work_email,email_verification_status,email_source\n"
        f"cmp_acme,con_acme,alice@acme.com,{verification},clay\n",
    )
    operations: dict[str, dict[str, object]] = {
        "exa:cmp_acme": {"state": "completed", "contact_ids": ["con_acme"]},
        "clay:batch": {
            "state": clay_state,
            "routine_run_id": "routine-one",
            "contact_ids": ["con_acme"],
        },
    }
    pause_reason: str | None = None
    if clay_state == "pending":
        checkpoint_status = "paused_pending"
        pause_reason = "clay_pending"
    if instant_status is not None:
        instantly: dict[str, object] = {
            "state": "completed",
            "email": "alice@acme.com",
            "status": instant_status,
        }
        if malformed_instantly:
            instantly["unexpected"] = "must-fail-closed"
        operations["instantly:con_acme"] = instantly
    write_json_atomic(
        run_dir / "contact_checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            status=checkpoint_status,
            pause_reason=pause_reason,
            provider_state={"operations": operations},
        ).to_dict(),
    )


def _provider_outcome(
    report: CanaryCoverageReport,
    provider: str,
) -> tuple[str, str]:
    row = next(item for item in report.providers if item.provider == provider)
    return row.integration_outcome, row.business_outcome


@pytest.mark.parametrize("clay_state", ["pending", "completed"])
def test_normal_clay_success_requires_start_and_results_usage(
    tmp_path: Path,
    clay_state: str,
) -> None:
    run_id = f"clay-missing-start-{clay_state}"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_m1_m3(run_dir, run_id)
    _write_contact_artifacts(run_dir, run_id, clay_state=clay_state)
    for event in (
        UsageEvent(provider="exa", operation="people_search"),
        UsageEvent(provider="clay", operation="work_email_routine_results"),
    ):
        append_jsonl(run_dir / "contact_usage_events.jsonl", event.to_dict())

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    assert _provider_outcome(report, "clay")[0] == "failure"
    assert report.pipeline_outcome == "failure"
    assert report.overall_outcome == "failure"


def test_terminal_instantly_requires_authoritative_create_usage(tmp_path: Path) -> None:
    run_id = "instantly-get-without-create"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_m1_m3(run_dir, run_id)
    _write_contact_artifacts(run_dir, run_id, instant_status="verified")
    for event in (
        UsageEvent(provider="exa", operation="people_search"),
        UsageEvent(provider="clay", operation="work_email_routine_start"),
        UsageEvent(provider="clay", operation="work_email_routine_results"),
        UsageEvent(provider="instantly", operation="email_verification_get"),
    ):
        append_jsonl(run_dir / "contact_usage_events.jsonl", event.to_dict())

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    assert _provider_outcome(report, "instantly")[0] == "failure"
    assert report.pipeline_outcome == "failure"
    assert report.overall_outcome == "failure"


def test_instantly_invalid_is_provider_success_but_pipeline_inconclusive(tmp_path: Path) -> None:
    run_id = "instantly-invalid"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_m1_m3(run_dir, run_id)
    _write_contact_artifacts(run_dir, run_id, instant_status="invalid")
    for event in (
        UsageEvent(provider="exa", operation="people_search"),
        UsageEvent(provider="clay", operation="work_email_routine_start"),
        UsageEvent(provider="clay", operation="work_email_routine_results"),
        UsageEvent(provider="instantly", operation="email_verification_create"),
    ):
        append_jsonl(run_dir / "contact_usage_events.jsonl", event.to_dict())

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    assert _provider_outcome(report, "instantly") == ("success", "invalid")
    assert report.pipeline_outcome == "inconclusive"


def test_malformed_normal_provider_state_cannot_be_masked_by_verified_outputs(
    tmp_path: Path,
) -> None:
    run_id = "malformed-normal-provider-state"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_m1_m3(run_dir, run_id)
    _write_contact_artifacts(
        run_dir,
        run_id,
        instant_status="verified",
        malformed_instantly=True,
    )
    for event in (
        UsageEvent(provider="exa", operation="people_search"),
        UsageEvent(provider="clay", operation="work_email_routine_start"),
        UsageEvent(provider="clay", operation="work_email_routine_results"),
        UsageEvent(provider="instantly", operation="email_verification_create"),
    ):
        append_jsonl(run_dir / "contact_usage_events.jsonl", event.to_dict())

    report = build_canary_coverage_report(tmp_path, run_id=run_id)

    assert report.pipeline_outcome == "failure"
    assert report.overall_outcome == "failure"
    assert "contact_state_invalid" in report.safety_flags


def test_coverage_only_failure_does_not_change_normal_pipeline_truth(tmp_path: Path) -> None:
    run_id = "coverage-only-failure"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    _write_m1_m3(run_dir, run_id)
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
    exa_people = next(item for item in report.providers if item.provider == "exa_people")

    assert exa_people.source == "coverage_only"
    assert exa_people.integration_outcome == "failure"
    assert report.pipeline_outcome == "inconclusive"
    assert report.overall_outcome == "failure"
