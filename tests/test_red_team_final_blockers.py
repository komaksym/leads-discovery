"""Final adversarial attacks for the three remaining production-readiness blockers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from leads_discovery.discovery.base import DiscoveryProviderError, request_json
from leads_discovery.discovery.exa import ExaDiscoveryProvider
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

_EVIDENCE_ID = "ev_red_team_final"


class _TrackedStream(httpx.SyncByteStream):
    """Yield controlled chunks while exposing exactly how far the consumer read."""

    def __init__(self, chunks: list[bytes]) -> None:
        """Store chunks and initialize the consumption counter."""
        self._chunks = chunks
        self.chunks_consumed = 0

    def __iter__(self) -> Iterator[bytes]:
        """Yield each chunk lazily and count it before control returns to the caller."""
        for chunk in self._chunks:
            self.chunks_consumed += 1
            yield chunk


class _UnreadableStream(httpx.SyncByteStream):
    """Fail if an oversized Content-Length response body is touched at all."""

    def __init__(self) -> None:
        """Initialize the read counter."""
        self.chunks_consumed = 0

    def __iter__(self) -> Iterator[bytes]:
        """Reject the first attempted body read."""
        self.chunks_consumed += 1
        raise AssertionError("declared-oversized response body was consumed")
        yield b""  # pragma: no cover


def _company() -> CompanyRecord:
    """Build one company for extraction-to-evaluation negative-evidence attacks."""
    return CompanyRecord(
        company_id="cmp_red_team_final",
        name="Red Team Industrial",
        normalized_name="red team industrial",
        domain="red-team.example",
        normalized_domain="red-team.example",
        country="US",
    )


def _bundle(excerpt: str) -> EvidenceBundle:
    """Build one cited evidence item containing the supplied public-text excerpt."""
    return EvidenceBundle(
        company_id="cmp_red_team_final",
        items=[
            EvidenceItem(
                evidence_id=_EVIDENCE_ID,
                url="https://red-team.example/about",
                title="About Red Team Industrial",
                excerpt=excerpt,
                provider="exa",
            )
        ],
        raw_records=[],
        usage_events=[],
    )


def _negative_result() -> ExtractionResult:
    """Build one high-confidence model claim that PVF relevance is false."""
    facts = {key: ExtractedFact(None, 0.0, []) for key in FACT_KEYS}
    facts["pvf_relevant"] = ExtractedFact(False, 0.99, [_EVIDENCE_ID])
    return ExtractionResult(
        company_id="cmp_red_team_final",
        model="deepseek-v4-flash",
        facts=facts,
        usage_event=UsageEvent(provider="deepseek", operation="structured_extraction"),
    )


def _evaluate_negative_claim(excerpt: str) -> CompanyRecord:
    """Run the claimed negative fact through extraction filtering and final evaluation."""
    extracted = apply_extraction(_company(), _bundle(excerpt), _negative_result())
    return evaluate_company(extracted)


@pytest.mark.parametrize(
    "excerpt",
    [
        (
            "We do not sell electrical equipment. "
            "We distribute industrial pipe, valves, and fittings."
        ),
        "We are not a manufacturer. We distribute industrial valves.",
        (
            "We do not sell electrical equipment; however, "
            "we distribute industrial pipe, valves, and fittings."
        ),
        (
            "We do not sell electrical equipment:\n"
            "we distribute industrial pipe, valves, and fittings."
        ),
        "We do not install valves. We distribute industrial valves.",
        "We do not manufacture valves. We distribute industrial valves.",
        (
            "We do not manufacture pipe. "
            "We distribute industrial pipe, valves, and fittings."
        ),
        (
            "We do not install pipe. "
            "We distribute industrial pipe, valves, and fittings."
        ),
    ],
)
def test_unrelated_negative_semantics_cannot_hard_reject(excerpt: str) -> None:
    """Negation unrelated to PVF selling/distribution cannot validate pvf_relevant=false."""
    result = _evaluate_negative_claim(excerpt)

    assert result.features["pvf_relevant"] is None
    assert result.final_decision != "rejected"
    assert "confirmed_not_pvf_relevant" not in result.rejection_reasons


@pytest.mark.parametrize(
    "excerpt",
    [
        "We do not sell or distribute pipe, valves, or fittings.",
        "Our company does not offer piping products.",
    ],
)
def test_genuine_negative_semantics_still_support_rejection(excerpt: str) -> None:
    """Direct negation of PVF selling/distribution remains usable hard-negative evidence."""
    result = _evaluate_negative_claim(excerpt)

    assert result.features["pvf_relevant"] is False
    assert result.final_decision == "rejected"
    assert "confirmed_not_pvf_relevant" in result.rejection_reasons


def _exa_request() -> DiscoveryRequest:
    """Build one valid minimal Exa company-search request."""
    return DiscoveryRequest(
        request_id="exa:red-team-final:v1",
        provider="exa",
        query_family="core-pvf",
        target_country_code="US",
        queries=("industrial PVF distributors",),
        max_results_per_query=1,
        max_results_total=1,
        max_cost_usd=None,
    )


def test_stream_bound_rejects_oversized_content_length_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declared oversize must fail before any provider-controlled body byte is consumed."""
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "8")
    stream = _UnreadableStream()

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return a declared-oversized response whose body must remain unread."""
        return httpx.Response(200, headers={"Content-Length": "9"}, stream=stream)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(DiscoveryProviderError),
    ):
        ExaDiscoveryProvider(api_key="test", client=client).search(_exa_request())

    assert stream.chunks_consumed == 0


def test_stream_bound_stops_many_small_chunks_at_crossing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunked no-Length input must stop on the first chunk that crosses the ceiling."""
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "5")
    stream = _TrackedStream([b"12", b"34", b"56", b"78", b"90"])

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return five lazy chunks without a Content-Length header."""
        return httpx.Response(200, stream=stream)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(DiscoveryProviderError),
    ):
        ExaDiscoveryProvider(api_key="test", client=client).search(_exa_request())

    assert stream.chunks_consumed == 3


def test_stream_bound_stops_after_one_very_large_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single crossing chunk must not cause any later provider chunks to be consumed."""
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "8")
    stream = _TrackedStream([b"x" * 1024, b"must-not-be-read"])

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return one huge first chunk followed by a sentinel second chunk."""
        return httpx.Response(200, stream=stream)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(DiscoveryProviderError),
    ):
        ExaDiscoveryProvider(api_key="test", client=client).search(_exa_request())

    assert stream.chunks_consumed == 1


def test_json_oversize_near_end_stops_before_sentinel_and_sanitizes_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Late oversize must stop before later chunks and never expose body text in errors."""
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "24")
    stream = _TrackedStream(
        [
            b'{"results":[',
            b'"1234567890"',
            b",",
            b"secret-provider-body",
            b"]}",
            b"sentinel",
        ]
    )
    response = httpx.Response(200, stream=stream)

    with pytest.raises(DiscoveryProviderError) as captured:
        request_json(
            response,
            provider="exa",
            request_id="red-team-near-end",
            operation="company_search",
            request_count=1,
        )

    assert stream.chunks_consumed == 3
    assert "secret-provider-body" not in str(captured.value)
    assert "secret-provider-body" not in repr(captured.value.usage_event.to_dict())


def test_bounded_streamed_json_still_parses_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal JSON below the bound must preserve existing provider behavior."""
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "64")
    stream = _TrackedStream([b'{"results":', b"[]}"])

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return one small valid streamed Exa payload."""
        return httpx.Response(200, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        batch = ExaDiscoveryProvider(api_key="test", client=client).search(_exa_request())

    assert batch.records == []
    assert stream.chunks_consumed == 2


def test_exa_timeout_is_provider_owned_with_generic_injected_client() -> None:
    """A caller client with no timeout policy must still receive explicit Exa request timeouts."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Capture request extensions at the real provider boundary."""
        timeout = request.extensions.get("timeout")
        assert isinstance(timeout, dict)
        seen.append(timeout)
        return httpx.Response(200, json={"results": []})

    with httpx.Client(transport=httpx.MockTransport(handler), timeout=None) as client:
        batch = ExaDiscoveryProvider(api_key="test", client=client).search(_exa_request())

    assert batch.records == []
    assert len(seen) == 1
    assert all(value is not None and float(value) > 0 for value in seen[0].values())
    assert seen[0]["connect"] < seen[0]["read"]
