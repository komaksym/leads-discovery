"""Focused independent regressions for the final live-readiness blockers."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from m3_factories import build_company

from leads_discovery.discovery.base import DiscoveryProviderError
from leads_discovery.discovery.exa import ExaDiscoveryProvider
from leads_discovery.discovery.queries import build_discovery_requests
from leads_discovery.models import (
    CompanyRecord,
    DiscoveryRequest,
    EvidenceBundle,
    EvidenceItem,
    ExtractedFact,
    ExtractionResult,
    UsageEvent,
)
from leads_discovery.research.extract import FACT_KEYS, apply_extraction
from leads_discovery.scoring import evaluate_company


class _GuardedChunkStream(httpx.SyncByteStream):
    """Expose lazy chunks and abort CI if a caller never enforces any finite limit."""

    def __init__(self, *, chunk: bytes, allowed_chunks: int) -> None:
        """Store one reusable chunk and a test-only runaway-consumption guard."""
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
    item = EvidenceItem.from_dict(payload)
    bundle = EvidenceBundle(
        company_id=company.company_id,
        items=[item],
        raw_records=[],
        usage_events=[],
    )
    facts = {key: ExtractedFact(None, 0.0, []) for key in FACT_KEYS}
    facts["pvf_relevant"] = ExtractedFact(False, 0.95, [item.evidence_id])
    return apply_extraction(
        company,
        bundle,
        ExtractionResult(
            company_id=company.company_id,
            model="deepseek-v4-flash",
            facts=facts,
            usage_event=UsageEvent(
                provider="deepseek",
                operation="structured_extraction",
            ),
        ),
    )


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
