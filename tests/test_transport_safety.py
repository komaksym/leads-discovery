"""Behavioral contracts for bounded, secret-safe provider transport."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from leads_discovery.discovery.base import (
    DiscoveryProviderError,
    ProviderRequestContext,
    ResponseTooLargeError,
    read_bounded_response,
    request_json,
    safe_transport_call,
    validation_error,
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


def _transport_context() -> ProviderRequestContext:
    """Build one stable request identity for direct transport-boundary tests."""
    return ProviderRequestContext(
        provider="exa",
        request_id="exa:transport-cost:v1",
        operation="company_research",
        request_count=1,
    )


def test_rate_limited_rejection_records_zero_cost_and_keeps_budget_known() -> None:
    """A 429 rejection is knowably unbilled and must not poison the budget ledger."""

    def failing_call() -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    with pytest.raises(DiscoveryProviderError) as captured:
        safe_transport_call(failing_call, context=_transport_context())

    assert captured.value.kind == "rate_limited"
    assert captured.value.retryable is True
    assert captured.value.usage_event.estimated_cost_usd == 0.0

    tracker = CostTracker(
        [
            UsageEvent(
                provider="exa",
                operation="company_research",
                estimated_cost_usd=0.098,
            ),
            captured.value.usage_event,
        ]
    )

    assert tracker.provider_estimated_spend("exa") == 0.098


def test_connect_failure_records_zero_cost_as_never_billed() -> None:
    """A connect failure never reached the provider and must cost zero."""

    def failing_call() -> httpx.Response:
        raise httpx.ConnectError(
            "connection refused",
            request=httpx.Request("POST", "https://api.exa.ai/search"),
        )

    with pytest.raises(DiscoveryProviderError) as captured:
        safe_transport_call(failing_call, context=_transport_context())

    assert captured.value.kind == "transient"
    assert captured.value.retryable is True
    assert captured.value.usage_event.estimated_cost_usd == 0.0


def test_ambiguous_transport_failure_keeps_cost_unknown() -> None:
    """An ambiguous mid-dispatch failure may have billed and must stay unknown."""

    def failing_call() -> httpx.Response:
        raise httpx.ReadError(
            "connection dropped",
            request=httpx.Request("POST", "https://api.exa.ai/search"),
        )

    with pytest.raises(DiscoveryProviderError) as captured:
        safe_transport_call(failing_call, context=_transport_context())

    assert captured.value.kind == "transient"
    assert captured.value.retryable is False
    assert captured.value.usage_event.estimated_cost_usd is None


def test_local_validation_error_records_zero_cost() -> None:
    """Local validation never dispatched and must not poison the budget ledger."""
    error = validation_error(
        provider="exa",
        request_id="exa:transport:v1",
        message="bad request",
        operation="company_search",
    )

    assert error.usage_event.request_count == 0
    assert error.usage_event.estimated_cost_usd == 0.0
