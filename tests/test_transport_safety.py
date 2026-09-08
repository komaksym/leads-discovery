"""Behavioral contracts for bounded, secret-safe provider transport."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from leads_discovery.discovery.apify import ApifyDiscoveryProvider
from leads_discovery.discovery.base import (
    DiscoveryProviderError,
    ResponseTooLargeError,
    read_bounded_response,
    request_json,
)
from leads_discovery.discovery.exa import ExaDiscoveryProvider
from leads_discovery.models import (
    CompanyRecord,
    DiscoveryRequest,
    EvidenceBundle,
    EvidenceItem,
    UsageEvent,
)
from leads_discovery.pipeline.costs import CostTracker
from leads_discovery.research.evidence import ExaEvidenceResearcher
from leads_discovery.research.extract import DeepSeekExtractor, DeepSeekPriceSchedule


class _TrackedStream(httpx.SyncByteStream):
    """Yield controlled chunks and expose exactly how far a consumer read."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.chunks_consumed = 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            self.chunks_consumed += 1
            yield chunk


class _ExplodingStream(httpx.SyncByteStream):
    """Fail during body streaming with provider-controlled text."""

    def __init__(self) -> None:
        self.chunks_consumed = 0

    def __iter__(self) -> Iterator[bytes]:
        self.chunks_consumed += 1
        yield b'{"partial":'
        self.chunks_consumed += 1
        raise httpx.ReadError(
            "secret-provider-body",
            request=httpx.Request("GET", "https://provider.example"),
        )


class _UnreadableStream(httpx.SyncByteStream):
    """Fail if a declared-oversized response body is touched."""

    def __init__(self) -> None:
        self.chunks_consumed = 0

    def __iter__(self) -> Iterator[bytes]:
        self.chunks_consumed += 1
        raise AssertionError("oversized body must not be consumed")
        yield b""  # pragma: no cover


def _exa_request() -> DiscoveryRequest:
    return DiscoveryRequest(
        request_id="exa:transport:v1",
        provider="exa",
        query_family="core-pvf",
        target_country_code="US",
        queries=("industrial PVF distributors",),
        max_results_per_query=1,
        max_results_total=1,
        max_cost_usd=None,
    )


def _company() -> CompanyRecord:
    return CompanyRecord(
        company_id="cmp_transport",
        name="Transport Industrial",
        normalized_name="transport industrial",
        domain="transport.example",
        normalized_domain="transport.example",
        country="US",
    )


def test_declared_oversize_is_rejected_before_body_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "8")
    stream = _UnreadableStream()
    response = httpx.Response(
        200,
        headers={"Content-Length": "9"},
        stream=stream,
    )

    with pytest.raises(ResponseTooLargeError):
        read_bounded_response(response)

    assert stream.chunks_consumed == 0


def test_chunked_oversize_stops_on_crossing_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "5")
    stream = _TrackedStream([b"12", b"34", b"56", b"must-not-read"])
    response = httpx.Response(200, stream=stream)

    with pytest.raises(ResponseTooLargeError):
        read_bounded_response(response)

    assert stream.chunks_consumed == 3


def test_read_time_http_error_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "1024")
    stream = _ExplodingStream()
    response = httpx.Response(200, stream=stream)

    with pytest.raises(DiscoveryProviderError) as captured:
        request_json(
            response,
            provider="exa",
            request_id="read-failure",
            operation="company_search",
            request_count=1,
        )

    assert captured.value.kind == "invalid_response"
    assert captured.value.retryable is False
    assert "secret-provider-body" not in str(captured.value)
    assert "secret-provider-body" not in repr(captured.value.usage_event.to_dict())
    assert stream.chunks_consumed == 2


def test_normal_streamed_json_preserves_provider_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "64")
    response = httpx.Response(200, stream=_TrackedStream([b'{"results":', b"[]}"]))

    payload = request_json(
        response,
        provider="exa",
        request_id="normal",
        operation="company_search",
        request_count=1,
    )

    assert payload == {"results": []}


def test_exa_discovery_owns_explicit_timeout_with_injected_transport() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeout = request.extensions.get("timeout")
        assert isinstance(timeout, dict)
        seen.append(timeout)
        return httpx.Response(200, json={"results": []})

    with httpx.Client(transport=httpx.MockTransport(handler), timeout=None) as client:
        result = ExaDiscoveryProvider(api_key="test-key", client=client).search(_exa_request())

    assert result.records == []
    assert seen == [
        {
            "connect": 5.0,
            "read": 30.0,
            "write": 30.0,
            "pool": 30.0,
        }
    ]


def test_exa_research_owns_explicit_timeout_with_injected_transport() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeout = request.extensions.get("timeout")
        assert isinstance(timeout, dict)
        seen.append(timeout)
        return httpx.Response(200, json={"results": []})

    with httpx.Client(transport=httpx.MockTransport(handler), timeout=None) as client:
        result = ExaEvidenceResearcher(api_key="test-key", client=client).research(_company())

    assert result.items == []
    assert len(seen) == 3
    assert all(
        timeout
        == {
            "connect": 5.0,
            "read": 30.0,
            "write": 30.0,
            "pool": 30.0,
        }
        for timeout in seen
    )


def test_deepseek_declared_oversize_is_secret_safe_and_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "8")
    stream = _UnreadableStream()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": "9"},
            stream=stream,
        )

    bundle = EvidenceBundle(
        company_id="cmp_transport",
        items=[
            EvidenceItem(
                evidence_id="ev_transport",
                url="https://transport.example/about",
                title="About",
                excerpt="Industrial valves.",
                provider="exa",
            )
        ],
        raw_records=[],
        usage_events=[],
    )
    with httpx.Client(transport=httpx.MockTransport(handler), timeout=None) as client:
        extractor = DeepSeekExtractor(
            api_key="deepseek-secret",
            client=client,
            model="deepseek-v4-flash",
            prices=DeepSeekPriceSchedule(0.0, 0.0, 0.0),
        )
        with pytest.raises(DiscoveryProviderError) as captured:
            extractor.extract(_company(), bundle)

    assert captured.value.kind == "invalid_response"
    assert "deepseek-secret" not in str(captured.value)
    assert stream.chunks_consumed == 0


def test_exa_rate_limited_search_records_zero_cost_and_keeps_budget_known() -> None:
    """An Exa 429 proves no billed search and must not poison the budget ledger."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler), timeout=None) as client,
        pytest.raises(DiscoveryProviderError) as captured,
    ):
        ExaDiscoveryProvider(api_key="test-key", client=client).search(_exa_request())

    assert captured.value.kind == "rate_limited"
    assert captured.value.retryable is True
    assert captured.value.usage_event.estimated_cost_usd == 0.0

    tracker = CostTracker(
        [
            UsageEvent(
                provider="exa",
                operation="company_search",
                estimated_cost_usd=0.098,
            ),
            captured.value.usage_event,
        ]
    )

    assert tracker.provider_estimated_spend("exa") == 0.098


def test_exa_connect_failure_records_zero_cost_as_never_billed() -> None:
    """An Exa connect failure never reached the provider and must cost zero."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler), timeout=None) as client,
        pytest.raises(DiscoveryProviderError) as captured,
    ):
        ExaDiscoveryProvider(api_key="test-key", client=client).search(_exa_request())

    assert captured.value.kind == "transient"
    assert captured.value.retryable is True
    assert captured.value.usage_event.estimated_cost_usd == 0.0


def test_exa_ambiguous_failure_keeps_cost_unknown() -> None:
    """An ambiguous mid-dispatch Exa failure may have billed and must stay unknown."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection dropped", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler), timeout=None) as client,
        pytest.raises(DiscoveryProviderError) as captured,
    ):
        ExaDiscoveryProvider(api_key="test-key", client=client).search(_exa_request())

    assert captured.value.kind == "transient"
    assert captured.value.retryable is False
    assert captured.value.usage_event.estimated_cost_usd is None


def test_exa_local_validation_error_records_zero_cost() -> None:
    """Exa local validation never dispatched and must not poison the budget ledger."""
    bad_request = DiscoveryRequest(
        request_id="exa:transport:v1",
        provider="exa",
        query_family="core-pvf",
        target_country_code="US",
        queries=("one query", "second query"),
        max_results_per_query=1,
        max_results_total=2,
        max_cost_usd=None,
    )

    with (
        httpx.Client(transport=httpx.MockTransport(_unreachable), timeout=None) as client,
        pytest.raises(DiscoveryProviderError) as captured,
    ):
        ExaDiscoveryProvider(api_key="test-key", client=client).search(bad_request)

    assert captured.value.kind == "invalid_request"
    assert captured.value.usage_event.request_count == 0
    assert captured.value.usage_event.estimated_cost_usd == 0.0


def test_deepseek_empty_bundle_validation_records_zero_cost() -> None:
    """DeepSeek local validation never dispatched and must not poison the budget ledger."""
    empty = EvidenceBundle(
        company_id="cmp_transport",
        items=[],
        raw_records=[],
        usage_events=[],
    )

    with httpx.Client(transport=httpx.MockTransport(_unreachable), timeout=None) as client:
        extractor = DeepSeekExtractor(
            api_key="deepseek-secret",
            client=client,
            model="deepseek-v4-flash",
            prices=DeepSeekPriceSchedule(0.0, 0.0, 0.0),
        )
        with pytest.raises(DiscoveryProviderError) as captured:
            extractor.extract(_company(), empty)

    assert captured.value.kind == "invalid_request"
    assert captured.value.usage_event.request_count == 0
    assert captured.value.usage_event.estimated_cost_usd == 0.0


def test_apify_post_start_poll_failure_keeps_cost_unknown() -> None:
    """A post-start Apify poll failure leaves actor spend ambiguous and stays unknown."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                201,
                json={"data": {"id": "run-123", "status": "RUNNING"}},
            )
        return httpx.Response(500, json={"error": "actor host failed"})

    with httpx.Client(transport=httpx.MockTransport(handler), timeout=None) as client:
        provider = ApifyDiscoveryProvider(
            api_token="test-token",
            client=client,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
        )
        with pytest.raises(DiscoveryProviderError) as captured:
            provider.search(_apify_request())

    assert captured.value.usage_event.estimated_cost_usd is None


def _unreachable(_request: httpx.Request) -> httpx.Response:
    """Fail any test that unexpectedly reaches the network."""
    raise AssertionError("local validation must fail before dispatch")


def _apify_request() -> DiscoveryRequest:
    """Build one valid Apify request for post-start failure coverage."""
    return DiscoveryRequest(
        request_id="apify:transport:v1",
        provider="apify",
        query_family="core-pvf",
        target_country_code="US",
        queries=("pvf one", "pvf two", "pvf three"),
        max_results_per_query=1,
        max_results_total=3,
        max_cost_usd=0.25,
    )
