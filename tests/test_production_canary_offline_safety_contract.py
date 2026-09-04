"""Highest-seam offline safety failure contract for the fixed production canary."""

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
    read_jsonl,
    set_m4_credentials,
)

from leads_discovery import production_canary
from leads_discovery.cli import main as cli_main
from leads_discovery.models import CompanyRecord, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.state import append_jsonl, read_json, write_checkpoint

_PROFILE = "https://www.linkedin.com/in/pat-owner"


def _rejected_company() -> CompanyRecord:
    facts = accepted_facts()
    facts["known_current_direct_competitor_customer"] = (True, 0.95)
    return build_company(
        facts=facts,
        company_id="cmp_canary",
        name="Acme Valve",
        domain="acmevalve.com",
    )


def _exa_one(request: httpx.Request) -> httpx.Response:
    payload = json_body(request)
    assert payload["category"] == "people"
    return httpx.Response(
        200,
        json={
            "results": [
                person_result(
                    name="Pat Owner",
                    title="President and Owner",
                    company="Acme Valve",
                    domain="acmevalve.com",
                    profile_url=_PROFILE,
                )
            ],
            "costDollars": {"total": 0.001},
        },
    )


def _seed_authoritative_run(data_root: Path, run_id: str, company: CompanyRecord) -> None:
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


def test_coverage_transport_safety_failure_is_provider_and_pipeline_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded-response safety rejection fails closed and never becomes inconclusive."""
    run_id = "canary-coverage-transport-safety"
    company = _rejected_company()
    clay = ClayRoutineScript([])
    real_cli = cli_main
    seeded = False

    def seeded_cli(argv: Sequence[str] | None = None) -> int:
        nonlocal seeded
        assert argv is not None
        if argv[0] == "run":
            if not seeded:
                _seed_authoritative_run(tmp_path, run_id, company)
                seeded = True
            return 0
        return real_cli(argv)

    def apollo(_request: httpx.Request) -> httpx.Response:
        monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "8")
        return httpx.Response(
            200,
            json={"credits_used": 1, "person": None},
        )

    stub = WireStub({"exa": _exa_one, "clay": clay, "apollo": apollo})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)
    monkeypatch.setattr(production_canary, "cli_main", seeded_cli)

    assert production_canary.main(["--run-id", run_id, "--data-root", str(tmp_path)]) == 2
    clay.release_started()
    assert production_canary.main(["--run-id", run_id, "--data-root", str(tmp_path)]) == 1
    assert len(stub.for_provider("apollo")) == 1
    assert len(stub.for_provider("instantly")) == 0

    run_dir = tmp_path / run_id
    report = read_json(run_dir / "canary_coverage_report.json")
    assert report is not None
    assert report["overall_outcome"] == "failure"
    assert "coverage_paid_outcome_unresolved" in report["safety_flags"]
    providers = report["providers"]
    assert isinstance(providers, list)
    apollo_rows = [
        row for row in providers if isinstance(row, dict) and row.get("provider") == "apollo"
    ]
    assert len(apollo_rows) == 1
    assert apollo_rows[0]["integration_outcome"] == "failure"

    request_count = len(stub.requests)
    assert production_canary.main(["--run-id", run_id, "--data-root", str(tmp_path)]) == 1
    assert len(stub.requests) == request_count
    assert read_jsonl(run_dir / "contacts.jsonl") == []
