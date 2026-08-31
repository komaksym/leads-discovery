"""Concrete M4 people-discovery provider built on the shared request boundary."""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

import httpx

from leads_discovery.discovery.base import (
    classify_http_status,
    provider_error,
    request_json,
    safe_transport_call,
)
from leads_discovery.models import CompanyRecord, UsageEvent

_EXA_SEARCH_URL = "https://api.exa.ai/search"
_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


@dataclass(slots=True)
class ExaPeopleResult:
    """Return bounded Exa People rows plus authoritative usage."""

    results: list[dict[str, Any]]
    usage_event: UsageEvent

    def __post_init__(self) -> None:
        self.results = deepcopy(self.results)
        self.usage_event = UsageEvent.from_dict(self.usage_event.to_dict())


class ExaPeopleProvider:
    """Search Exa's people category for one currently accepted company."""

    def __init__(self, *, api_key: str, client: httpx.Client) -> None:
        if not api_key.strip():
            raise ValueError("api_key must be nonempty")
        self._api_key = api_key
        self._client = client

    def search(self, company: CompanyRecord) -> ExaPeopleResult:
        """Execute one bounded people search through the shared safe transport."""
        metadata = {"company_id": company.company_id}
        request = self._client.build_request(
            "POST",
            _EXA_SEARCH_URL,
            headers={"x-api-key": self._api_key},
            json={
                "query": (
                    f"Current employees at {company.name} closest to buying operational software: "
                    "Owner, President, CEO, COO, Managing Partner, General Manager, senior Sales, "
                    "Operations, Commercial, Estimating, Inside Sales leaders, branch or regional managers"
                ),
                "category": "people",
                "type": "auto",
                "numResults": 10,
                "contents": {"highlights": True},
            },
            timeout=_REQUEST_TIMEOUT,
        )
        response = safe_transport_call(
            lambda: self._client.send(request, stream=True),
            provider="exa",
            request_id=company.company_id,
            operation="people_search",
            request_count=1,
        )
        status_code = response.status_code
        if not 200 <= status_code < 300:
            response.close()
            kind, retryable = classify_http_status(status_code)
            raise provider_error(
                provider="exa",
                request_id=company.company_id,
                operation="people_search",
                request_count=1,
                kind=kind,
                retryable=retryable,
                status_code=status_code,
                metadata=metadata,
            ) from None
        payload = request_json(
            response,
            provider="exa",
            request_id=company.company_id,
            operation="people_search",
            request_count=1,
        )
        raw_results = payload.get("results")
        if not isinstance(raw_results, list) or any(
            not isinstance(item, dict) for item in raw_results[:10]
        ):
            raise provider_error(
                provider="exa",
                request_id=company.company_id,
                operation="people_search",
                request_count=1,
                kind="invalid_response",
                retryable=False,
                status_code=status_code,
                metadata=metadata,
            ) from None
        results = [deepcopy(cast(dict[str, Any], item)) for item in raw_results[:10]]

        estimated: float | None = None
        raw_cost = payload.get("costDollars")
        if raw_cost is not None:
            if not isinstance(raw_cost, dict):
                raise provider_error(
                    provider="exa",
                    request_id=company.company_id,
                    operation="people_search",
                    request_count=1,
                    kind="invalid_response",
                    retryable=False,
                    status_code=status_code,
                    metadata=metadata,
                ) from None
            total = raw_cost.get("total")
            if total is not None:
                if (
                    isinstance(total, bool)
                    or not isinstance(total, (int, float))
                    or not math.isfinite(total)
                    or total < 0
                ):
                    raise provider_error(
                        provider="exa",
                        request_id=company.company_id,
                        operation="people_search",
                        request_count=1,
                        kind="invalid_response",
                        retryable=False,
                        status_code=status_code,
                        metadata=metadata,
                    ) from None
                estimated = float(total)

        return ExaPeopleResult(
            results=results,
            usage_event=UsageEvent(
                provider="exa",
                operation="people_search",
                request_count=1,
                estimated_cost_usd=estimated,
                metadata={**metadata, "result_count": len(results)},
            ),
        )


__all__ = ["ExaPeopleProvider", "ExaPeopleResult"]
