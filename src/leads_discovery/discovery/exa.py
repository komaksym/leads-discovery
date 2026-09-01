"""Thin bounded Exa company-discovery HTTP adapter."""

from __future__ import annotations

import math
from typing import Any, cast

import httpx

from leads_discovery.discovery.base import (
    ProviderRequestContext,
    provider_error,
    request_json_at_boundary,
    stable_raw_record_id,
    utc_timestamp,
    validate_common_request,
    validation_error,
)
from leads_discovery.models import DiscoveryBatch, DiscoveryRecord, DiscoveryRequest, UsageEvent

_EXA_SEARCH_URL = "https://api.exa.ai/search"
_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class ExaDiscoveryProvider:
    """Translate one bounded discovery request to Exa's company search endpoint."""

    def __init__(self, *, api_key: str, client: httpx.Client) -> None:
        """Store a nonempty Exa credential and caller-owned injected HTTP client."""
        if not api_key.strip():
            raise ValueError("api_key must be nonempty")
        self._api_key = api_key
        self._client = client

    def search(self, request: DiscoveryRequest) -> DiscoveryBatch:
        """Execute exactly one Exa company-search request without retries."""
        try:
            validate_common_request(request, "exa")
            if len(request.queries) != 1:
                raise ValueError("Exa discovery requires exactly one query")
            if request.max_results_per_query != request.max_results_total:
                raise ValueError("Exa per-query and total caps must match")
            if request.max_cost_usd is not None:
                raise ValueError("Exa discovery requests do not carry an Apify cost cap")
        except ValueError as exc:
            raise validation_error(
                provider="exa",
                request_id=request.request_id,
                message=str(exc),
                operation="company_search",
            ) from None

        body = {
            "query": request.queries[0],
            "category": "company",
            "type": "auto",
            "numResults": request.max_results_total,
            "userLocation": request.target_country_code,
            "contents": {"highlights": True},
        }
        http_request = self._client.build_request(
            "POST",
            _EXA_SEARCH_URL,
            headers={"x-api-key": self._api_key},
            json=body,
            timeout=_REQUEST_TIMEOUT,
        )
        context = ProviderRequestContext("exa", request.request_id, "company_search", 1)
        payload_raw, status_code = request_json_at_boundary(
            self._client,
            http_request,
            context=context,
        )
        if not isinstance(payload_raw, dict):
            raise context.error(
                kind="invalid_response",
                retryable=False,
                status_code=status_code,
            ) from None
        payload = cast(dict[str, Any], payload_raw)
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise provider_error(
                provider="exa",
                request_id=request.request_id,
                operation="company_search",
                request_count=1,
                kind="invalid_response",
                retryable=False,
                status_code=status_code,
            ) from None

        retrieved_at = utc_timestamp()
        records: list[DiscoveryRecord] = []
        for raw in raw_results[: request.max_results_total]:
            if not isinstance(raw, dict):
                raise provider_error(
                    provider="exa",
                    request_id=request.request_id,
                    operation="company_search",
                    request_count=1,
                    kind="invalid_response",
                    retryable=False,
                    status_code=status_code,
                ) from None
            records.append(self._parse_record(request, raw, retrieved_at))

        estimated_cost = self._parse_cost(payload, request.request_id)
        usage = UsageEvent(
            provider="exa",
            operation="company_search",
            request_count=1,
            estimated_cost_usd=estimated_cost,
            metadata={
                "request_id": request.request_id,
                "query_family": request.query_family,
                "query": request.queries[0],
                "target_country_code": request.target_country_code,
                "result_count": len(records),
            },
        )
        return DiscoveryBatch(request=request, records=records, usage_events=[usage])

    @staticmethod
    def _parse_record(
        request: DiscoveryRequest, raw: dict[str, Any], retrieved_at: str
    ) -> DiscoveryRecord:
        """Map one Exa result while preserving the complete provider row."""
        entity = _first_company_entity(raw)
        properties_raw = entity.get("properties")
        properties = (
            cast(dict[str, Any], properties_raw)
            if isinstance(properties_raw, dict)
            else entity
        )
        headquarters = properties.get("headquarters")
        hq = cast(dict[str, Any], headquarters) if isinstance(headquarters, dict) else {}
        provider_result_id = (
            _optional_str(entity.get("id"))
            or _optional_str(properties.get("id"))
            or _optional_str(raw.get("id"))
        )
        title = _optional_str(raw.get("title"))
        name = _optional_str(properties.get("name")) or _optional_str(entity.get("name")) or title
        source_url = _optional_str(raw.get("url"))
        website_url = source_url
        highlights = raw.get("highlights")
        snippets = (
            [item for item in highlights if isinstance(item, str)]
            if isinstance(highlights, list)
            else []
        )
        snippet = "\n".join(snippets)[:2000] or None
        city = _optional_str(hq.get("city"))
        postal_code = _optional_str(hq.get("postalCode")) or _optional_str(hq.get("postal_code"))
        country_code = (
            _optional_str(hq.get("countryCode"))
            or _optional_str(hq.get("country_code"))
            or _optional_str(hq.get("country"))
        )
        parsed_identity: dict[str, Any] = {
            "name": name,
            "url": source_url,
            "city": city,
            "postal_code": postal_code,
            "country_code": country_code,
        }
        record_id = stable_raw_record_id(
            provider="exa",
            request=request,
            provider_result_id=provider_result_id,
            parsed_identity=parsed_identity,
            raw_metadata=raw,
        )
        return DiscoveryRecord(
            record_id=record_id,
            provider="exa",
            request_id=request.request_id,
            target_country_code=request.target_country_code,
            query=request.queries[0],
            provider_result_id=provider_result_id,
            name=name,
            source_url=source_url,
            website_url=website_url,
            city=city,
            region=None,
            postal_code=postal_code,
            country_code=country_code,
            title=title,
            snippet=snippet,
            raw_metadata=raw,
            retrieved_at=retrieved_at,
        )

    @staticmethod
    def _parse_cost(payload: dict[str, Any], request_id: str) -> float | None:
        """Read finite nonnegative authenticated Exa cost metadata when present."""
        cost_dollars = payload.get("costDollars")
        if cost_dollars is None:
            return None
        if not isinstance(cost_dollars, dict):
            raise provider_error(
                provider="exa",
                request_id=request_id,
                operation="company_search",
                request_count=1,
                kind="invalid_response",
                retryable=False,
            ) from None
        total = cost_dollars.get("total")
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
                request_id=request_id,
                operation="company_search",
                request_count=1,
                kind="invalid_response",
                retryable=False,
            ) from None
        return float(total)


def _first_company_entity(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the first company entity, falling back to the first untyped entity when safe."""
    entities = raw.get("entities")
    first_untyped: dict[str, Any] | None = None
    if isinstance(entities, list):
        for candidate in entities:
            if not isinstance(candidate, dict):
                continue
            entity = cast(dict[str, Any], candidate)
            kind = entity.get("type") or entity.get("entityType")
            if isinstance(kind, str) and kind.casefold() == "company":
                return entity
            if kind is None and first_untyped is None:
                first_untyped = entity
        if first_untyped is not None:
            return first_untyped
    raw_entity = raw.get("entity")
    return raw_entity if isinstance(raw_entity, dict) else {}


def _optional_str(value: Any) -> str | None:
    """Return a provider string value or None without fabricating data."""
    return value if isinstance(value, str) and value else None
