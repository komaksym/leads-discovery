"""End-to-end offline contract for the normal batch M1-M4 production path."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

import leads_discovery.discovery as discovery_module
import leads_discovery.research as research_module
from leads_discovery import production_canary
from leads_discovery.cli import main as cli_main
from leads_discovery.models import (
    CompanyRecord,
    DiscoveryBatch,
    DiscoveryRecord,
    DiscoveryRequest,
    EvidenceBundle,
    EvidenceItem,
    ExtractedFact,
    ExtractionResult,
    UsageEvent,
)
from leads_discovery.research.extract import FACT_KEYS
from m4_contract_fixtures import (
    ClayRoutineScript,
    WireStub,
    call_enrich_live,
    install_mock_http,
    json_body,
    person_result,
    read_csv,
    read_jsonl,
    set_m4_credentials,
)

_COMPANIES = (
    ("cmp_alpha", "Alpha Valve", "alpha-valve.com"),
    ("cmp_beta", "Beta Valve", "beta-valve.com"),
    ("cmp_gamma", "Gamma Valve", "gamma-valve.com"),
    ("cmp_zz_delta", "Delta Valve", "delta-valve.com"),
)
_SUPPORTED_FACTS: dict[str, bool | int] = {
    "pvf_relevant": True,
    "rfq_or_quote_workflow_evidence": True,
    "inside_sales_or_estimating_presence": True,
    "project_or_tender_business": True,
    "bom_or_line_item_complexity": True,
    "manufacturer_count_or_breadth": 20,
    "employee_count": 50,
    "branch_count": 3,
    "multi_location_signal": True,
    "known_current_direct_competitor_customer": False,
    "known_quote_automation_or_order_automation_relationship": False,
}
_SUPPORT_EXCERPTS = {
    "pvf_relevant": "We distribute industrial pipe, valves, and fittings.",
    "rfq_or_quote_workflow_evidence": "Our RFQ quotation workflow handles customer quotes.",
    "inside_sales_or_estimating_presence": "Our inside sales and estimating team prepares quotes.",
    "project_or_tender_business": "We bid on industrial projects and tenders.",
    "bom_or_line_item_complexity": "Our estimators process BOM bill of materials line items.",
    "manufacturer_count_or_breadth": "20 manufacturers supply our line card brands.",
    "employee_count": "50 employees work across the company.",
    "branch_count": "3 branches serve regional customers.",
    "multi_location_signal": "We operate multiple locations across the region.",
    "known_current_direct_competitor_customer": "No competitor customer relationship exists.",
    "known_quote_automation_or_order_automation_relationship": (
        "No quote automation relationship exists."
    ),
    "unsupported_direct_pain": "Our warehouse is open on weekdays.",
}
_DISCOVERY_REQUESTS: list[DiscoveryRequest] = []


class _ConstructorBomb:
    """Fail if a dry batch accidentally constructs any live provider."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("dry batch must not construct live providers")


class _BatchDiscovery:
    """Return four companies on every bounded Exa request so dedup must merge provenance."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls = 0

    def search(self, request: DiscoveryRequest) -> DiscoveryBatch:
        """Return duplicate company identities with request-specific provenance."""
        self.calls += 1
        _DISCOVERY_REQUESTS.append(request)
        records = [
            DiscoveryRecord(
                record_id=f"raw_{self.calls * 10 + index:024x}",
                provider="exa",
                request_id=request.request_id,
                target_country_code=request.target_country_code,
                query=request.queries[0] if request.queries else None,
                provider_result_id=f"{company_id}:{self.calls}",
                name=name,
                source_url=f"https://{domain}/source/{self.calls}",
                website_url=f"https://{domain}",
                city="Houston",
                region="TX",
                postal_code="77001",
                country_code="US",
                title=name,
                snippet="Industrial PVF distributor with an RFQ workflow.",
                raw_metadata={"request": request.request_id},
                retrieved_at="2026-09-03T10:00:00+00:00",
            )
            for index, (company_id, name, domain) in enumerate(_COMPANIES, start=1)
        ]
        return DiscoveryBatch(
            request=request,
            records=records,
            usage_events=[
                UsageEvent(
                    provider="exa",
                    operation="company_search",
                    request_count=1,
                    estimated_cost_usd=0.0,
                )
            ],
        )


class _BatchResearcher:
    """Return one twelve-item bounded evidence bundle for each selected company."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def research(self, company: CompanyRecord) -> EvidenceBundle:
        """Return proposition-specific evidence plus one intentionally unsupported citation."""
        items = [
            EvidenceItem(
                evidence_id=f"ev_{company.company_id}_{index:02d}",
                url=f"https://proof-{index}.com/{company.company_id}",
                title=key,
                excerpt=excerpt,
                source_type="web",
                provider="exa",
                retrieved_at="2026-09-03T10:05:00+00:00",
            )
            for index, (key, excerpt) in enumerate(_SUPPORT_EXCERPTS.items(), start=1)
        ]
        return EvidenceBundle(
            company_id=company.company_id,
            items=items,
            raw_records=[
                {"company_id": company.company_id, "evidence_id": item.evidence_id}
                for item in items
            ],
            usage_events=[
                UsageEvent(
                    provider="exa",
                    operation="company_research",
                    request_count=3,
                    estimated_cost_usd=0.0,
                )
            ],
        )


class _BatchExtractor:
    """Propose supported facts for two companies and sparse facts for the third."""

    def __init__(self, *_args: Any, **kwargs: Any) -> None:
        self.model = str(kwargs.get("model", "deepseek-v4-flash"))

    def reservation_cost_usd(
        self, _company: CompanyRecord, _bundle: EvidenceBundle
    ) -> float:
        """Keep the offline contract at zero synthetic spend."""
        return 0.0

    def extract(
        self, company: CompanyRecord, bundle: EvidenceBundle
    ) -> ExtractionResult:
        """Return candidate facts; production evidence support canonicalizes them."""
        evidence_by_title = {
            item.title: item.evidence_id for item in bundle.items if item.title is not None
        }
        facts = {key: ExtractedFact(None, 0.0, []) for key in FACT_KEYS}
        keys: Sequence[str]
        if company.company_id == "cmp_gamma":
            keys = ("pvf_relevant",)
        else:
            keys = tuple(_SUPPORTED_FACTS)
        for key in keys:
            facts[key] = ExtractedFact(
                _SUPPORTED_FACTS[key],
                0.9,
                [evidence_by_title[key]],
            )
        if company.company_id != "cmp_gamma":
            facts["direct_quotation_pain_evidence"] = ExtractedFact(
                True,
                0.9,
                [evidence_by_title["unsupported_direct_pain"]],
            )
        return ExtractionResult(
            company_id=company.company_id,
            model=self.model,
            facts=facts,
            usage_event=UsageEvent(
                provider="deepseek",
                operation="structured_extraction",
                request_count=1,
                estimated_cost_usd=0.0,
            ),
        )


def _verified(request: httpx.Request) -> httpx.Response:
    """Return one terminal verified email result."""
    body = json_body(request) if request.method == "POST" else {}
    email = str(body.get("email") or request.url.path.rsplit("/", 1)[-1])
    return httpx.Response(
        200,
        json={
            "email": email,
            "status": "completed",
            "verification_status": "verified",
            "credits_used": 1,
        },
    )


def _people(request: httpx.Request) -> httpx.Response:
    """Return one current owner for each accepted company and reject any other company."""
    body = json_body(request)
    query = body.get("query")
    assert isinstance(query, str)
    if "Alpha Valve" in query:
        person = person_result(
            name="Alice Owner",
            title="President and Owner",
            company="Alpha Valve",
            domain="alpha-valve.com",
            profile_url="https://www.linkedin.com/in/alice-owner",
        )
    elif "Beta Valve" in query:
        person = person_result(
            name="Bob Owner",
            title="President and Owner",
            company="Beta Valve",
            domain="beta-valve.com",
            profile_url="https://www.linkedin.com/in/bob-owner",
        )
    else:
        raise AssertionError(f"M4 searched a non-accepted company: {query}")
    return httpx.Response(
        200,
        json={"results": [person], "costDollars": {"total": 0.0}},
    )


def test_batch_limits_do_not_authorize_live_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Batch cardinality and prospecting criteria remain inert without execute-live."""
    monkeypatch.setattr(discovery_module, "ExaDiscoveryProvider", _ConstructorBomb)
    monkeypatch.setattr(research_module, "ExaEvidenceResearcher", _ConstructorBomb)
    monkeypatch.setattr(research_module, "DeepSeekExtractor", _ConstructorBomb)

    code = cli_main(
        [
            "run",
            "--run-id",
            "batch-dry",
            "--data-root",
            str(tmp_path),
            "--market",
            "industrial pumps",
            "--search-term",
            "regional distributors",
            "--search-term",
            "RFQ workflow",
            "--target-geography",
            "US",
            "--max-candidates",
            "4",
            "--max-evaluated",
            "3",
            "--deepseek-budget-usd",
            "1",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "dry_run"
    assert payload["max_candidates"] == 4
    assert payload["max_evaluated"] == 3
    assert payload["search_terms"] == ["regional distributors", "RFQ workflow"]
    assert payload["target_geographies"] == ["US"]
    assert not (tmp_path / "batch-dry").exists()


def test_normal_batch_processes_multiple_companies_through_m1_m4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One normal run keeps four discovered candidates, evaluates three, and enriches two."""
    _DISCOVERY_REQUESTS.clear()
    monkeypatch.setattr(discovery_module, "ExaDiscoveryProvider", _BatchDiscovery)
    monkeypatch.setattr(research_module, "ExaEvidenceResearcher", _BatchResearcher)
    monkeypatch.setattr(research_module, "DeepSeekExtractor", _BatchExtractor)
    monkeypatch.setenv("EXA_API_KEY", "offline-exa")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-deepseek")

    run_id = "batch-contract"
    code = cli_main(
        [
            "run",
            "--run-id",
            run_id,
            "--data-root",
            str(tmp_path),
            "--market",
            "industrial pumps",
            "--search-term",
            "regional distributors",
            "--search-term",
            "RFQ workflow",
            "--target-geography",
            "US",
            "--max-candidates",
            "4",
            "--max-evaluated",
            "3",
            "--deepseek-budget-usd",
            "1",
            "--execute-live",
        ]
    )
    run_payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert run_payload["evaluation"]["evaluated_count"] == 3
    assert run_payload["evaluation"]["accepted_count"] == 2
    assert run_payload["evaluation"]["uncertain_count"] == 1

    assert _DISCOVERY_REQUESTS
    assert {request.target_country_code for request in _DISCOVERY_REQUESTS} == {"US"}
    search_text = " ".join(
        query for request in _DISCOVERY_REQUESTS for query in request.queries
    ).casefold()
    assert "regional distributors" in search_text
    assert "rfq workflow" in search_text

    run_dir = tmp_path / run_id
    assert len(read_jsonl(run_dir / "companies_deduped.jsonl")) == 4
    evaluated = read_jsonl(run_dir / "companies_evaluated.jsonl")
    assert len(evaluated) == 3
    by_id = {str(row["company_id"]): row for row in evaluated}
    assert set(by_id) == {"cmp_alpha", "cmp_beta", "cmp_gamma"}
    assert by_id["cmp_alpha"]["final_decision"] == "accepted"
    assert by_id["cmp_beta"]["final_decision"] == "accepted"
    assert by_id["cmp_gamma"]["final_decision"] == "uncertain"

    for row in evaluated:
        assert row["discovery_sources"] == ["exa"]
        assert row["discovery_queries"]
        assert len(row["discovery_records"]) >= 1
        evidence = row["evidence"]
        assert isinstance(evidence, list)
        assert 1 <= len(evidence) <= 12
        assert sum(len(str(item.get("excerpt") or "")) for item in evidence) <= 20_000

    assert by_id["cmp_alpha"]["features"]["direct_quotation_pain_evidence"] is None
    assert by_id["cmp_beta"]["features"]["direct_quotation_pain_evidence"] is None
    assert (run_dir / "research_raw.jsonl").exists()

    clay = ClayRoutineScript(
        [
            {"work_email": "alice.owner@alpha-valve.com"},
            {"work_email": "bob.owner@beta-valve.com"},
        ]
    )
    stub = WireStub({"exa": _people, "clay": clay, "instantly": _verified})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    enrich_code = call_enrich_live(tmp_path, run_id)
    for _ in range(6):
        if enrich_code == 0:
            break
        clay.release_started()
        enrich_code = call_enrich_live(tmp_path, run_id)
    assert enrich_code == 0

    contacts = read_jsonl(run_dir / "contacts.jsonl")
    assert {row["company_id"] for row in contacts} == {"cmp_alpha", "cmp_beta"}
    assert all(row["email_verification_status"] == "verified" for row in contacts)
    assert len(read_csv(run_dir / "leads.csv")) == 2
    assert len(stub.for_provider("exa")) == 2
    assert stub.for_provider("apollo") == []

    request_count = len(stub.requests)
    assert call_enrich_live(tmp_path, run_id) == 0
    assert len(stub.requests) == request_count
    assert set(tmp_path.iterdir()) == {run_dir}


def test_one_company_canary_rejects_normal_batch_controls() -> None:
    """The credential canary cannot be reused as a configurable batch entry point."""
    with pytest.raises(SystemExit):
        production_canary.main(
            ["--run-id", "canary-fixed", "--max-candidates", "2"]
        )
