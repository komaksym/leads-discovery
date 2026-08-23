"""Contract tests for research selection, Exa research, and bounded evidence bundles."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import httpx
import pytest

from leads_discovery.models import CompanyRecord, EvidenceBundle, EvidenceItem, UsageEvent
from leads_discovery.research.evidence import (
    ExaEvidenceResearcher,
    build_evidence_bundle,
    build_research_requests,
    select_research_companies,
)

API_KEY = "exa-research-secret"


def _company(
    company_id: str,
    *,
    name: str | None = None,
    domain: str | None = "example.com",
    country: str | None = "US",
    sources: list[str] | None = None,
    record_count: int = 1,
    review_reasons: list[str] | None = None,
) -> CompanyRecord:
    """Build a canonical company with deterministic discovery provenance."""
    company_name = name or company_id.replace("cmp_", "Company ")
    return CompanyRecord(
        company_id=company_id,
        name=company_name,
        normalized_name=company_name.casefold(),
        domain=domain,
        normalized_domain=domain,
        country=country,
        discovery_sources=["exa"] if sources is None else list(sources),
        discovery_records=[
            {"record_id": f"raw_{company_id}_{index}"} for index in range(record_count)
        ],
        review_reasons=[] if review_reasons is None else list(review_reasons),
        stage_status={"deduplication": "completed"},
        created_at="2026-08-23T09:00:00+00:00",
        updated_at="2026-08-23T09:00:00+00:00",
    )


def _evidence(
    index: int,
    *,
    url: str | None = None,
    excerpt: str | None = None,
) -> EvidenceItem:
    """Build a deterministic evidence item for bundle-boundary tests."""
    final_url = url or f"https://source{index}.com/page/{index}"
    return EvidenceItem(
        evidence_id=f"ev_{index:024x}",
        url=final_url,
        title=f"Title {index}",
        excerpt=f"Excerpt {index}" if excerpt is None else excerpt,
        source_type="web",
        provider="exa",
        retrieved_at="2026-08-23T12:00:00+00:00",
    )


def test_selection_priority_is_deterministic_and_capped_at_twenty() -> None:
    """Selection follows domain/country/provider-count/record-count/company-ID priority."""
    companies = [
        _company("cmp_06", domain=None, country="US", sources=["exa", "apify"], record_count=10),
        _company(
            "cmp_05", domain="five.com", country=None, sources=["exa", "apify"], record_count=9
        ),
        _company("cmp_04", domain="four.com", country="CA", sources=["exa"], record_count=9),
        _company(
            "cmp_03", domain="three.com", country="US", sources=["exa", "apify"], record_count=1
        ),
        _company(
            "cmp_02", domain="two.com", country="US", sources=["exa", "apify"], record_count=3
        ),
        _company(
            "cmp_01", domain="one.com", country="US", sources=["exa", "apify"], record_count=3
        ),
        _company(
            "cmp_out",
            domain="outside.com",
            country="MX",
            sources=["exa", "apify"],
            record_count=99,
        ),
    ]

    selected = select_research_companies(companies, limit=20)

    assert [company.company_id for company in selected] == [
        "cmp_01",
        "cmp_02",
        "cmp_03",
        "cmp_04",
        "cmp_05",
        "cmp_06",
    ]
    assert all(company.company_id != "cmp_out" for company in selected)
    assert all(company.final_decision is None for company in companies)
    assert all(company.status == "active" for company in companies)

    many = [_company(f"cmp_{index:02d}", domain=f"d{index}.com") for index in range(25)]
    assert len(select_research_companies(many)) == 20


@pytest.mark.parametrize("limit", [0, 21])
def test_selection_rejects_limits_outside_one_to_twenty(limit: int) -> None:
    """Paid research selection limits outside 1..20 are invalid."""
    with pytest.raises((TypeError, ValueError)):
        select_research_companies([_company("cmp_1")], limit=limit)


def test_exact_three_query_research_catalog_and_whitespace_normalization() -> None:
    """Each company gets exactly the three frozen Exa research queries in order."""
    company = _company("cmp_acme", name="  Acme Valve  ", domain="acme.com")
    requests = build_research_requests(company)

    assert [request.query_family for request in requests] == [
        "company-profile",
        "quotation-workload",
        "economic-incumbent-pain",
    ]
    assert [request.query for request in requests] == [
        '"Acme Valve" acme.com pipe valves fittings products industries locations line card',
        '"Acme Valve" acme.com RFQ quote quotation BOM estimating project tender inside sales',
        (
            '"Acme Valve" acme.com employees branches revenue automation ERP ecommerce quote '
            "software competitor manual workflow"
        ),
    ]
    assert all(request.company_id == "cmp_acme" for request in requests)
    assert all(request.max_results == 5 for request in requests)
    assert len({request.request_id for request in requests}) == 3

    domainless = build_research_requests(_company("cmp_none", name="Acme Valve", domain=None))
    assert all("  " not in request.query for request in domainless)
    assert domainless[0].query == (
        '"Acme Valve" pipe valves fittings products industries locations line card'
    )


def test_exa_research_exact_payload_call_order_raw_preservation_ids_and_usage() -> None:
    """Research makes three bounded searches and preserves full provider rows separately."""
    seen: list[httpx.Request] = []
    call_index = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_index
        seen.append(request)
        index = call_index
        call_index += 1
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": f"result-{index}",
                        "url": f"https://source{index}.com/page",
                        "title": f"Source {index}",
                        "highlights": [f"highlight-{index}-a", f"highlight-{index}-b"],
                        "opaque": {"full": [index, "preserve"]},
                    }
                ],
                "costDollars": {"total": 0.001 * (index + 1)},
            },
        )

    company = _company("cmp_acme", name="Acme Valve", domain="acme.com")
    expected_requests = build_research_requests(company)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bundle = ExaEvidenceResearcher(api_key=API_KEY, client=client).research(company)

    assert len(seen) == 3
    for sent, expected in zip(seen, expected_requests, strict=True):
        assert sent.method == "POST"
        assert str(sent.url) == "https://api.exa.ai/search"
        assert sent.headers["x-api-key"] == API_KEY
        assert json.loads(sent.content) == {
            "query": expected.query,
            "type": "auto",
            "numResults": 5,
            "contents": {"highlights": True},
        }

    assert bundle.company_id == "cmp_acme"
    assert [item.url for item in bundle.items] == [
        "https://source0.com/page",
        "https://source1.com/page",
        "https://source2.com/page",
    ]
    assert [item.excerpt for item in bundle.items] == [
        "highlight-0-a\nhighlight-0-b",
        "highlight-1-a\nhighlight-1-b",
        "highlight-2-a\nhighlight-2-b",
    ]
    assert all(re.fullmatch(r"ev_[0-9a-f]{24}", item.evidence_id) for item in bundle.items)
    assert all(item.source_type == "web" for item in bundle.items)
    assert all(item.provider == "exa" for item in bundle.items)
    assert len({item.retrieved_at for item in bundle.items}) == 1
    retrieved = datetime.fromisoformat(bundle.items[0].retrieved_at)
    assert retrieved.tzinfo is not None
    offset = retrieved.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0
    assert len(bundle.raw_records) == 3
    assert bundle.raw_records[0]["opaque"] == {"full": [0, "preserve"]}
    assert bundle.raw_records[1]["id"] == "result-1"
    assert bundle.raw_records[2]["id"] == "result-2"

    assert len(bundle.usage_events) == 1
    usage = bundle.usage_events[0]
    assert usage.provider == "exa"
    assert usage.operation == "company_research"
    assert usage.request_count == 3
    assert usage.estimated_cost_usd == pytest.approx(0.006)
    assert usage.exact_cost_usd is None
    assert API_KEY not in json.dumps(usage.to_dict(), sort_keys=True)


def test_research_progress_callback_precedes_next_http_request_and_reports_deltas() -> None:
    """Each successful Exa response is exposed as one durable delta before the next call."""
    progress: list[EvidenceBundle] = []
    call_index = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_index
        assert len(progress) == call_index
        index = call_index
        call_index += 1
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": f"progress-{index}",
                        "url": f"https://progress{index}.com/page",
                        "title": f"Progress {index}",
                        "highlights": [f"excerpt-{index}"],
                    }
                ],
                "costDollars": {"total": 0.001 * (index + 1)},
            },
        )

    company = _company("cmp_progress", name="Progress Co", domain="progress.com")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bundle = ExaEvidenceResearcher(api_key=API_KEY, client=client).research(
            company,
            on_progress=progress.append,
        )

    assert call_index == 3
    assert len(progress) == 3
    assert [len(delta.raw_records) for delta in progress] == [1, 1, 1]
    assert [delta.raw_records[0]["id"] for delta in progress] == [
        "progress-0",
        "progress-1",
        "progress-2",
    ]
    assert [delta.usage_events[0].request_count for delta in progress] == [1, 1, 1]
    assert [delta.usage_events[0].estimated_cost_usd for delta in progress] == pytest.approx(
        [0.001, 0.002, 0.003]
    )
    assert bundle.usage_events[0].request_count == 3
    assert bundle.usage_events[0].estimated_cost_usd == pytest.approx(0.006)


def test_evidence_ids_are_stable_across_research_timestamps() -> None:
    """Evidence identity depends on provider, normalized URL, and excerpt, not retrieval time."""
    response = {
        "results": [
            {
                "id": "same",
                "url": "https://example.com/evidence",
                "title": "Same evidence",
                "highlights": ["same excerpt"],
            }
        ]
    }

    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response))
    ) as client:
        researcher = ExaEvidenceResearcher(api_key=API_KEY, client=client)
        company = _company("cmp_acme", name="Acme Valve", domain="acme.com")
        first = researcher.research(company)
        second = researcher.research(company)

    assert first.items[0].evidence_id == second.items[0].evidence_id


def test_missing_highlights_produces_unknown_excerpt_not_invented_text() -> None:
    """A result without highlights remains valid evidence with excerpt=None."""

    def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": f"https://example.com/{abs(hash(query))}", "title": "No text"}
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bundle = ExaEvidenceResearcher(api_key=API_KEY, client=client).research(
            _company("cmp_acme", name="Acme Valve", domain="acme.com")
        )

    assert bundle.items
    assert all(item.excerpt is None for item in bundle.items)


def test_bundle_url_dedup_domain_limits_item_limit_and_own_domain_exception() -> None:
    """The pure bundle builder keeps first URLs with two-per-domain/four-own-domain bounds."""
    company = _company("cmp_acme", name="Acme", domain="acme.com")
    items = [
        _evidence(1, url="https://acme.com/a"),
        _evidence(2, url="https://acme.com/b"),
        _evidence(3, url="https://acme.com/c"),
        _evidence(4, url="https://acme.com/d"),
        _evidence(5, url="https://acme.com/e"),
        _evidence(6, url="https://other.com/a"),
        _evidence(7, url="https://other.com/b"),
        _evidence(8, url="https://other.com/c"),
        _evidence(9, url="https://unique9.com/a"),
        _evidence(10, url="https://unique10.com/a"),
        _evidence(11, url="https://unique11.com/a"),
        _evidence(12, url="https://unique12.com/a"),
        _evidence(13, url="https://unique13.com/a"),
        _evidence(14, url="https://unique14.com/a"),
        _evidence(15, url="https://unique15.com/a"),
        _evidence(16, url="https://unique16.com/a"),
        _evidence(17, url="https://unique17.com/a"),
        _evidence(18, url="https://unique18.com/a"),
        _evidence(19, url="https://unique19.com/a"),
        _evidence(20, url="https://unique20.com/a"),
        _evidence(21, url="https://unique20.com/a"),
    ]

    bundle = build_evidence_bundle(
        company=company,
        items=items,
        raw_records=[],
        usage_events=[],
    )
    urls = [item.url for item in bundle.items]

    assert urls[:4] == [
        "https://acme.com/a",
        "https://acme.com/b",
        "https://acme.com/c",
        "https://acme.com/d",
    ]
    assert "https://acme.com/e" not in urls
    assert urls.count("https://other.com/a") == 1
    assert "https://other.com/c" not in urls
    assert len(bundle.items) <= 12
    assert len(urls) == len(set(urls))


def test_bundle_caps_each_excerpt_and_total_excerpt_characters() -> None:
    """Model input is bounded to 2,000 characters per item and 20,000 overall."""
    company = _company("cmp_acme", domain="acme.com")
    items = [
        _evidence(index, url=f"https://d{index}.com/x", excerpt=str(index) * 2500)
        for index in range(1, 20)
    ]

    bundle = build_evidence_bundle(
        company=company,
        items=items,
        raw_records=[],
        usage_events=[],
    )
    excerpts = [item.excerpt or "" for item in bundle.items]

    assert len(bundle.items) <= 12
    assert all(len(excerpt) <= 2000 for excerpt in excerpts)
    assert sum(len(excerpt) for excerpt in excerpts) <= 20_000


def test_bundle_preserves_full_raw_rows_and_defensively_copies_inputs() -> None:
    """Bounded prompt items never destroy full research rows or retain caller-owned mutables."""
    company = _company("cmp_acme")
    raw_records: list[dict[str, Any]] = [
        {"id": "raw-1", "nested": {"values": [1, 2]}}
    ]
    usage = UsageEvent(provider="exa", operation="company_research", metadata={"nested": [1]})
    items = [_evidence(1)]

    bundle = build_evidence_bundle(
        company=company,
        items=items,
        raw_records=raw_records,
        usage_events=[usage],
    )
    raw_records[0]["nested"]["values"].append(3)
    usage.metadata["nested"].append(2)

    assert bundle.raw_records == [{"id": "raw-1", "nested": {"values": [1, 2]}}]
    assert bundle.usage_events[0].metadata == {"nested": [1]}


def test_empty_evidence_is_successful_and_never_fabricated() -> None:
    """Three valid empty searches produce a successful empty evidence bundle."""
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"results": []}))
    ) as client:
        bundle = ExaEvidenceResearcher(api_key=API_KEY, client=client).research(
            _company("cmp_empty", name="Empty Co", domain="empty.example")
        )

    assert bundle.items == []
    assert bundle.raw_records == []
    assert len(bundle.usage_events) == 1
    assert bundle.usage_events[0].request_count == 3
