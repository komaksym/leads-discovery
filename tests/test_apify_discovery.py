"""Black-box contract tests for the single-run Apify discovery adapter."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import httpx
import pytest

from leads_discovery.discovery.apify import ApifyDiscoveryProvider
from leads_discovery.discovery.base import DiscoveryProviderError
from leads_discovery.models import DiscoveryRequest

TOKEN = "apify-secret-contract-token"
TERMS = (
    "pipe valve fitting supplier",
    "industrial valve supplier",
    "industrial pipe and flow control supplier",
)


def _request(**overrides: Any) -> DiscoveryRequest:
    """Build one valid capped U.S. Apify discovery request."""
    values: dict[str, Any] = {
        "request_id": "apify:us:maps-pvf:v1",
        "provider": "apify",
        "query_family": "maps-pvf",
        "target_country_code": "US",
        "queries": TERMS,
        "max_results_per_query": 5,
        "max_results_total": 15,
        "max_cost_usd": 0.125,
    }
    values.update(overrides)
    return DiscoveryRequest(**values)


def _expected_input() -> dict[str, Any]:
    """Return the exact minimal Actor input frozen by the M2 contract."""
    return {
        "searchStringsArray": list(TERMS),
        "countryCode": "us",
        "language": "en",
        "maxCrawledPlacesPerSearch": 5,
        "website": "allPlaces",
        "skipClosedPlaces": False,
        "scrapePlaceDetailPage": False,
        "includeWebResults": False,
        "scrapeDirectories": False,
        "scrapeContacts": False,
        "scrapeSocialMediaProfiles": {
            "facebooks": False,
            "instagrams": False,
            "youtubes": False,
            "tiktoks": False,
            "twitters": False,
        },
        "maximumLeadsEnrichmentRecords": 0,
        "maxReviews": 0,
        "maxImages": 0,
        "maxCompetitorsToAnalyze": 0,
        "verifyLeadsEnrichmentEmails": False,
        "scrapeReviewsPersonalData": False,
        "enableCompetitorAnalysis": False,
    }


def test_apify_exact_start_input_one_run_callback_poll_dataset_mapping_and_usage() -> None:
    """One request starts one capped Actor and persists its ID before polling."""
    events: list[str] = []
    requests: list[httpx.Request] = []
    row = {
        "placeId": "place-1",
        "cid": "cid-1",
        "title": "Acme Industrial Supply",
        "url": "https://maps.google.com/?cid=1",
        "website": "https://acme.example",
        "city": "Houston",
        "state": "Texas",
        "postalCode": "77001",
        "countryCode": "US",
        "searchString": TERMS[0],
        "description": "Industrial valve and pipe supplier",
        "categoryName": "Industrial equipment supplier",
        "temporarilyClosed": False,
        "futureField": {"must": "survive"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            events.append("start")
            return httpx.Response(
                201,
                json={
                    "data": {
                        "id": "run-123",
                        "status": "RUNNING",
                        "defaultDatasetId": "dataset-123",
                    }
                },
            )
        if request.url.path == "/v2/actor-runs/run-123":
            events.append("poll")
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "run-123",
                        "status": "SUCCEEDED",
                        "defaultDatasetId": "dataset-123",
                        "usageTotalUsd": 0.031,
                    }
                },
            )
        if request.url.path == "/v2/datasets/dataset-123/items":
            events.append("dataset")
            return httpx.Response(200, json=[row, {**row, "placeId": "overflow"}] * 8)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def on_run_started(run_id: str) -> None:
        assert run_id == "run-123"
        events.append("callback")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ApifyDiscoveryProvider(
            api_token=TOKEN,
            client=client,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
            on_run_started=on_run_started,
        )
        batch = provider.search(_request())

    assert events[:3] == ["start", "callback", "poll"]
    assert events[-1] == "dataset"
    assert sum(request.method == "POST" for request in requests) == 1

    start = requests[0]
    assert start.method == "POST"
    assert start.url.path == "/v2/acts/compass~crawler-google-places/runs"
    assert start.headers["authorization"] == f"Bearer {TOKEN}"
    assert dict(start.url.params) == {
        "waitForFinish": "0",
        "maxItems": "15",
        "maxTotalChargeUsd": "0.125",
    }
    assert json.loads(start.content) == _expected_input()

    dataset_request = requests[-1]
    assert dataset_request.url.path == "/v2/datasets/dataset-123/items"
    assert dataset_request.url.params.get("clean") == "true"

    assert len(batch.records) == 15
    record = batch.records[0]
    assert record.provider_result_id in {"place-1", "cid-1"}
    assert record.name == "Acme Industrial Supply"
    assert record.source_url == "https://maps.google.com/?cid=1"
    assert record.website_url == "https://acme.example"
    assert record.city == "Houston"
    assert record.region == "Texas"
    assert record.postal_code == "77001"
    assert record.country_code == "US"
    assert record.query == TERMS[0]
    assert record.raw_metadata == row
    assert re.fullmatch(r"raw_[0-9a-f]{24}", record.record_id)
    assert len({item.retrieved_at for item in batch.records}) == 1
    retrieved = datetime.fromisoformat(record.retrieved_at)
    assert retrieved.tzinfo is not None
    offset = retrieved.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0

    assert len(batch.usage_events) == 1
    usage = batch.usage_events[0]
    assert usage.provider == "apify"
    assert usage.operation == "google_maps_search"
    assert usage.request_count == len(requests)
    assert usage.estimated_cost_usd == pytest.approx(0.031)
    assert usage.exact_cost_usd is None
    safe_usage = json.dumps(usage.to_dict(), sort_keys=True)
    assert TOKEN not in safe_usage
    assert "authorization" not in safe_usage.lower()


def test_missing_search_string_and_returned_country_stay_unknown() -> None:
    """Apify never substitutes request intent for omitted returned-row facts."""
    row = {
        "placeId": "place-1",
        "title": "Unknown Geography",
        "url": "https://maps.google.com/?cid=1",
        "website": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run-1", "status": "RUNNING"}})
        if request.url.path == "/v2/actor-runs/run-1":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "run-1",
                        "status": "SUCCEEDED",
                        "defaultDatasetId": "ds",
                    }
                },
            )
        return httpx.Response(200, json=[row])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        batch = ApifyDiscoveryProvider(
            api_token=TOKEN,
            client=client,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
        ).search(_request(max_results_total=1, max_results_per_query=1))

    assert batch.records[0].query is None
    assert batch.records[0].country_code is None
    assert batch.records[0].target_country_code == "US"


def test_resume_never_starts_a_replacement_run() -> None:
    """Resume polls and fetches only the supplied existing Actor run."""
    methods: list[str] = []
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        paths.append(request.url.path)
        if request.url.path == "/v2/actor-runs/existing-run":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "existing-run",
                        "status": "SUCCEEDED",
                        "defaultDatasetId": "existing-dataset",
                    }
                },
            )
        if request.url.path == "/v2/datasets/existing-dataset/items":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        batch = ApifyDiscoveryProvider(
            api_token=TOKEN,
            client=client,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
        ).resume(_request(), "existing-run")

    assert "POST" not in methods
    assert paths == [
        "/v2/actor-runs/existing-run",
        "/v2/datasets/existing-dataset/items",
    ]
    assert batch.records == []


def test_polling_deadline_is_local_retryable_transient_and_never_replaces_run() -> None:
    """A five-minute local deadline leaves the capped remote run untouched."""
    now = 0.0
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        assert seconds > 0
        sleeps.append(seconds)
        now += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "slow-run", "status": "RUNNING"}})
        return httpx.Response(200, json={"data": {"id": "slow-run", "status": "RUNNING"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ApifyDiscoveryProvider(
            api_token=TOKEN,
            client=client,
            monotonic=monotonic,
            sleep=sleep,
        )
        with pytest.raises(DiscoveryProviderError) as caught:
            provider.search(_request())

    assert caught.value.kind == "transient"
    assert caught.value.retryable is True
    assert sum(request.method == "POST" for request in requests) == 1
    assert now >= 300
    assert sleeps


def test_rejected_cap_is_not_raised_or_retried() -> None:
    """An Actor minimum-cap rejection returns invalid_request without cap escalation."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(400, text=f"minimum charge; do not leak {TOKEN}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ApifyDiscoveryProvider(api_token=TOKEN, client=client)
        with pytest.raises(DiscoveryProviderError) as caught:
            provider.search(_request(max_cost_usd=0.05))

    assert caught.value.kind == "invalid_request"
    assert caught.value.retryable is False
    assert len(requests) == 1
    assert requests[0].url.params.get("maxTotalChargeUsd") == "0.05"
    assert TOKEN not in str(caught.value)


def test_authenticated_apify_usage_above_authorized_cap_is_invalid_response() -> None:
    """PRV-07 fails closed if provider-reported spend exceeds the exact authorized run cap."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "data": {
                        "id": "over-cap-run",
                        "status": "SUCCEEDED",
                        "defaultDatasetId": "over-cap-dataset",
                        "usageTotalUsd": 0.126,
                    }
                },
            )
        if request.url.path == "/v2/datasets/over-cap-dataset/items":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ApifyDiscoveryProvider(api_token=TOKEN, client=client)
        with pytest.raises(DiscoveryProviderError) as caught:
            provider.search(_request(max_cost_usd=0.125))

    assert caught.value.kind == "invalid_response"
    assert caught.value.retryable is False


@pytest.mark.parametrize("status", ["FAILED", "TIMED-OUT", "ABORTED"])
def test_terminal_actor_failure_is_permanent(status: str) -> None:
    """Failed, timed-out, and aborted Actor runs are terminal permanent errors."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run-x", "status": "RUNNING"}})
        return httpx.Response(200, json={"data": {"id": "run-x", "status": status}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ApifyDiscoveryProvider(
            api_token=TOKEN,
            client=client,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
        )
        with pytest.raises(DiscoveryProviderError) as caught:
            provider.search(_request())

    assert caught.value.kind == "permanent"
    assert caught.value.retryable is False


def test_unknown_actor_state_is_invalid_response() -> None:
    """Provider API drift in run state fails closed rather than broadening behavior."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run-x", "status": "RUNNING"}})
        return httpx.Response(200, json={"data": {"id": "run-x", "status": "NEW_STATE"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ApifyDiscoveryProvider(
            api_token=TOKEN,
            client=client,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
        )
        with pytest.raises(DiscoveryProviderError) as caught:
            provider.search(_request())

    assert caught.value.kind == "invalid_response"
    assert caught.value.retryable is False


@pytest.mark.parametrize(
    ("status_code", "kind", "retryable"),
    [
        (401, "authentication", False),
        (403, "authentication", False),
        (402, "budget_exhausted", False),
        (408, "rate_limited", True),
        (429, "rate_limited", True),
        (500, "transient", True),
        (404, "permanent", False),
    ],
)
def test_apify_http_failure_classification_is_sanitized(
    status_code: int,
    kind: str,
    retryable: bool,
) -> None:
    """Shared HTTP failure classes apply to Apify without leaking response text."""
    calls = 0
    unsafe = f"provider body containing {TOKEN}"

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, text=unsafe)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ApifyDiscoveryProvider(api_token=TOKEN, client=client)
        with pytest.raises(DiscoveryProviderError) as caught:
            provider.search(_request())

    error = caught.value
    assert calls == 1
    assert error.provider == "apify"
    assert error.kind == kind
    assert error.retryable is retryable
    assert error.status_code == status_code
    assert error.usage_event.request_count == 1
    assert TOKEN not in str(error)
    assert unsafe not in str(error)
    assert TOKEN not in json.dumps(error.usage_event.to_dict(), sort_keys=True)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(201, content=b"{"),
        httpx.Response(201, json={}),
        httpx.Response(201, json={"data": {"status": "RUNNING"}}),
        httpx.Response(201, json={"data": "not-an-object"}),
    ],
)
def test_malformed_apify_start_responses_are_invalid_response(response: httpx.Response) -> None:
    """Malformed Actor start envelopes fail closed after exactly one attempted call."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ApifyDiscoveryProvider(api_token=TOKEN, client=client)
        with pytest.raises(DiscoveryProviderError) as caught:
            provider.search(_request())

    assert calls == 1
    assert caught.value.kind == "invalid_response"
    assert caught.value.retryable is False
    assert caught.value.usage_event.request_count == 1


def test_malformed_apify_dataset_type_is_invalid_response() -> None:
    """A successful run with a non-list dataset response is invalid rather than fabricated."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "data": {
                        "id": "run-x",
                        "status": "SUCCEEDED",
                        "defaultDatasetId": "ds",
                    }
                },
            )
        if request.url.path == "/v2/datasets/ds/items":
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ApifyDiscoveryProvider(api_token=TOKEN, client=client)
        with pytest.raises(DiscoveryProviderError) as caught:
            provider.search(_request())

    assert caught.value.kind == "invalid_response"
    assert caught.value.retryable is False


@pytest.mark.parametrize(
    "discovery_request",
    [
        _request(provider="exa"),
        _request(target_country_code="MX"),
        _request(queries=(TERMS[0],)),
        _request(queries=(TERMS[0], "", TERMS[2])),
        _request(max_results_total=0),
        _request(max_results_total=101),
        _request(max_cost_usd=None),
        _request(max_cost_usd=-0.01),
        _request(max_cost_usd=1.01),
    ],
)
def test_invalid_apify_requests_fail_before_http(discovery_request: DiscoveryRequest) -> None:
    """Invalid provider/geography/catalog/result/cap requests perform no HTTP calls."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ApifyDiscoveryProvider(api_token=TOKEN, client=client)
        with pytest.raises((DiscoveryProviderError, TypeError, ValueError)):
            provider.search(discovery_request)

    assert calls == 0
