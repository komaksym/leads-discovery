"""Review-gap contracts for the fixed production canary composition."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from m3_factories import accepted_facts, build_company
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
from leads_discovery.cli import main as cli_main
from leads_discovery.contacts.selection import select_contacts
from leads_discovery.models import CompanyRecord, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.canary_provider_coverage import (
    CanaryProviderCoverageSummary,
    run_live_provider_coverage,
)
from leads_discovery.pipeline.state import append_jsonl, read_json, write_checkpoint

_EMAIL = "coverage.owner@acmevalve.com"
_PROFILE = "https://www.linkedin.com/in/pat-owner"
_CANONICAL_AND_NORMAL_ARTIFACTS = (
    "companies_evaluated.jsonl",
    "checkpoint.json",
    "contacts.jsonl",
    "leads.csv",
    "contact_usage_events.jsonl",
    "contact_usage.json",
    "contact_checkpoint.json",
)


def _rejected_company() -> CompanyRecord:
    """Return one deterministic hard-rejected company with otherwise complete evidence."""
    facts = accepted_facts()
    facts["known_current_direct_competitor_customer"] = (True, 0.95)
    return build_company(
        facts=facts,
        company_id="cmp_canary",
        name="Acme Valve",
        domain="acmevalve.com",
    )


def _uncertain_company() -> CompanyRecord:
    """Return one deterministic review-gated company without triggering hard rejection."""
    facts = accepted_facts()
    facts["pvf_relevant"] = (True, 0.70)
    return build_company(
        facts=facts,
        company_id="cmp_canary",
        name="Acme Valve",
        domain="acmevalve.com",
    )


def _person() -> dict[str, Any]:
    """Return one Exa row accepted by the production deterministic selector."""
    return person_result(
        name="Pat Owner",
        title="President and Owner",
        company="Acme Valve",
        domain="acmevalve.com",
        profile_url=_PROFILE,
    )


def _exa_one(request: httpx.Request) -> httpx.Response:
    """Return one production-parseable Exa People result."""
    assert json_body(request)["category"] == "people"
    return httpx.Response(
        200,
        json={"results": [_person()], "costDollars": {"total": 0.001}},
    )


def _exa_zero(request: httpx.Request) -> httpx.Response:
    """Return a valid parsed zero-result Exa People response."""
    assert json_body(request)["category"] == "people"
    return httpx.Response(200, json={"results": [], "costDollars": {"total": 0.001}})


def _terminal_instantly(request: httpx.Request) -> httpx.Response:
    """Return one terminal verified result for the exact coverage-derived email."""
    assert request.method == "POST"
    assert json_body(request)["email"] == _EMAIL
    return httpx.Response(
        200,
        json={
            "email": _EMAIL,
            "status": "completed",
            "verification_status": "verified",
            "credits_used": 1,
        },
    )


def _seed_authoritative_run(
    data_root: Path,
    run_id: str,
    company: CompanyRecord,
) -> Path:
    """Seed only already-authoritative M1-M3 evidence; M4 and coverage remain real."""
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
    """Let the canary call its normal run phase while M1-M3 remain outside this ticket."""
    real_cli = cli_main
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
    """Install inert credentials, in-memory HTTP, and the authoritative M1-M3 seam."""
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)
    return _install_seeded_run(monkeypatch, tmp_path, run_id, company)


def _run_canary(data_root: Path, run_id: str) -> int:
    """Drive the public fixed-canary entrypoint used by production readiness."""
    return production_canary.main(
        ["--run-id", run_id, "--data-root", str(data_root)]
    )


def _operations(path: Path) -> dict[str, Any]:
    """Read one checkpoint's externally persisted operation map."""
    payload = read_json(path)
    assert payload is not None
    checkpoint = RunCheckpoint.from_dict(payload)
    operations = checkpoint.provider_state.get("operations")
    assert isinstance(operations, dict)
    return operations


def _canonical_and_normal_snapshot(run_dir: Path) -> dict[str, bytes]:
    """Snapshot every canonical artifact plus authoritative normal M4 fallback state."""
    return {
        name: (run_dir / name).read_bytes()
        for name in _CANONICAL_AND_NORMAL_ARTIFACTS
    }


def test_successful_coverage_only_waterfall_uses_selected_contact_without_mutating_normal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage-only Clay/Apollo use selector output and cannot change canonical M4 state."""
    run_id = "canary-review-coverage-immutability"
    clay = ClayRoutineScript([])

    def apollo(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "credits_used": 1,
                "person": {"email": _EMAIL, "linkedin_url": _PROFILE},
            },
        )

    stub = WireStub(
        {
            "exa": _exa_one,
            "clay": clay,
            "apollo": apollo,
            "instantly": _terminal_instantly,
        }
    )
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _rejected_company(), stub)
    real_coverage = run_live_provider_coverage
    snapshot_checks = 0

    def coverage_with_snapshot(
        data_root: Path,
        *,
        run_id: str,
    ) -> CanaryProviderCoverageSummary:
        nonlocal snapshot_checks
        before = _canonical_and_normal_snapshot(data_root / run_id)
        summary = real_coverage(data_root, run_id=run_id)
        assert _canonical_and_normal_snapshot(data_root / run_id) == before
        snapshot_checks += 1
        return summary

    monkeypatch.setattr(
        production_canary,
        "run_live_provider_coverage",
        coverage_with_snapshot,
    )

    assert _run_canary(tmp_path, run_id) == 2
    clay.release_started()
    assert _run_canary(tmp_path, run_id) == 2
    assert snapshot_checks == 2

    evaluated_rows = read_jsonl(run_dir / "companies_evaluated.jsonl")
    assert len(evaluated_rows) == 1
    evaluated = CompanyRecord.from_dict(evaluated_rows[0])
    expected_selected = select_contacts(evaluated, [_person()], limit=1)
    assert len(expected_selected) == 1
    expected = expected_selected[0]

    assert len(clay.posts) == 1
    clay_item = json_body(clay.posts[0])["items"][0]
    assert clay_item["id"] == expected.contact_id
    assert clay_item["inputs"] == {
        "full_name": expected.full_name,
        "company_name": expected.company_name,
        "company_domain": expected.company_domain,
        "linkedin_url": expected.linkedin_url,
        "profile_url": expected.profile_url,
    }

    apollo_requests = stub.for_provider("apollo")
    assert len(apollo_requests) == 1
    apollo_body = json_body(apollo_requests[0])
    assert {
        "name": apollo_body["name"],
        "domain": apollo_body["domain"],
        "organization_name": apollo_body["organization_name"],
        "linkedin_url": apollo_body["linkedin_url"],
    } == {
        "name": expected.full_name,
        "domain": expected.company_domain,
        "organization_name": expected.company_name,
        "linkedin_url": expected.linkedin_url,
    }

    assert read_jsonl(run_dir / "contacts.jsonl") == []
    assert read_csv(run_dir / "leads.csv") == []
    assert _operations(run_dir / "contact_checkpoint.json") == {}
    assert (run_dir / "contact_usage_events.jsonl").read_text(encoding="utf-8") == ""

    private_operations = _operations(run_dir / "canary_paid_checkpoint.json")
    assert private_operations["coverage:exa_people"]["state"] == "completed"
    assert private_operations["coverage:clay"]["state"] == "completed"
    assert private_operations["coverage:apollo"]["state"] == "completed"
    assert private_operations["coverage:instantly"]["state"] == "completed"


def test_uncertain_company_makes_zero_normal_m4_provider_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An uncertain company stays outside normal M4 while exact-company coverage may run."""
    run_id = "canary-review-uncertain"
    stub = WireStub({"exa": _exa_zero})
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _uncertain_company(), stub)

    assert _run_canary(tmp_path, run_id) == 2

    evaluated_rows = read_jsonl(run_dir / "companies_evaluated.jsonl")
    assert len(evaluated_rows) == 1
    assert evaluated_rows[0]["final_decision"] == "uncertain"

    assert len(stub.for_provider("exa")) == 1
    assert len(stub.for_provider("clay")) == 0
    assert len(stub.for_provider("apollo")) == 0
    assert len(stub.for_provider("instantly")) == 0
    assert _operations(run_dir / "contact_checkpoint.json") == {}
    assert read_jsonl(run_dir / "contacts.jsonl") == []
    assert read_csv(run_dir / "leads.csv") == []

    private_operations = _operations(run_dir / "canary_paid_checkpoint.json")
    assert private_operations["coverage:exa_people"]["state"] == "completed"
