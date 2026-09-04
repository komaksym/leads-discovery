"""Highest-seam offline contracts for the fixed production canary composition."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from m3_factories import accepted_facts, build_company, write_jsonl
from m4_contract_fixtures import (
    ClayRoutineScript,
    WireStub,
    install_mock_http,
    json_body,
    person_result,
    prepare_evaluated_run,
    read_csv,
    read_jsonl,
    set_m4_credentials,
)

from leads_discovery import production_canary
from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.selection import select_contacts
from leads_discovery.models import CompanyRecord, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.state import append_jsonl, read_json, write_checkpoint

_EMAIL = "pat.owner@acmevalve.com"
_FALLBACK_EMAIL = "fallback.owner@acmevalve.com"
_PROFILE = "https://www.linkedin.com/in/pat-owner"


def _accepted_company() -> CompanyRecord:
    """Return one deterministic M3-accepted company for canary composition tests."""
    return build_company(
        facts=accepted_facts(),
        company_id="cmp_canary",
        name="Acme Valve",
        domain="acmevalve.com",
    )


def _rejected_company() -> CompanyRecord:
    """Return one deterministic hard-rejected company while preserving complete M3 evidence."""
    facts = accepted_facts()
    facts["known_current_direct_competitor_customer"] = (True, 0.95)
    return build_company(
        facts=facts,
        company_id="cmp_canary",
        name="Acme Valve",
        domain="acmevalve.com",
    )


def _person() -> dict[str, Any]:
    """Return one Exa row accepted by the production deterministic contact selector."""
    return person_result(
        name="Pat Owner",
        title="President and Owner",
        company="Acme Valve",
        domain="acmevalve.com",
        profile_url=_PROFILE,
    )


def _exa_one(request: httpx.Request) -> httpx.Response:
    """Return one production-parseable Exa People result."""
    payload = json_body(request)
    assert payload["category"] == "people"
    return httpx.Response(
        200,
        json={"results": [_person()], "costDollars": {"total": 0.001}},
    )


def _exa_zero(request: httpx.Request) -> httpx.Response:
    """Return one valid parsed zero-result Exa People response."""
    payload = json_body(request)
    assert payload["category"] == "people"
    return httpx.Response(200, json={"results": [], "costDollars": {"total": 0.001}})


def _terminal_instantly(
    status: str,
    *,
    expected_email: str,
) -> Callable[[httpx.Request], httpx.Response]:
    """Return one terminal Instantly responder and assert exact normalized input."""

    def responder(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert json_body(request)["email"] == expected_email
        return httpx.Response(
            200,
            json={
                "email": expected_email,
                "status": "completed",
                "verification_status": status,
                "credits_used": 1,
            },
        )

    return responder


def _seed_authoritative_run(
    data_root: Path,
    run_id: str,
    company: CompanyRecord,
) -> Path:
    """Seed only already-authoritative M1-M3 evidence; M4 and coverage stay fully real."""
    run_dir = prepare_evaluated_run(data_root, run_id, [company])
    payload = read_json(run_dir / "checkpoint.json")
    assert payload is not None
    checkpoint = RunCheckpoint.from_dict(payload)
    checkpoint.status = "completed"
    checkpoint.pending_company_id = None
    checkpoint.pending_stage = None
    checkpoint.pause_reason = None
    checkpoint.provider_state["operations"] = {
        "discovery:canary": {
            "state": "completed",
            "provider": "exa",
            "operation": "company_search",
        },
        f"research:{company.company_id}": {
            "state": "completed",
            "provider": "exa",
            "operation": "company_research",
        },
        f"extraction:{company.company_id}": {
            "state": "completed",
            "provider": "deepseek",
            "operation": "structured_extraction",
        },
    }
    write_checkpoint(run_dir / "checkpoint.json", checkpoint)

    usage_path = run_dir / "usage_events.jsonl"
    usage_path.write_text("", encoding="utf-8", newline="\n")
    for event in (
        UsageEvent(provider="exa", operation="company_search", request_count=1),
        UsageEvent(provider="exa", operation="company_research", request_count=1),
        UsageEvent(provider="deepseek", operation="structured_extraction", request_count=1),
    ):
        append_jsonl(usage_path, event.to_dict())
    return run_dir


def _install_seeded_run(
    monkeypatch: pytest.MonkeyPatch,
    data_root: Path,
    run_id: str,
    company: CompanyRecord,
) -> Path:
    """Let the canary call its normal run phase while keeping M1-M3 outside this ticket's scope."""
    real_cli = production_canary.cli_main
    seeded = False

    def seeded_cli(argv: Sequence[str] | None = None) -> int:
        nonlocal seeded
        assert argv is not None
        if argv[0] == "run":
            if not seeded:
                _seed_authoritative_run(data_root, run_id, company)
                seeded = True
            return 0
        return real_cli(argv)

    monkeypatch.setattr(production_canary, "cli_main", seeded_cli)
    return data_root / run_id


def _install_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    run_id: str,
    company: CompanyRecord,
    stub: WireStub,
) -> Path:
    """Install inert credentials, in-memory HTTP, and the authoritative completed M1-M3 seam."""
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)
    return _install_seeded_run(monkeypatch, tmp_path, run_id, company)


def _run_canary(data_root: Path, run_id: str) -> int:
    """Drive the same public fixed-canary entrypoint used by the credentialed workflow."""
    return production_canary.main(
        ["--run-id", run_id, "--data-root", str(data_root)]
    )


def _report(run_dir: Path) -> dict[str, Any]:
    """Read the derived sanitized report produced by the canary itself."""
    payload = json.loads((run_dir / "canary_coverage_report.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _provider(report: dict[str, Any], name: str) -> dict[str, Any]:
    """Return one named provider row from the deterministic coverage report."""
    providers = report.get("providers")
    assert isinstance(providers, list)
    for item in providers:
        if isinstance(item, dict) and item.get("provider") == name:
            return item
    raise AssertionError(f"missing provider coverage row: {name}")


def _operations(path: Path) -> dict[str, Any]:
    """Read one checkpoint's externally persisted operation map."""
    payload = read_json(path)
    assert payload is not None
    checkpoint = RunCheckpoint.from_dict(payload)
    operations = checkpoint.provider_state.get("operations")
    assert isinstance(operations, dict)
    return operations


def test_normal_clay_success_shadows_apollo_once_with_exact_selected_contact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clay remains canonical while skipped Apollo gets exactly one coverage-only call."""
    run_id = "canary-clay-success"
    clay = ClayRoutineScript([{"work_email": f" {_EMAIL.title()} "}])

    def apollo(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "credits_used": 1,
                "person": {"email": "shadow.owner@acmevalve.com", "linkedin_url": _PROFILE},
            },
        )

    stub = WireStub(
        {
            "exa": _exa_one,
            "clay": clay,
            "apollo": apollo,
            "instantly": _terminal_instantly("verified", expected_email=_EMAIL),
        }
    )
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _accepted_company(), stub)
    assert _run_canary(tmp_path, run_id) == 2
    clay.release_started()
    assert _run_canary(tmp_path, run_id) == 0

    contacts = read_jsonl(run_dir / "contacts.jsonl")
    assert len(contacts) == 1
    contact = ContactRecord.from_dict(contacts[0])
    assert contact.work_email == _EMAIL
    assert contact.email_source == "clay"
    assert contact.email_verification_status == "verified"

    clay_body = json_body(clay.posts[0])
    clay_item = clay_body["items"][0]
    assert clay_item["id"] == contact.contact_id
    assert clay_item["inputs"] == {
        "full_name": contact.full_name,
        "company_name": contact.company_name,
        "company_domain": contact.company_domain,
        "linkedin_url": contact.linkedin_url,
        "profile_url": contact.profile_url,
    }

    apollo_requests = stub.for_provider("apollo")
    assert len(apollo_requests) == 1
    apollo_body = json_body(apollo_requests[0])
    assert apollo_body["name"] == contact.full_name
    assert apollo_body["domain"] == contact.company_domain
    assert apollo_body["organization_name"] == contact.company_name
    assert apollo_body["linkedin_url"] == contact.linkedin_url

    instantly_requests = stub.for_provider("instantly")
    assert len(instantly_requests) == 1
    assert json_body(instantly_requests[0])["email"] == contact.work_email

    normal_operations = _operations(run_dir / "contact_checkpoint.json")
    assert not any(key.startswith("apollo:") for key in normal_operations)
    assert normal_operations[f"instantly:{contact.contact_id}"]["status"] == "verified"
    private_operations = _operations(run_dir / "canary_paid_checkpoint.json")
    assert private_operations["coverage:apollo"]["state"] == "completed"

    leads = read_csv(run_dir / "leads.csv")
    assert len(leads) == 1
    assert leads[0]["contact_id"] == contact.contact_id
    assert leads[0]["work_email"] == _EMAIL

    report = _report(run_dir)
    assert report["pipeline_outcome"] == "success"
    assert report["overall_outcome"] == "success"
    apollo_coverage = _provider(report, "apollo")
    assert apollo_coverage["source"] == "coverage_only"
    assert apollo_coverage["integration_outcome"] == "success"

    report_text = json.dumps(report, sort_keys=True)
    assert _EMAIL not in report_text
    assert "Pat Owner" not in report_text
    assert _PROFILE not in report_text

    request_count = len(stub.requests)
    report_before = report
    assert _run_canary(tmp_path, run_id) == 0
    assert len(stub.requests) == request_count
    assert _report(run_dir) == report_before


def test_normal_apollo_fallback_consumes_shared_slot_and_verifies_exact_fallback_email(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal Apollo fallback suppresses shadow Apollo and owns Instantly lineage."""
    run_id = "canary-apollo-fallback"
    clay = ClayRoutineScript([])

    def apollo(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "credits_used": 1,
                "person": {"email": f" {_FALLBACK_EMAIL.title()} ", "linkedin_url": _PROFILE},
            },
        )

    stub = WireStub(
        {
            "exa": _exa_one,
            "clay": clay,
            "apollo": apollo,
            "instantly": _terminal_instantly("verified", expected_email=_FALLBACK_EMAIL),
        }
    )
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _accepted_company(), stub)

    assert _run_canary(tmp_path, run_id) == 2
    clay.release_started()
    assert _run_canary(tmp_path, run_id) == 0

    assert len(stub.for_provider("apollo")) == 1
    assert len(stub.for_provider("instantly")) == 1
    contact = ContactRecord.from_dict(read_jsonl(run_dir / "contacts.jsonl")[0])
    assert contact.work_email == _FALLBACK_EMAIL
    assert contact.email_source == "apollo"
    assert json_body(stub.for_provider("instantly")[0])["email"] == _FALLBACK_EMAIL

    report = _report(run_dir)
    apollo_coverage = _provider(report, "apollo")
    assert apollo_coverage["source"] == "normal"
    assert apollo_coverage["integration_outcome"] == "success"
    assert apollo_coverage["usage"]["request_count"] == 1

    private_operations = _operations(run_dir / "canary_paid_checkpoint.json")
    assert "coverage:apollo" not in private_operations


def test_rejected_company_zero_result_uses_exact_company_and_no_synthetic_downstream_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected M4 stays empty while coverage-only Exa zero-result is provider success."""
    run_id = "canary-rejected-zero"
    stub = WireStub({"exa": _exa_zero})
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _rejected_company(), stub)

    assert _run_canary(tmp_path, run_id) == 2
    assert len(stub.requests) == 1
    exa_request = stub.requests[0]
    evaluated = read_jsonl(run_dir / "companies_evaluated.jsonl")
    assert len(evaluated) == 1
    assert evaluated[0]["final_decision"] == "rejected"
    assert evaluated[0]["name"] in json_body(exa_request)["query"]

    assert _operations(run_dir / "contact_checkpoint.json") == {}
    assert read_jsonl(run_dir / "contacts.jsonl") == []
    assert read_csv(run_dir / "leads.csv") == []
    assert len(stub.for_provider("clay")) == 0
    assert len(stub.for_provider("apollo")) == 0
    assert len(stub.for_provider("instantly")) == 0

    private_operations = _operations(run_dir / "canary_paid_checkpoint.json")
    assert private_operations["coverage:exa_people"]["state"] == "completed"

    report = _report(run_dir)
    assert report["pipeline_outcome"] == "inconclusive"
    assert report["overall_outcome"] == "inconclusive"
    exa_coverage = _provider(report, "exa_people")
    assert exa_coverage["source"] == "coverage_only"
    assert exa_coverage["integration_outcome"] == "success"
    assert exa_coverage["business_outcome"] == "no_qualifying_contact"
    assert exa_coverage["usage"]["request_count"] == 1
    for provider_name in ("clay", "apollo", "instantly"):
        row = _provider(report, provider_name)
        assert row["integration_outcome"] == "inconclusive"
        assert row["usage"]["request_count"] == 0

    request_count = len(stub.requests)
    assert _run_canary(tmp_path, run_id) == 2
    assert len(stub.requests) == request_count


def test_valid_clay_no_email_and_apollo_no_match_are_provider_successes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Business misses stay distinct from integration failure and never synthesize verification."""
    run_id = "canary-business-misses"
    clay = ClayRoutineScript([])

    def apollo(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"credits_used": 1, "person": None})

    stub = WireStub({"exa": _exa_one, "clay": clay, "apollo": apollo})
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _accepted_company(), stub)

    assert _run_canary(tmp_path, run_id) == 2
    clay.release_started()
    assert _run_canary(tmp_path, run_id) == 2

    assert len(stub.for_provider("apollo")) == 1
    assert len(stub.for_provider("instantly")) == 0
    contact = ContactRecord.from_dict(read_jsonl(run_dir / "contacts.jsonl")[0])
    assert contact.work_email is None
    assert contact.email_source is None
    assert contact.email_verification_status is None

    report = _report(run_dir)
    clay_coverage = _provider(report, "clay")
    apollo_coverage = _provider(report, "apollo")
    instantly_coverage = _provider(report, "instantly")
    assert clay_coverage["integration_outcome"] == "success"
    assert clay_coverage["business_outcome"] == "no_email"
    assert apollo_coverage["integration_outcome"] == "success"
    assert apollo_coverage["business_outcome"] == "no_match"
    assert instantly_coverage["integration_outcome"] == "inconclusive"
    assert report["pipeline_outcome"] == "inconclusive"
    assert report["overall_outcome"] == "inconclusive"


def test_instantly_invalid_is_provider_success_but_pipeline_remains_inconclusive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parsed invalid verification proves integration health without creating a verified lead."""
    run_id = "canary-instantly-invalid"
    clay = ClayRoutineScript([{"work_email": _EMAIL}])

    def apollo(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"credits_used": 1, "person": None})

    stub = WireStub(
        {
            "exa": _exa_one,
            "clay": clay,
            "apollo": apollo,
            "instantly": _terminal_instantly("invalid", expected_email=_EMAIL),
        }
    )
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _accepted_company(), stub)

    assert _run_canary(tmp_path, run_id) == 2
    clay.release_started()
    assert _run_canary(tmp_path, run_id) == 2

    contact = ContactRecord.from_dict(read_jsonl(run_dir / "contacts.jsonl")[0])
    assert contact.work_email == _EMAIL
    assert contact.email_verification_status == "invalid"
    assert read_csv(run_dir / "leads.csv") == []

    report = _report(run_dir)
    instantly_coverage = _provider(report, "instantly")
    assert instantly_coverage["source"] == "normal"
    assert instantly_coverage["integration_outcome"] == "success"
    assert instantly_coverage["business_outcome"] == "invalid"
    assert report["pipeline_outcome"] == "inconclusive"
    assert report["overall_outcome"] == "inconclusive"


@pytest.mark.parametrize("failure_mode", ["authentication", "schema", "transport"])
def test_coverage_paid_failure_has_durable_intent_and_never_redispatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    """Attempted coverage failure is durable, fails the canary, and freezes replay."""
    run_id = f"canary-coverage-{failure_mode}"
    run_dir = tmp_path / run_id
    clay = ClayRoutineScript([])
    observed_intent: list[dict[str, Any]] = []

    def apollo(request: httpx.Request) -> httpx.Response:
        checkpoint = read_json(run_dir / "canary_paid_checkpoint.json")
        assert checkpoint is not None
        operations = checkpoint["provider_state"]["operations"]
        entry = operations["coverage:apollo"]
        assert entry["state"] == "in_flight"
        assert entry["dispatch_usage_recorded"] is False
        observed_intent.append(dict(entry))
        if failure_mode == "authentication":
            return httpx.Response(401, json={"error": "unauthorized"})
        if failure_mode == "schema":
            return httpx.Response(200, json={"credits_used": "one", "person": None})
        raise httpx.ReadTimeout("unknown paid outcome", request=request)

    stub = WireStub({"exa": _exa_one, "clay": clay, "apollo": apollo})
    _install_contract(monkeypatch, tmp_path, run_id, _rejected_company(), stub)

    assert _run_canary(tmp_path, run_id) == 2
    clay.release_started()
    assert _run_canary(tmp_path, run_id) == 1
    assert len(observed_intent) == 1
    assert len(stub.for_provider("apollo")) == 1
    assert len(stub.for_provider("instantly")) == 0

    private_operations = _operations(run_dir / "canary_paid_checkpoint.json")
    apollo_state = private_operations["coverage:apollo"]
    assert apollo_state["state"] == "in_flight"
    assert apollo_state["dispatch_usage_recorded"] is True

    report = _report(run_dir)
    assert report["overall_outcome"] == "failure"
    assert "coverage_paid_outcome_unresolved" in report["safety_flags"]
    assert _provider(report, "apollo")["integration_outcome"] == "failure"

    request_count = len(stub.requests)
    assert _run_canary(tmp_path, run_id) == 1
    assert len(stub.requests) == request_count
    assert len(stub.for_provider("instantly")) == 0


def test_coverage_clay_resumes_same_operation_and_stops_at_three_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage-only Clay never creates a replacement start and obeys the fixed read ceiling."""
    run_id = "canary-clay-read-ceiling"
    clay = ClayRoutineScript([{"work_email": _EMAIL}])
    stub = WireStub({"exa": _exa_one, "clay": clay})
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _rejected_company(), stub)

    assert _run_canary(tmp_path, run_id) == 2
    for _ in range(4):
        assert _run_canary(tmp_path, run_id) == 2

    assert len(clay.posts) == 1
    assert len(clay.gets) == 3
    routine_run_id = clay.latest_run_id
    assert routine_run_id is not None
    assert {
        request.url.path for request in clay.gets
    } == {f"/public/v0/routines/run/{routine_run_id}/results"}
    assert len(stub.for_provider("apollo")) == 0
    assert len(stub.for_provider("instantly")) == 0

    private_operations = _operations(run_dir / "canary_paid_checkpoint.json")
    clay_state = private_operations["coverage:clay"]
    assert clay_state["state"] == "pending"
    assert clay_state["dispatch_sequence"] == 3


def test_coverage_instantly_resumes_same_email_and_stops_at_three_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage-only Instantly reuses one create identity and obeys the fixed GET ceiling."""
    run_id = "canary-instantly-read-ceiling"
    clay = ClayRoutineScript([{"work_email": _EMAIL}])

    def apollo(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"credits_used": 1, "person": None})

    def instantly(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert json_body(request)["email"] == _EMAIL
            credits = 1
        else:
            assert request.method == "GET"
            assert request.url.path == f"/api/v2/email-verification/{_EMAIL}"
            credits = 0
        return httpx.Response(
            202,
            json={
                "email": _EMAIL,
                "status": "completed",
                "verification_status": "pending",
                "credits_used": credits,
            },
        )

    stub = WireStub(
        {"exa": _exa_one, "clay": clay, "apollo": apollo, "instantly": instantly}
    )
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _rejected_company(), stub)

    assert _run_canary(tmp_path, run_id) == 2
    clay.release_started()
    assert _run_canary(tmp_path, run_id) == 2
    for _ in range(4):
        assert _run_canary(tmp_path, run_id) == 2

    instantly_requests = stub.for_provider("instantly")
    posts = [request for request in instantly_requests if request.method == "POST"]
    gets = [request for request in instantly_requests if request.method == "GET"]
    assert len(posts) == 1
    assert len(gets) == 3
    assert {request.url.path for request in gets} == {
        f"/api/v2/email-verification/{_EMAIL}"
    }
    assert len(stub.for_provider("apollo")) == 1

    private_operations = _operations(run_dir / "canary_paid_checkpoint.json")
    instantly_state = private_operations["coverage:instantly"]
    assert instantly_state["state"] == "pending"
    assert instantly_state["dispatch_sequence"] == 3


def test_company_fingerprint_mismatch_fails_closed_without_new_provider_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persisted coverage state cannot be attached to a changed evaluated company."""
    run_id = "canary-company-fingerprint"
    clay = ClayRoutineScript([{"work_email": _EMAIL}])
    stub = WireStub({"exa": _exa_one, "clay": clay})
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _rejected_company(), stub)

    assert _run_canary(tmp_path, run_id) == 2
    request_count = len(stub.requests)

    evaluated_path = run_dir / "companies_evaluated.jsonl"
    evaluated = read_jsonl(evaluated_path)
    evaluated[0]["name"] = "Changed Valve"
    evaluated[0]["normalized_name"] = "changed valve"
    write_jsonl(evaluated_path, evaluated)

    assert _run_canary(tmp_path, run_id) == 1
    assert len(stub.requests) == request_count


def test_contact_fingerprint_mismatch_fails_closed_without_reusing_shadow_apollo_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completed shadow state cannot be re-associated with a changed canonical contact."""
    run_id = "canary-contact-fingerprint"
    clay = ClayRoutineScript([{"work_email": _EMAIL}])

    def apollo(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"credits_used": 1, "person": None})

    stub = WireStub(
        {
            "exa": _exa_one,
            "clay": clay,
            "apollo": apollo,
            "instantly": _terminal_instantly("verified", expected_email=_EMAIL),
        }
    )
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _accepted_company(), stub)

    assert _run_canary(tmp_path, run_id) == 2
    clay.release_started()
    assert _run_canary(tmp_path, run_id) == 0
    request_count = len(stub.requests)

    contacts_path = run_dir / "contacts.jsonl"
    contacts = read_jsonl(contacts_path)
    contacts[0]["full_name"] = "Changed Person"
    write_jsonl(contacts_path, contacts)

    assert _run_canary(tmp_path, run_id) == 1
    assert len(stub.requests) == request_count
    assert len(stub.for_provider("apollo")) == 1


def test_present_contact_artifact_without_durable_exa_evidence_does_not_suppress_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Output-file presence is never provider authority; operation plus usage evidence is."""
    run_id = "canary-stale-contact-artifact"
    stub = WireStub({"exa": _exa_zero})
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _rejected_company(), stub)
    real_coverage = production_canary.run_live_provider_coverage

    def coverage_with_stale_contact(data_root: Path, *, run_id: str) -> Any:
        evaluated_rows = read_jsonl(data_root / run_id / "companies_evaluated.jsonl")
        evaluated = CompanyRecord.from_dict(evaluated_rows[0])
        selected = select_contacts(evaluated, [_person()], limit=1)
        assert len(selected) == 1
        write_jsonl(
            data_root / run_id / "contacts.jsonl",
            [selected[0].to_dict()],
        )
        return real_coverage(data_root, run_id=run_id)

    monkeypatch.setattr(
        production_canary,
        "run_live_provider_coverage",
        coverage_with_stale_contact,
    )

    assert _run_canary(tmp_path, run_id) == 2
    assert len(stub.for_provider("exa")) == 1
    assert _operations(run_dir / "contact_checkpoint.json") == {}
    private_operations = _operations(run_dir / "canary_paid_checkpoint.json")
    assert private_operations["coverage:exa_people"]["state"] == "completed"
    assert len(stub.for_provider("clay")) == 0
    assert len(stub.for_provider("apollo")) == 0
    assert len(stub.for_provider("instantly")) == 0
