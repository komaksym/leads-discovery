"""Focused independent regressions for the final live-readiness blockers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.providers import (
    ApolloContactProvider,
    ClayContactProvider,
    ContactProviderError,
    ExaPeopleProvider,
    InstantlyVerificationProvider,
)
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

_EVIDENCE_ID = "ev_remaining_readiness"


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


def _invoke_m4_provider(provider_name: str, client: httpx.Client) -> None:
    """Call one public M4 provider adapter through the injected HTTP seam."""
    contact = ContactRecord(
        contact_id="contact-transport",
        company_id="cmp_transport",
        company_name="Transport Valve",
        company_domain="transport.example",
        company_final_score=1.0,
        full_name="Taylor Transport",
        title="President",
        decision_rank=1,
        decision_reason="owner",
    )
    company = CompanyRecord(
        company_id="cmp_transport",
        name="Transport Valve",
        domain="transport.example",
        normalized_domain="transport.example",
    )
    if provider_name == "exa":
        ExaPeopleProvider(api_key="test", client=client).search(company)
    elif provider_name == "clay":
        ClayContactProvider(api_key="test", routine_id="routine-1", client=client).start([contact])
    elif provider_name == "apollo":
        ApolloContactProvider(api_key="test", client=client).enrich(contact)
    elif provider_name == "instantly":
        InstantlyVerificationProvider(api_key="test", client=client).create(
            "taylor@transport.example"
        )
    else:  # pragma: no cover - parameterization controls provider names.
        raise AssertionError(f"unsupported provider: {provider_name}")


def _exa_request() -> DiscoveryRequest:
    """Return the suite's normal bounded Exa company-search request."""
    return next(
        request
        for request in build_discovery_requests(include_apify=False)
        if request.provider == "exa"
    )


def _evaluate_negative_relevance(excerpt: str) -> CompanyRecord:
    """Apply one cited negative relevance claim through the production extraction boundary."""
    company = CompanyRecord(
        company_id="cmp_remaining_readiness",
        name="Remaining Readiness Valve",
        domain="remaining.example",
        normalized_domain="remaining.example",
    )
    bundle = EvidenceBundle(
        company_id=company.company_id,
        items=[
            EvidenceItem(
                evidence_id=_EVIDENCE_ID,
                url="https://remaining.example/about",
                excerpt=excerpt,
                provider="exa",
            )
        ],
        raw_records=[],
        usage_events=[],
    )
    facts = {key: ExtractedFact(None, 0.0, []) for key in FACT_KEYS}
    facts["pvf_relevant"] = ExtractedFact(False, 0.95, [_EVIDENCE_ID])
    extracted = apply_extraction(
        company,
        bundle,
        ExtractionResult(
            company_id=company.company_id,
            model="deepseek-v4-flash",
            facts=facts,
            usage_event=UsageEvent(provider="deepseek", operation="structured_extraction"),
        ),
    )
    return evaluate_company(extracted)


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
    result = _evaluate_negative_relevance(excerpt)

    assert result.final_decision == "uncertain"
    assert "confirmed_not_pvf_relevant" not in result.rejection_reasons


def test_genuine_pvf_negation_remains_valid_negative_evidence() -> None:
    """A negation directly governing PVF selling/distribution may support rejection."""
    result = _evaluate_negative_relevance(
        "We do not sell or distribute pipe, valves, or fittings."
    )

    assert result.final_decision == "rejected"
    assert "confirmed_not_pvf_relevant" in result.rejection_reasons


def test_exa_chunked_response_limit_stops_stream_consumption_early() -> None:
    """A no-Length oversized body must be rejected before its stream is exhausted."""
    stream = _GuardedChunkStream(chunk=b"x" * (1024 * 1024), allowed_chunks=32)

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return an indefinitely chunked provider body with no Content-Length."""
        return httpx.Response(200, stream=stream)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(DiscoveryProviderError),
    ):
        ExaDiscoveryProvider(api_key="test-key", client=client).search(_exa_request())

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

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(DiscoveryProviderError),
    ):
        ExaDiscoveryProvider(api_key="test-key", client=client).search(_exa_request())

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


@pytest.mark.parametrize("provider_name", ["exa", "clay", "apollo", "instantly"])
def test_m4_oversized_content_length_is_rejected_before_body_read(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
) -> None:
    """Every M4 provider must reject an oversized declaration before body consumption."""
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "8")
    stream = _UnreadableStream()

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return a declared oversized body that must remain completely unread."""
        return httpx.Response(
            200,
            headers={"Content-Length": "9"},
            stream=stream,
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ContactProviderError) as exc_info,
    ):
        _invoke_m4_provider(provider_name, client)

    assert stream.chunks_consumed == 0
    assert exc_info.value.kind == "invalid_response"
    assert exc_info.value.retryable is False


@pytest.mark.parametrize("provider_name", ["exa", "clay", "apollo", "instantly"])
def test_m4_chunked_oversize_aborts_on_first_crossing_chunk(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
) -> None:
    """Every M4 provider must stop a no-Length stream at the first crossing chunk."""
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "5")
    stream = _GuardedChunkStream(chunk=b"123", allowed_chunks=3)

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return a chunked response whose second chunk crosses the configured limit."""
        return httpx.Response(200, stream=stream)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ContactProviderError) as exc_info,
    ):
        _invoke_m4_provider(provider_name, client)

    assert stream.chunks_consumed == 2
    assert exc_info.value.kind == "invalid_response"
    assert exc_info.value.retryable is False


@pytest.mark.parametrize("provider_name", ["exa", "clay", "apollo", "instantly"])
def test_m4_transport_does_not_retry_provider_failures(provider_name: str) -> None:
    """Adapters may classify retryability but must never replay an operation internally."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return one retryable provider failure and count actual request attempts."""
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": "temporary"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ContactProviderError) as exc_info,
    ):
        _invoke_m4_provider(provider_name, client)

    assert calls == 1
    assert exc_info.value.kind == "transient"
    assert exc_info.value.retryable is True


def test_exa_provider_boundary_owns_explicit_deterministic_timeout() -> None:
    """Exa request timeout behavior must remain explicit even with a timeout-free caller client."""
    observed: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Capture the timeout policy attached to the actual Exa provider request."""
        timeout = request.extensions.get("timeout")
        assert isinstance(timeout, dict)
        observed.append(timeout)
        return httpx.Response(200, json={"results": []})

    with httpx.Client(transport=httpx.MockTransport(handler), timeout=None) as client:
        first = ExaDiscoveryProvider(api_key="test-key", client=client).search(_exa_request())
        second = ExaDiscoveryProvider(api_key="test-key", client=client).search(_exa_request())

    assert first.records == []
    assert second.records == []
    assert len(observed) == 2
    assert observed[0] == observed[1]
    assert all(value is not None and float(value) > 0 for value in observed[0].values())


@pytest.mark.parametrize("provider_name", ["exa", "clay", "apollo", "instantly"])
def test_m4_provider_boundaries_own_explicit_deterministic_timeout(
    provider_name: str,
) -> None:
    """Every paid M4 adapter must attach a finite timeout to its actual request."""
    observed: list[dict[str, Any]] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        """Capture the request policy and return the smallest valid provider response."""
        timeout = _request.extensions.get("timeout")
        assert isinstance(timeout, dict)
        observed.append(timeout)
        payloads: dict[str, dict[str, Any]] = {
            "exa": {"results": []},
            "clay": {"routine_run_id": "run-1"},
            "apollo": {"person": None},
            "instantly": {"verification_status": "invalid"},
        }
        payload = payloads[provider_name]
        return httpx.Response(200, json=payload)

    contact = ContactRecord(
        contact_id="contact-1",
        company_id="cmp_acme",
        company_name="Acme Valve",
        company_domain="acme.com",
        company_final_score=1.0,
        full_name="Alex Acme",
        title="President",
        decision_rank=1,
        decision_reason="owner",
    )
    company = CompanyRecord(
        company_id="cmp_acme",
        name="Acme Valve",
        domain="acme.com",
        normalized_domain="acme.com",
    )
    with httpx.Client(transport=httpx.MockTransport(handler), timeout=None) as client:
        if provider_name == "exa":
            ExaPeopleProvider(api_key="test", client=client).search(company)
        elif provider_name == "clay":
            ClayContactProvider(api_key="test", routine_id="routine-1", client=client).start(
                [contact]
            )
        elif provider_name == "apollo":
            ApolloContactProvider(api_key="test", client=client).enrich(contact)
        else:
            InstantlyVerificationProvider(api_key="test", client=client).create(
                "alex@acme.com"
            )

    assert len(observed) == 1
    assert all(value is not None and float(value) > 0 for value in observed[0].values())


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
