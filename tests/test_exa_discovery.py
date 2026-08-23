"""Black-box contract tests for the bounded Exa discovery adapter."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import httpx
import pytest

from leads_discovery.discovery.base import DiscoveryProviderError
from leads_discovery.discovery.exa import ExaDiscoveryProvider
from leads_discovery.models import DiscoveryRequest

API_KEY = "exa-secret-contract-key"
QUERY = "Independent PVF distributors in the United States"


def _request(**overrides: Any) -> DiscoveryRequest:
    """Build one valid Exa discovery request with optional field overrides."""
    values: dict[str, Any] = {
        "request_id": "exa:us:core-pvf:v1",
        "provider": "exa",
        "query_family": "core-pvf",
        "target_country_code": "US",
        "queries": (QUERY,),
        "max_results_per_query": 2,
        "max_results_total": 2,
        "max_cost_usd": None,
    }
    values.update(overrides)
    return DiscoveryRequest(**values)


def _success_payload() -> dict[str, Any]:
    """Return a representative company-vertical response with extra raw fields."""
    return {
        "results": [
            {
                "id": "result-1",
                "url": "https://acme.example/about",
                "title": "Acme fallback title",
                "highlights": ["First highlight", "Second highlight"],
                "entities": [
                    {
                        "id": "entity-1",
                        "type": "company",
                        "name": "Acme Valve",
                        "url": "https://www.acmevalve.com",
                        "headquarters": {
                            "city": "Tulsa",
                            "postalCode": "74101",
                            "country": "United States",
                        },
                    }
                ],
                "employeeCount": 123,
                "opaqueFutureField": {"preserve": [1, 2, 3]},
            },
            {
                "id": "result-2",
                "url": "https://bravo.example",
                "title": "Bravo Industrial",
                "highlights": [],
                "entities": [],
                "score": 0.91,
            },
            {
                "id": "result-3",
                "url": "https://overflow.example",
                "title": "Must be capped",
                "highlights": ["overflow"],
                "entities": [],
            },
        ],
        "costDollars": {"total": 0.0125},
    }


def test_exact_exa_wire_request_mapping_raw_preservation_and_usage() -> None:
    """Exa sends only the frozen company-search payload and maps bounded output."""
    seen: list[httpx.Request] = []
    payload = _success_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        batch = ExaDiscoveryProvider(api_key=API_KEY, client=client).search(_request())

    assert len(seen) == 1
    sent = seen[0]
    assert sent.method == "POST"
    assert str(sent.url) == "https://api.exa.ai/search"
    assert sent.headers["x-api-key"] == API_KEY
    assert json.loads(sent.content) == {
        "query": QUERY,
        "category": "company",
        "type": "auto",
        "numResults": 2,
        "userLocation": "US",
        "contents": {"highlights": True},
    }

    assert batch.request == _request()
    assert len(batch.records) == 2
    first, second = batch.records
    assert first.provider == "exa"
    assert first.request_id == "exa:us:core-pvf:v1"
    assert first.target_country_code == "US"
    assert first.query == QUERY
    assert first.provider_result_id in {"entity-1", "result-1"}
    assert first.name == "Acme Valve"
    assert first.source_url == "https://acme.example/about"
    assert first.website_url == "https://acme.example/about"
    assert first.city == "Tulsa"
    assert first.region is None
    assert first.postal_code == "74101"
    assert first.country_code == "United States"
    assert first.raw_metadata == payload["results"][0]
    assert second.raw_metadata == payload["results"][1]
    assert second.name == "Bravo Industrial"
    assert second.city is None
    assert second.region is None
    assert second.postal_code is None
    assert second.country_code is None

    assert first.retrieved_at == second.retrieved_at
    parsed_timestamp = datetime.fromisoformat(first.retrieved_at)
    assert parsed_timestamp.tzinfo is not None
    assert parsed_timestamp.utcoffset() is not None
    assert parsed_timestamp.utcoffset().total_seconds() == 0
    assert re.fullmatch(r"raw_[0-9a-f]{24}", first.record_id)
    assert re.fullmatch(r"raw_[0-9a-f]{24}", second.record_id)
    assert first.record_id != second.record_id

    assert len(batch.usage_events) == 1
    usage = batch.usage_events[0]
    assert usage.provider == "exa"
    assert usage.operation == "company_search"
    assert usage.request_count == 1
    assert usage.estimated_cost_usd == pytest.approx(0.0125)
    assert usage.exact_cost_usd is None
    serialized_usage = json.dumps(usage.to_dict(), sort_keys=True)
    assert API_KEY not in serialized_usage
    assert "x-api-key" not in serialized_usage.lower()


def test_exa_uses_first_company_entity_and_title_fallback_only_when_needed() -> None:
    """Non-company entities are skipped and company name wins over result title."""
    response = {
        "results": [
            {
                "id": "result-1",
                "url": "https://acme.example/about",
                "title": "Result title",
                "entities": [
                    {"id": "person-1", "type": "person", "name": "Wrong Person"},
                    {
                        "id": "company-1",
                        "type": "company",
                        "name": "Right Company",
                        "url": "https://right-company.com",
                    },
                ],
            }
        ]
    }

    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response))
    ) as client:
        batch = ExaDiscoveryProvider(api_key=API_KEY, client=client).search(
            _request(max_results_total=1, max_results_per_query=1)
        )

    assert batch.records[0].provider_result_id in {"company-1", "result-1"}
    assert batch.records[0].name == "Right Company"


def test_exa_record_ids_are_stable_across_retrieval_times() -> None:
    """Retrieval time is excluded from the deterministic raw-record identity."""
    response = {
        "results": [{"id": "stable-id", "url": "https://stable.example", "title": "Stable"}]
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ExaDiscoveryProvider(api_key=API_KEY, client=client)
        first = provider.search(_request(max_results_total=1, max_results_per_query=1))
        second = provider.search(_request(max_results_total=1, max_results_per_query=1))

    assert first.records[0].record_id == second.records[0].record_id


def test_exa_highlights_are_ordered_and_capped_at_two_thousand_characters() -> None:
    """Discovery snippets preserve highlight order but never exceed 2,000 characters."""
    response = {
        "results": [
            {
                "id": "long",
                "url": "https://long.example",
                "title": "Long",
                "highlights": ["A" * 1500, "B" * 1500],
            }
        ]
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        batch = ExaDiscoveryProvider(api_key=API_KEY, client=client).search(
            _request(max_results_total=1, max_results_per_query=1)
        )

    snippet = batch.records[0].snippet
    assert snippet is not None
    assert len(snippet) <= 2000
    assert snippet.startswith("A" * 100)


def test_empty_valid_exa_result_list_is_successful() -> None:
    """An empty valid result list is a successful, zero-row discovery batch."""
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"results": []}))
    ) as client:
        batch = ExaDiscoveryProvider(api_key=API_KEY, client=client).search(_request())

    assert batch.records == []
    assert len(batch.usage_events) == 1
    assert batch.usage_events[0].request_count == 1


@pytest.mark.parametrize(
    ("status_code", "kind", "retryable"),
    [
        (401, "authentication", False),
        (403, "authentication", False),
        (402, "budget_exhausted", False),
        (400, "invalid_request", False),
        (422, "invalid_request", False),
        (408, "rate_limited", True),
        (429, "rate_limited", True),
        (500, "transient", True),
        (503, "transient", True),
        (404, "permanent", False),
    ],
)
def test_exa_status_classification_and_safe_usage(
    status_code: int,
    kind: str,
    retryable: bool,
) -> None:
    """HTTP failures use the shared classification table and count the attempted call."""
    secret_body = f"provider leaked {API_KEY}"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=secret_body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ExaDiscoveryProvider(api_key=API_KEY, client=client)
        with pytest.raises(DiscoveryProviderError) as caught:
            provider.search(_request())

    error = caught.value
    assert error.provider == "exa"
    assert error.kind == kind
    assert error.retryable is retryable
    assert error.status_code == status_code
    assert error.usage_event.request_count == 1
    assert API_KEY not in str(error)
    assert secret_body not in str(error)
    assert API_KEY not in json.dumps(error.usage_event.to_dict(), sort_keys=True)


def test_negative_authenticated_exa_cost_is_invalid_response() -> None:
    """Authenticated provider cost must be nonnegative when supplied."""
    response = {"results": [], "costDollars": {"total": -0.01}}
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response))
    ) as client:
        provider = ExaDiscoveryProvider(api_key=API_KEY, client=client)
        with pytest.raises(DiscoveryProviderError) as caught:
            provider.search(_request())

    assert caught.value.kind == "invalid_response"
    assert caught.value.usage_event.request_count == 1


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_authenticated_exa_cost_is_invalid_response(cost: float) -> None:
    """PRV-04/INV-04 reject non-finite authenticated spend before it reaches budget state."""
    response = {"results": [], "costDollars": {"total": cost}}
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response))
    ) as client:
        provider = ExaDiscoveryProvider(api_key=API_KEY, client=client)
        with pytest.raises(DiscoveryProviderError) as caught:
            provider.search(_request())

    assert caught.value.kind == "invalid_response"
    assert caught.value.usage_event.request_count == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"{"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"results": {"not": "a list"}}),
        httpx.Response(200, json={"results": ["not an object"]}),
    ],
)
def test_malformed_exa_responses_are_invalid_response(response: httpx.Response) -> None:
    """Malformed required envelopes fail closed without fabricating rows."""
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        provider = ExaDiscoveryProvider(api_key=API_KEY, client=client)
        with pytest.raises(DiscoveryProviderError) as caught:
            provider.search(_request())

    assert caught.value.kind == "invalid_response"
    assert caught.value.retryable is False
    assert caught.value.usage_event.request_count == 1


def test_transport_failure_is_retryable_transient_and_sanitized() -> None:
    """Transport errors never expose unsafe transport text or credentials."""
    unsafe = f"connect failed with token={API_KEY}"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(unsafe, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ExaDiscoveryProvider(api_key=API_KEY, client=client)
        with pytest.raises(DiscoveryProviderError) as caught:
            provider.search(_request())

    error = caught.value
    assert error.kind == "transient"
    assert error.retryable is True
    assert error.status_code is None
    assert error.usage_event.request_count == 1
    assert API_KEY not in str(error)
    assert unsafe not in str(error)
    assert error.__cause__ is None


@pytest.mark.parametrize(
    "request",
    [
        _request(provider="apify"),
        _request(target_country_code="MX"),
        _request(queries=("",)),
        _request(queries=(QUERY, "second")),
        _request(max_results_total=0),
        _request(max_results_total=101),
    ],
)
def test_invalid_exa_requests_fail_before_http(request: DiscoveryRequest) -> None:
    """Wrong provider/geography/cardinality/query/total requests make zero HTTP attempts."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"results": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ExaDiscoveryProvider(api_key=API_KEY, client=client)
        with pytest.raises((DiscoveryProviderError, TypeError, ValueError)):
            provider.search(request)

    assert calls == 0


def test_blank_exa_credential_is_rejected_without_http() -> None:
    """Adapters require a nonempty credential and never source one from the environment."""
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    with (
        httpx.Client(transport=transport) as client,
        pytest.raises((TypeError, ValueError)),
    ):
        ExaDiscoveryProvider(api_key="", client=client)
