"""Regression tests for the final three production-readiness blockers."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

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
    ExtractedFact,
    ExtractionResult,
    UsageEvent,
)
from leads_discovery.research.extract import FACT_KEYS, apply_extraction

_EVIDENCE_ID = "ev_final_readiness_negative"


class _TrackedStream(httpx.SyncByteStream):
    """Expose exactly how many network-style chunks a bounded reader consumes."""

    def __init__(self, chunks: list[bytes]) -> None:
        """Store chunks and initialize a read counter."""
        self._chunks = chunks
        self.read_chunks = 0

    def __iter__(self) -> Any:
        """Yield chunks while recording each incremental consumption."""
        for chunk in self._chunks:
            self.read_chunks += 1
            yield chunk


def _company() -> CompanyRecord:
    """Build one company for negative-evidence regression tests."""
    return CompanyRecord(
        company_id="cmp_final_readiness",
        name="Acme Industrial",
        normalized_name="acme industrial",
        domain="acme.example",
        normalized_domain="acme.example",
        country="US",
    )


def _bundle(excerpt: str) -> EvidenceBundle:
    """Build one cited evidence item with the supplied excerpt."""
    return EvidenceBundle(
        company_id="cmp_final_readiness",
        items=[
            EvidenceItem(
                evidence_id=_EVIDENCE_ID,
                url="https://acme.example/about",
                title="About Acme",
                excerpt=excerpt,
                provider="exa",
            )
        ],
        raw_records=[],
        usage_events=[],
    )


def _negative_result() -> ExtractionResult:
    """Build a model result claiming the cited evidence proves PVF irrelevance."""
    facts = {key: ExtractedFact(None, 0.0, []) for key in FACT_KEYS}
    facts["pvf_relevant"] = ExtractedFact(False, 0.99, [_EVIDENCE_ID])
    return ExtractionResult(
        company_id="cmp_final_readiness",
        model="deepseek-v4-flash",
        facts=facts,
        usage_event=UsageEvent(provider="deepseek", operation="structured_extraction"),
    )


@pytest.mark.parametrize(
    "excerpt",
    [
        "We do not sell electrical equipment. We distribute pipe, valves, and fittings.",
        "We are not a manufacturer. We distribute industrial valves.",
        "We do not manufacture pipe. We distribute industrial pipe, valves, and fittings.",
        "We do not install pipe. We distribute industrial pipe, valves, and fittings.",
        "We do not serve residential customers. Our PVF products serve industrial facilities.",
    ],
)
def test_unrelated_negation_cannot_support_pvf_false(excerpt: str) -> None:
    """Negation elsewhere in a citation cannot manufacture a hard-negative PVF fact."""
    updated = apply_extraction(_company(), _bundle(excerpt), _negative_result())

    assert updated.features["pvf_relevant"] is None
    assert updated.feature_confidence["pvf_relevant"] == {
        "confidence": 0.0,
        "evidence_ids": [],
    }


@pytest.mark.parametrize(
    "excerpt",
    [
        "We do not sell pipe, valves, or fittings.",
        "Our company does not distribute PVF products.",
        "We exclusively sell electrical equipment and do not offer piping products.",
    ],
)
def test_locally_supported_pvf_false_is_preserved(excerpt: str) -> None:
    """Explicit local negation of the target concept remains valid negative evidence."""
    updated = apply_extraction(_company(), _bundle(excerpt), _negative_result())

    assert updated.features["pvf_relevant"] is False
    assert updated.feature_confidence["pvf_relevant"] == {
        "confidence": 0.99,
        "evidence_ids": [_EVIDENCE_ID],
    }


def test_declared_oversized_response_is_rejected_before_body_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized Content-Length fails before any body chunk is consumed."""
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "10")
    stream = _TrackedStream([b"x" * 100])
    response = httpx.Response(
        200,
        headers={"content-length": "11"},
        stream=stream,
    )

    with pytest.raises(ResponseTooLargeError):
        read_bounded_response(response)

    assert stream.read_chunks == 0


def test_chunked_response_stops_reading_when_running_limit_is_crossed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-length body aborts on the crossing chunk instead of buffering later chunks."""
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "5")
    stream = _TrackedStream([b"123", b"456", b"789"])
    response = httpx.Response(200, stream=stream)

    with pytest.raises(ResponseTooLargeError):
        read_bounded_response(response)

    assert stream.read_chunks == 2


def test_normal_streamed_json_still_decodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal JSON body remains compatible with the bounded streaming decoder."""
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "64")
    response = httpx.Response(200, stream=_TrackedStream([b'{"ok":', b"true}"]))

    assert request_json(
        response,
        provider="exa",
        request_id="normal",
        operation="search",
        request_count=1,
    ) == {"ok": True}


def _exa_request() -> DiscoveryRequest:
    """Build one valid minimal Exa discovery request."""
    return DiscoveryRequest(
        request_id="exa:final:v1",
        provider="exa",
        query_family="core-pvf",
        target_country_code="US",
        queries=("industrial PVF distributors",),
        max_results_per_query=1,
        max_results_total=1,
        max_cost_usd=None,
    )


def test_exa_provider_enforces_chunk_limit_while_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Exa adapter must not eagerly buffer chunks beyond the configured ceiling."""
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "5")
    stream = _TrackedStream([b'{"res', b'ults":', b"[]}"])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(DiscoveryProviderError) as captured,
    ):
        ExaDiscoveryProvider(api_key="test", client=client).search(_exa_request())

    assert captured.value.kind == "invalid_response"
    assert stream.read_chunks == 2


def test_exa_provider_applies_explicit_timeout_over_client_default() -> None:
    """Exa request timeout semantics do not depend on a specially configured caller client."""
    seen_timeout: dict[str, float | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        timeout = request.extensions.get("timeout")
        assert isinstance(timeout, dict)
        seen_timeout.update(timeout)
        return httpx.Response(200, json={"results": []})

    with httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=None,
    ) as client:
        batch = ExaDiscoveryProvider(api_key="test", client=client).search(_exa_request())

    assert batch.records == []
    assert seen_timeout == {
        "connect": 5.0,
        "read": 30.0,
        "write": 30.0,
        "pool": 30.0,
    }
