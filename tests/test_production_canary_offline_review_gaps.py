"""Review-gap contract for the fixed production canary composition."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from m4_contract_fixtures import ClayRoutineScript, WireStub, json_body, read_jsonl
from test_production_canary_offline_contract import (
    _EMAIL,
    _PROFILE,
    _exa_one,
    _install_contract,
    _person,
    _rejected_company,
    _run_canary,
    _terminal_instantly,
)

from leads_discovery import production_canary
from leads_discovery.contacts.selection import select_contacts
from leads_discovery.models import CompanyRecord
from leads_discovery.pipeline.canary_provider_coverage import (
    CanaryProviderCoverageSummary,
    run_live_provider_coverage,
)

_CANONICAL_AND_NORMAL_ARTIFACTS = (
    "companies_evaluated.jsonl",
    "checkpoint.json",
    "contacts.jsonl",
    "leads.csv",
    "contact_usage_events.jsonl",
    "contact_usage.json",
    "contact_checkpoint.json",
)


def _canonical_and_normal_snapshot(run_dir: Path) -> dict[str, bytes | None]:
    """Snapshot canonical artifacts plus authoritative normal M4 state, including absence."""
    snapshot: dict[str, bytes | None] = {}
    for name in _CANONICAL_AND_NORMAL_ARTIFACTS:
        path = run_dir / name
        snapshot[name] = path.read_bytes() if path.exists() else None
    return snapshot


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
            "instantly": _terminal_instantly("verified", expected_email=_EMAIL),
        }
    )
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _rejected_company(), stub)
    real_coverage = run_live_provider_coverage

    def coverage_with_snapshot(
        data_root: Path,
        *,
        run_id: str,
    ) -> CanaryProviderCoverageSummary:
        before = _canonical_and_normal_snapshot(data_root / run_id)
        summary = real_coverage(data_root, run_id=run_id)
        assert _canonical_and_normal_snapshot(data_root / run_id) == before
        return summary

    monkeypatch.setattr(
        production_canary,
        "run_live_provider_coverage",
        coverage_with_snapshot,
    )

    assert _run_canary(tmp_path, run_id) == 2
    clay.release_started()
    assert _run_canary(tmp_path, run_id) == 2

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
        "Full Name": expected.full_name,
        "Company Domain": expected.company_domain,
        "Company Name": expected.company_name,
        "Social Profile URL": expected.linkedin_url or expected.profile_url,
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
