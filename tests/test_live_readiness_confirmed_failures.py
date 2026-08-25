"""Behavioral contracts for the three confirmed live-readiness failures."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from m3_factories import build_company

import leads_discovery.discovery.exa as exa_module
from leads_discovery.discovery.base import DiscoveryProviderError
from leads_discovery.discovery.exa import ExaDiscoveryProvider
from leads_discovery.discovery.queries import build_discovery_requests
from leads_discovery.models import EvidenceItem
from leads_discovery.scoring import evaluate_company


class CountingStream(httpx.SyncByteStream):
    """Expose exactly how far a caller consumes one synthetic response body."""

    def __init__(self, chunks: list[bytes], *, fail_after: int | None = None) -> None:
        """Store lazy chunks and optionally fail if the consumer reads too far."""
        self._chunks = chunks
        self._fail_after = fail_after
        self.read_count = 0

    def __iter__(self) -> Iterator[bytes]:
        """Yield chunks while recording every consumed body segment."""
        for chunk in self._chunks:
            self.read_count += 1
            if self._fail_after is not None and self.read_count > self._fail_after:
                raise AssertionError("provider consumed an unbounded response body")
            yield chunk

    def close(self) -> None:
        """Satisfy the synchronous HTTPX stream lifecycle contract."""


def _exa_request() -> Any:
    """Return the suite's normal bounded Exa discovery request."""
    return next(request for request in build_discovery_requests() if request.provider == "exa")


def _company_with_pvf_false(excerpt: str):
    """Build one high-confidence negative PVF claim backed by supplied evidence text."""
    company = build_company(facts={"pvf_relevant": (False, 0.95)})
    payload = company.evidence[0].to_dict()
    payload["excerpt"] = excerpt
    company.evidence = [EvidenceItem.from_dict(payload)]
    return company


@pytest.mark.parametrize(
    "excerpt",
    [
        (
            "We do not sell electrical equipment. "
            "We distribute industrial pipe, valves, and fittings."
        ),
        "We are not a manufacturer. We distribute industrial valves.",
    ],
)
def test_negative_evidence_must_support_the_negative_claim(excerpt: str) -> None:
    """Unrelated negation cannot validate pvf_relevant=false or hard-reject a company."""
    result = evaluate_company(_company_with_pvf_false(excerpt))

    assert result.final_decision == "uncertain"
    assert "confirmed_not_pvf_relevant" not in result.rejection_reasons


def test_genuine_pvf_negative_evidence_may_hard_reject() -> None:
    """A direct negative about selling/distributing PVF remains valid negative evidence."""
    result = evaluate_company(
        _company_with_pvf_false("We do not sell or distribute pipe, valves, or fittings.")
    )

    assert result.final_decision == "rejected"
    assert "confirmed_not_pvf_relevant" in result.rejection_reasons


def test_exa_stops_consuming_chunked_body_after_response_limit() -> None:
    """A no-Content-Length response must be rejected before its whole body is buffered."""
    chunk = b"x" * (2 * 1024 * 1024)
    stream = CountingStream([chunk] * 40, fail_after=20)

    def handler(request: httpx.Request) -> httpx.Response:
        """Return one oversized chunked response with no declared length."""
        return httpx.Response(200, stream=stream, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ExaDiscoveryProvider(api_key="test-key", client=client)
        with pytest.raises(DiscoveryProviderError):
            provider.search(_exa_request())

    assert stream.read_count <= 20


def test_exa_rejects_oversized_content_length_before_body_read() -> None:
    """A declared oversized body must be rejected without consuming response bytes."""
    stream = CountingStream([b"must-not-be-read"], fail_after=0)

    def handler(request: httpx.Request) -> httpx.Response:
        """Advertise an obviously oversized provider payload."""
        return httpx.Response(
            200,
            headers={"Content-Length": "1000000000"},
            stream=stream,
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ExaDiscoveryProvider(api_key="test-key", client=client)
        with pytest.raises(DiscoveryProviderError):
            provider.search(_exa_request())

    assert stream.read_count == 0


def test_exa_small_json_response_still_works() -> None:
    """Normal bounded JSON remains usable through the same provider boundary."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Return the smallest valid Exa search result envelope."""
        return httpx.Response(200, json={"results": []}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ExaDiscoveryProvider(api_key="test-key", client=client)
        batch = provider.search(_exa_request())

    assert batch.records == []


def test_exa_default_adapter_owns_an_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default Exa construction must explicitly choose its HTTP timeout policy."""
    real_client = httpx.Client
    missing = object()
    observed: list[object] = []
    created: list[httpx.Client] = []

    def client_factory(*args: Any, **kwargs: Any) -> httpx.Client:
        """Capture constructor timeout while keeping all HTTP work in memory."""
        observed.append(kwargs.get("timeout", missing))
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"results": []}, request=request)
        )
        client = real_client(*args, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(exa_module.httpx, "Client", client_factory)
    try:
        provider = ExaDiscoveryProvider(api_key="test-key")
        provider.search(_exa_request())
    finally:
        for client in created:
            client.close()

    assert observed
    assert observed[0] is not missing
    assert observed[0] is not None


def test_exa_injected_mock_client_remains_supported() -> None:
    """Explicit provider timeout ownership must not break caller-injected transports."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a valid result through a caller-owned in-memory client."""
        return httpx.Response(200, json={"results": []}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ExaDiscoveryProvider(api_key="test-key", client=client)
        assert provider.search(_exa_request()).records == []
