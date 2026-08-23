"""Deterministic Exa evidence research and pure bounded prompt-bundle construction."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import httpx

from leads_discovery.dedup import _registrable_http_domain
from leads_discovery.discovery.base import (
    classify_http_status,
    provider_error,
    safe_transport_call,
    utc_timestamp,
)
from leads_discovery.models import (
    CompanyRecord,
    EvidenceBundle,
    EvidenceItem,
    ResearchRequest,
    UsageEvent,
)

_EXA_SEARCH_URL = "https://api.exa.ai/search"
_RESEARCH_FAMILIES = (
    (
        "company-profile",
        '"{name}" {domain} pipe valves fittings products industries locations line card',
    ),
    (
        "quotation-workload",
        '"{name}" {domain} RFQ quote quotation BOM estimating project tender inside sales',
    ),
    (
        "economic-incumbent-pain",
        '"{name}" {domain} employees branches revenue automation ERP ecommerce quote software '
        "competitor manual workflow",
    ),
)


def select_research_companies(
    companies: Iterable[CompanyRecord], *, limit: int = 20
) -> tuple[CompanyRecord, ...]:
    """Select at most 20 in-scope companies using deterministic spend-priority ordering."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("limit must be in 1..20")
    eligible = [
        company
        for company in companies
        if "OUTSIDE_GEOGRAPHY" not in company.review_reasons
        and company.country in {None, "US", "CA"}
    ]
    eligible.sort(
        key=lambda company: (
            (company.normalized_domain or company.domain) is None,
            company.country not in {"US", "CA"},
            -len(set(company.discovery_sources)),
            -len(company.discovery_records),
            company.company_id,
        )
    )
    return tuple(eligible[:limit])


def build_research_requests(company: CompanyRecord) -> tuple[ResearchRequest, ...]:
    """Build the exact three bounded Exa research requests in frozen catalog order."""
    name = company.name.strip()
    domain = company.normalized_domain or company.domain or ""
    requests = []
    for family, template in _RESEARCH_FAMILIES:
        query = re.sub(r"\s+", " ", template.format(name=name, domain=domain)).strip()
        requests.append(
            ResearchRequest(
                request_id=f"exa:{company.company_id}:{family}:v1",
                company_id=company.company_id,
                query_family=family,
                query=query,
                max_results=5,
            )
        )
    return tuple(requests)


def _normalize_http_url(url: str) -> str | None:
    """Normalize an HTTP(S) URL for deterministic evidence identity and exact deduplication."""
    try:
        parsed = urlsplit(url.strip())
        if parsed.scheme.casefold() not in {"http", "https"} or parsed.hostname is None:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port
        host = parsed.hostname.encode("idna").decode("ascii").casefold()
    except (ValueError, UnicodeError):
        return None
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.casefold(), netloc, path, parsed.query, ""))


def _evidence_id(normalized_url: str, excerpt: str | None) -> str:
    """Build the frozen stable Exa evidence ID from provider, normalized URL, and excerpt."""
    identity = json.dumps(
        ["exa", normalized_url, excerpt],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "ev_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def build_evidence_bundle(
    *,
    company: CompanyRecord,
    items: Iterable[EvidenceItem],
    raw_records: Iterable[dict[str, Any]],
    usage_events: Iterable[UsageEvent],
) -> EvidenceBundle:
    """Purely deduplicate and bound evidence while preserving complete raw rows separately."""
    own_domain = company.normalized_domain or company.domain
    seen_urls: set[str] = set()
    domain_counts: dict[str, int] = defaultdict(int)
    bounded: list[EvidenceItem] = []
    excerpt_chars = 0
    for source in items:
        normalized_url = _normalize_http_url(source.url)
        if normalized_url is None or normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        source_domain = _registrable_http_domain(source.url)
        domain_key = source_domain or (urlsplit(normalized_url).hostname or normalized_url)
        domain_limit = 4 if own_domain is not None and source_domain == own_domain else 2
        if domain_counts[domain_key] >= domain_limit:
            continue
        excerpt = source.excerpt[:2000] if source.excerpt is not None else None
        remaining = 20000 - excerpt_chars
        if excerpt is not None and len(excerpt) > remaining:
            excerpt = excerpt[:remaining]
        item = EvidenceItem(
            evidence_id=source.evidence_id,
            url=source.url,
            title=source.title,
            excerpt=excerpt,
            source_type=source.source_type,
            provider=source.provider,
            retrieved_at=source.retrieved_at,
        )
        bounded.append(item)
        domain_counts[domain_key] += 1
        excerpt_chars += len(excerpt or "")
        if len(bounded) >= 12 or excerpt_chars >= 20000:
            break
    return EvidenceBundle(
        company_id=company.company_id,
        items=bounded,
        raw_records=[dict(row) for row in raw_records],
        usage_events=list(usage_events),
    )


class ExaEvidenceResearcher:
    """Collect exactly three bounded Exa searches and retain raw rows outside model input."""

    def __init__(self, *, api_key: str, client: httpx.Client) -> None:
        """Store a nonempty Exa credential and caller-owned injected HTTP client."""
        if not api_key.strip():
            raise ValueError("api_key must be nonempty")
        self._api_key = api_key
        self._client = client

    def research(
        self,
        company: CompanyRecord,
        *,
        on_progress: Callable[[EvidenceBundle], None] | None = None,
    ) -> EvidenceBundle:
        """Execute all three searches and optionally report each successful call before the next."""
        return self._research_from(company, start_index=0, on_progress=on_progress)

    def _research_from(
        self,
        company: CompanyRecord,
        *,
        start_index: int,
        on_progress: Callable[[EvidenceBundle], None] | None,
    ) -> EvidenceBundle:
        """Execute unfinished searches from a validated zero-based catalog position."""
        requests = build_research_requests(company)
        if (
            isinstance(start_index, bool)
            or not isinstance(start_index, int)
            or not 0 <= start_index <= len(requests)
        ):
            raise ValueError("start_index must identify a bounded research request position")
        retrieved_at = utc_timestamp()
        raw_records: list[dict[str, Any]] = []
        items: list[EvidenceItem] = []
        costs: list[float | None] = []
        result_counts: list[int] = []
        attempted = 0
        for request in requests[start_index:]:
            attempted += 1
            attempted_position = start_index + attempted
            delta_request_count = 1 if on_progress is not None else attempted
            response = safe_transport_call(
                lambda request=request: self._client.post(
                    _EXA_SEARCH_URL,
                    headers={"x-api-key": self._api_key},
                    json={
                        "query": request.query,
                        "type": "auto",
                        "numResults": request.max_results,
                        "contents": {"highlights": True},
                    },
                ),
                provider="exa",
                request_id=request.request_id,
                operation="company_research",
                request_count=delta_request_count,
            )
            if not 200 <= response.status_code < 300:
                kind, retryable = classify_http_status(response.status_code)
                raise provider_error(
                    provider="exa",
                    request_id=request.request_id,
                    operation="company_research",
                    request_count=delta_request_count,
                    kind=kind,
                    retryable=retryable,
                    status_code=response.status_code,
                    metadata={
                        "company_id": company.company_id,
                        "attempted_requests": attempted_position,
                    },
                ) from None
            try:
                payload_raw = response.json()
            except Exception:
                raise provider_error(
                    provider="exa",
                    request_id=request.request_id,
                    operation="company_research",
                    request_count=delta_request_count,
                    kind="invalid_response",
                    retryable=False,
                    status_code=response.status_code,
                    metadata={
                        "company_id": company.company_id,
                        "attempted_requests": attempted_position,
                    },
                ) from None
            if not isinstance(payload_raw, dict) or not isinstance(
                payload_raw.get("results"), list
            ):
                raise provider_error(
                    provider="exa",
                    request_id=request.request_id,
                    operation="company_research",
                    request_count=delta_request_count,
                    kind="invalid_response",
                    retryable=False,
                    status_code=response.status_code,
                    metadata={
                        "company_id": company.company_id,
                        "attempted_requests": attempted_position,
                    },
                ) from None
            payload = cast(dict[str, Any], payload_raw)
            results = cast(list[Any], payload["results"])
            if any(not isinstance(row, dict) for row in results):
                raise provider_error(
                    provider="exa",
                    request_id=request.request_id,
                    operation="company_research",
                    request_count=delta_request_count,
                    kind="invalid_response",
                    retryable=False,
                    status_code=response.status_code,
                    metadata={
                        "company_id": company.company_id,
                        "attempted_requests": attempted_position,
                    },
                ) from None
            bounded_results = results[: request.max_results]
            call_raw_records = [cast(dict[str, Any], raw) for raw in bounded_results]
            call_items = [
                item
                for raw in call_raw_records
                if (item := self._to_evidence(raw, retrieved_at)) is not None
            ]
            call_cost = _exa_cost(
                payload,
                company.company_id,
                delta_request_count,
                attempted_position,
            )
            raw_records.extend(call_raw_records)
            items.extend(call_items)
            costs.append(call_cost)
            result_counts.append(len(call_raw_records))

            if on_progress is not None:
                delta_usage = UsageEvent(
                    provider="exa",
                    operation="company_research",
                    request_count=1,
                    estimated_cost_usd=call_cost,
                    metadata={
                        "company_id": company.company_id,
                        "request_id": request.request_id,
                        "query_family": request.query_family,
                        "result_count": len(call_raw_records),
                    },
                )
                on_progress(
                    build_evidence_bundle(
                        company=company,
                        items=call_items,
                        raw_records=call_raw_records,
                        usage_events=[delta_usage],
                    )
                )

        estimated = (
            sum(cost for cost in costs if cost is not None)
            if all(cost is not None for cost in costs)
            else None
        )
        usage = UsageEvent(
            provider="exa",
            operation="company_research",
            request_count=attempted,
            estimated_cost_usd=estimated,
            metadata={
                "company_id": company.company_id,
                "request_count": attempted,
                "result_counts": result_counts,
            },
        )
        return build_evidence_bundle(
            company=company,
            items=items,
            raw_records=raw_records,
            usage_events=[usage],
        )

    @staticmethod
    def _to_evidence(raw: dict[str, Any], retrieved_at: str) -> EvidenceItem | None:
        """Convert one valid Exa result into stable public evidence without invented excerpts."""
        url = raw.get("url")
        if not isinstance(url, str):
            return None
        normalized_url = _normalize_http_url(url)
        if normalized_url is None:
            return None
        highlights = raw.get("highlights")
        parts = (
            [part for part in highlights if isinstance(part, str)]
            if isinstance(highlights, list)
            else []
        )
        excerpt = "\n".join(parts) or None
        title = raw.get("title") if isinstance(raw.get("title"), str) else None
        return EvidenceItem(
            evidence_id=_evidence_id(normalized_url, excerpt),
            url=url,
            title=title,
            excerpt=excerpt,
            source_type="web",
            provider="exa",
            retrieved_at=retrieved_at,
        )


def _exa_cost(
    payload: dict[str, Any],
    company_id: str,
    request_count: int,
    attempted: int,
) -> float | None:
    """Read one finite nonnegative authenticated Exa research cost or reject malformed metadata."""
    cost = payload.get("costDollars")
    if cost is None:
        return None
    if not isinstance(cost, dict):
        raise provider_error(
            provider="exa",
            request_id=company_id,
            operation="company_research",
            request_count=request_count,
            kind="invalid_response",
            retryable=False,
            metadata={"company_id": company_id, "attempted_requests": attempted},
        ) from None
    total = cost.get("total")
    if total is None:
        return None
    if (
        isinstance(total, bool)
        or not isinstance(total, (int, float))
        or not math.isfinite(total)
        or total < 0
    ):
        raise provider_error(
            provider="exa",
            request_id=company_id,
            operation="company_research",
            request_count=request_count,
            kind="invalid_response",
            retryable=False,
            metadata={"company_id": company_id, "attempted_requests": attempted},
        ) from None
    return float(total)
