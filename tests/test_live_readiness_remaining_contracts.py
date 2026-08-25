"""Focused independent regressions for the final live-readiness blockers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import httpx
import pytest
from m3_factories import build_company

from leads_discovery.discovery.base import DiscoveryProviderError
from leads_discovery.discovery.exa import ExaDiscoveryProvider
from leads_discovery.discovery.queries import build_discovery_requests
from leads_discovery.models import CompanyRecord, DiscoveryRequest, EvidenceItem
from leads_discovery.scoring import evaluate_company


class _GuardedChunkStream(httpx.SyncByteStream):
    """Expose bounded chunks and fail if a caller keeps consuming past the guard."""

    def __init__(self, *, chunk: bytes, allowed_chunks: int) -> None:
        """Store one reusable chunk and the maximum reads permitted by the oracle."""
        self._chunk = chunk
        self._allowed_chunks = allowed_chunks
        self.chunks_consumed = 0

    def __iter__(self) -> Iterator[bytes]:
        """Yield chunks lazily and expose an unmistakable unbounded-read failure."""
        while True:
            if self.chunks_consumed >= self._allowed_chunks:
                raise AssertionError("provider consumed an unbounded response body")
            self.chunks_consumed += 1
            yield self._chunk


class _UnreadableStream(httpx.SyncByteStream):
    """Fail on any body read so Content-Length rejection can prove it is early."""

    def __init__(self) -> None:
        """Initialize the read counter used by the assertion."""
        self.chunks_consumed = 0

    def __iter__(self) -> Iterator[bytes]:
        """Reject the first attempted body read."""
        self.chunks_consumed += 1
        raise AssertionError("oversized Content-Length body must not be consumed")
        yield b""  # pragma: no cover


def _exa_request() -> DiscoveryRequest:
    """Return the suite's normal bounded Exa company-search request."""
    return next(
        request
        for request in build_discovery_requests(include_apify=False)
        if request.provider == "exa"
    )


def _company_with_negative_relevance_evidence(excerpt: str) -> CompanyRecord:
    """Build one high-confidence negative relevance claim backed by supplied evidence."""
    company = build_company(facts={"pvf_relevant": (False, 0.95)})
    evidence = company.evidence[0]
    payload = evidence.to_dict()
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
def test_negative_claim_requires_semantically_associated_negation(excerpt: str) -> None:
    """Unrelated negation cannot validate pvf_relevant=false or hard-reject a company."""
    result = evaluate_company(_company_with_negative_relevance_evidence(excerpt))

    assert result.final_decision == "uncertain"
    assert "confirmed_not_pvf_relevant" not in result.rejection_reasons


def test_genuine_pvf_negation_remains_valid_negative_evidence() -> None:
    """A negation directly governing PVF selling/distribution may support rejection."""
    result = evaluate_company(
        _company_with_negative_relevance_evidence(
            "We do not sell or distribute pipe, valves, or fittings."
        )
    )

    assert result.final_decision == "rejected"
    assert "confirmed_not_pvf_relevant" in result.rejection_reasons


def test_exa_chunked_response_limit_stops_stream_consumption_early() -> None:
    """A no-Length oversized body must be rejected before its stream is exhausted."""
    stream = _GuardedChunkStream(chunk=b"x" * (1024 * 1024), allowed_chunks=32)

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return an indefinitely chunked provider body with no Content-Length."""
        return httpx.Response(200, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ExaDiscoveryProvider(api_key="test-key", client=client)
        with pytest.raises(DiscoveryProviderError):
            provider.search(_exa_request())

    assert stream.chunks_consumed < 32


def test_exa_oversized_content_length_is_rejected_before_body_read() -> None:
    """An obviously oversized declared body must fail without touching its stream."""
    stream = _UnreadableStream()

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return an oversized declaration whose body must remain unread."""
        return httpx.Response(
            200,
            headers={"Content-Length": "1000000000"},
            stream=stream,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ExaDiscoveryProvider(api_key="test-key", client=client)
        with pytest.raises(DiscoveryProviderError):
            provider.search(_exa_request())

    assert stream.chunks_consumed == 0


def test_exa_small_json_response_still_works_with_response_limits() -> None:
    """Response bounding must preserve normal small provider JSON behavior."""

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return the smallest valid empty Exa search result."""
        return httpx.Response(200, json={"results": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ExaDiscoveryProvider(api_key="test-key", client=client)
        result = provider.search(_exa_request())

    assert result.records == []


def _timeout_signature(value: object) -> tuple[float | None, ...]:
    """Normalize supported HTTPX timeout forms without requiring one exact duration."""
    if isinstance(value, bool):
        raise AssertionError("boolean is not a provider timeout policy")
    if isinstance(value, (int, float)):
        number = float(value)
        return (number, number, number, number)
    if isinstance(value, httpx.Timeout):
        return (value.connect, value.read, value.write, value.pool)
    raise AssertionError(f"unsupported provider timeout value: {type(value).__name__}")


def test_exa_default_adapter_owns_explicit_deterministic_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default Exa construction must explicitly configure its own HTTP timeout policy."""
    real_client = httpx.Client
    provider_type = cast(Any, ExaDiscoveryProvider)
    observed: list[object] = []

    def capture_client(*args: Any, **kwargs: Any) -> httpx.Client:
        """Record explicit timeout construction while keeping a real offline client."""
        assert "timeout" in kwargs, "Exa must not inherit HTTPX's generic default timeout"
        observed.append(kwargs["timeout"])
        kwargs["transport"] = httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"results": []})
        )
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", capture_client)
    first = provider_type(api_key="test-key")
    second = provider_type(api_key="test-key")
    del first, second

    assert len(observed) == 2
    first_timeout = _timeout_signature(observed[0])
    second_timeout = _timeout_signature(observed[1])
    assert first_timeout == second_timeout
    assert all(value is not None and value > 0 for value in first_timeout)


def test_exa_injected_mock_client_remains_supported() -> None:
    """Explicit provider-owned defaults must not break caller-injected offline clients."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return one valid empty result through the injected MockTransport."""
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"results": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ExaDiscoveryProvider(api_key="test-key", client=client)
        result = provider.search(_exa_request())

    assert calls == 1
    assert result.records == []
