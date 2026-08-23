"""One-run capped and resumable Apify Google Maps discovery adapter."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, cast

import httpx

from leads_discovery.discovery.base import (
    classify_http_status,
    provider_error,
    request_json,
    safe_transport_call,
    stable_raw_record_id,
    utc_timestamp,
    validate_common_request,
    validation_error,
)
from leads_discovery.models import DiscoveryBatch, DiscoveryRecord, DiscoveryRequest, UsageEvent

_START_URL = "https://api.apify.com/v2/acts/compass~crawler-google-places/runs"
_RUN_URL = "https://api.apify.com/v2/actor-runs/{run_id}"
_DATASET_URL = "https://api.apify.com/v2/datasets/{dataset_id}/items"
_NONTERMINAL = {"READY", "RUNNING"}
_TERMINAL_ERROR = {"FAILED", "TIMED-OUT", "ABORTED"}


class ApifyDiscoveryProvider:
    """Run at most one explicitly capped Apify Google Maps Actor request."""

    def __init__(
        self,
        *,
        api_token: str,
        client: httpx.Client,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_run_started: Callable[[str], None] | None = None,
    ) -> None:
        """Store injected dependencies without owning clients or loading environment values."""
        if not api_token.strip():
            raise ValueError("api_token must be nonempty")
        self._api_token = api_token
        self._client = client
        self._monotonic = monotonic
        self._sleep = sleep
        self._on_run_started = on_run_started

    def search(self, request: DiscoveryRequest) -> DiscoveryBatch:
        """Start one capped Actor run and poll/fetch only that run."""
        self._validate(request)
        body = self._actor_input(request)
        response = safe_transport_call(
            lambda: self._client.post(
                _START_URL,
                headers={"Authorization": f"Bearer {self._api_token}"},
                params={
                    "waitForFinish": 60,
                    "maxItems": request.max_results_total,
                    "maxTotalChargeUsd": request.max_cost_usd,
                },
                json=body,
            ),
            provider="apify",
            request_id=request.request_id,
            operation="google_maps_search",
            request_count=1,
        )
        if not 200 <= response.status_code < 300:
            kind, retryable = classify_http_status(response.status_code)
            raise provider_error(
                provider="apify",
                request_id=request.request_id,
                operation="google_maps_search",
                request_count=1,
                kind=kind,
                retryable=retryable,
                status_code=response.status_code,
            ) from None
        payload = request_json(
            response,
            provider="apify",
            request_id=request.request_id,
            operation="google_maps_search",
            request_count=1,
        )
        data = self._run_data(payload, request, 1)
        run_id = _required_str(data.get("id"), request, 1)
        if self._on_run_started is not None:
            self._on_run_started(run_id)
        return self._poll_and_fetch(request, run_id, initial_data=data, request_count=1)

    def resume(self, request: DiscoveryRequest, run_id: str) -> DiscoveryBatch:
        """Resume polling/fetching the same persisted Actor run without starting a replacement."""
        self._validate(request)
        if not run_id.strip():
            raise validation_error(
                provider="apify",
                request_id=request.request_id,
                message="run_id must be nonempty",
                operation="google_maps_search",
            ) from None
        return self._poll_and_fetch(request, run_id, initial_data=None, request_count=0)

    @staticmethod
    def _validate(request: DiscoveryRequest) -> None:
        """Reject invalid Apify request scope or caps before any HTTP work."""
        try:
            validate_common_request(request, "apify")
            if len(request.queries) != 3:
                raise ValueError("Apify Maps discovery requires exactly three queries")
            if request.max_cost_usd is None or not 0 < request.max_cost_usd <= 1:
                raise ValueError("Apify max_cost_usd must be in (0, 1]")
        except ValueError as exc:
            raise validation_error(
                provider="apify",
                request_id=request.request_id,
                message=str(exc),
                operation="google_maps_search",
            ) from None

    @staticmethod
    def _actor_input(request: DiscoveryRequest) -> dict[str, Any]:
        """Build the exact minimal Actor input with all enrichment features disabled."""
        return {
            "searchStringsArray": list(request.queries),
            "countryCode": request.target_country_code.lower(),
            "language": "en",
            "maxCrawledPlacesPerSearch": request.max_results_per_query,
            "website": "allPlaces",
            "skipClosedPlaces": False,
            "scrapePlaceDetailPage": False,
            "includeWebResults": False,
            "scrapeDirectories": False,
            "scrapeContacts": False,
            "scrapeSocialMediaProfiles": {
                "facebooks": False,
                "instagrams": False,
                "youtubes": False,
                "tiktoks": False,
                "twitters": False,
            },
            "maximumLeadsEnrichmentRecords": 0,
            "maxReviews": 0,
            "maxImages": 0,
            "maxCompetitorsToAnalyze": 0,
            "verifyLeadsEnrichmentEmails": False,
            "scrapeReviewsPersonalData": False,
            "enableCompetitorAnalysis": False,
        }

    def _poll_and_fetch(
        self,
        request: DiscoveryRequest,
        run_id: str,
        *,
        initial_data: dict[str, Any] | None,
        request_count: int,
    ) -> DiscoveryBatch:
        """Poll one persisted run to a five-minute local deadline and fetch its default dataset."""
        deadline = self._monotonic() + 300.0
        data = initial_data
        backoff = 1.0
        while True:
            if data is None:
                if self._monotonic() >= deadline:
                    raise provider_error(
                        provider="apify",
                        request_id=request.request_id,
                        operation="google_maps_search",
                        request_count=request_count,
                        kind="transient",
                        retryable=True,
                        metadata={
                            "request_id": request.request_id,
                            "run_id": run_id,
                            "status": "RUNNING",
                        },
                    ) from None
                request_count += 1
                response = safe_transport_call(
                    lambda: self._client.get(
                        _RUN_URL.format(run_id=run_id),
                        headers={"Authorization": f"Bearer {self._api_token}"},
                    ),
                    provider="apify",
                    request_id=request.request_id,
                    operation="google_maps_search",
                    request_count=request_count,
                )
                if not 200 <= response.status_code < 300:
                    kind, retryable = classify_http_status(response.status_code)
                    raise provider_error(
                        provider="apify",
                        request_id=request.request_id,
                        operation="google_maps_search",
                        request_count=request_count,
                        kind=kind,
                        retryable=retryable,
                        status_code=response.status_code,
                        metadata={"request_id": request.request_id, "run_id": run_id},
                    ) from None
                payload = request_json(
                    response,
                    provider="apify",
                    request_id=request.request_id,
                    operation="google_maps_search",
                    request_count=request_count,
                )
                data = self._run_data(payload, request, request_count)
            status = _required_str(data.get("status"), request, request_count).upper()
            if status == "SUCCEEDED":
                break
            if status in _TERMINAL_ERROR:
                raise provider_error(
                    provider="apify",
                    request_id=request.request_id,
                    operation="google_maps_search",
                    request_count=request_count,
                    kind="permanent",
                    retryable=False,
                    metadata={"request_id": request.request_id, "run_id": run_id, "status": status},
                ) from None
            if status not in _NONTERMINAL:
                raise provider_error(
                    provider="apify",
                    request_id=request.request_id,
                    operation="google_maps_search",
                    request_count=request_count,
                    kind="invalid_response",
                    retryable=False,
                    metadata={"request_id": request.request_id, "run_id": run_id},
                ) from None
            if self._monotonic() >= deadline:
                raise provider_error(
                    provider="apify",
                    request_id=request.request_id,
                    operation="google_maps_search",
                    request_count=request_count,
                    kind="transient",
                    retryable=True,
                    metadata={"request_id": request.request_id, "run_id": run_id, "status": status},
                ) from None
            self._sleep(backoff)
            backoff = min(backoff * 2.0, 10.0)
            data = None

        dataset_id = _required_str(data.get("defaultDatasetId"), request, request_count)
        request_count += 1
        dataset_response = safe_transport_call(
            lambda: self._client.get(
                _DATASET_URL.format(dataset_id=dataset_id),
                headers={"Authorization": f"Bearer {self._api_token}"},
                params={"clean": "true"},
            ),
            provider="apify",
            request_id=request.request_id,
            operation="google_maps_search",
            request_count=request_count,
        )
        if not 200 <= dataset_response.status_code < 300:
            kind, retryable = classify_http_status(dataset_response.status_code)
            raise provider_error(
                provider="apify",
                request_id=request.request_id,
                operation="google_maps_search",
                request_count=request_count,
                kind=kind,
                retryable=retryable,
                status_code=dataset_response.status_code,
                metadata={"request_id": request.request_id, "run_id": run_id},
            ) from None
        try:
            raw_rows = dataset_response.json()
        except Exception:
            raise provider_error(
                provider="apify",
                request_id=request.request_id,
                operation="google_maps_search",
                request_count=request_count,
                kind="invalid_response",
                retryable=False,
                status_code=dataset_response.status_code,
                metadata={"request_id": request.request_id, "run_id": run_id},
            ) from None
        if not isinstance(raw_rows, list) or any(not isinstance(row, dict) for row in raw_rows):
            raise provider_error(
                provider="apify",
                request_id=request.request_id,
                operation="google_maps_search",
                request_count=request_count,
                kind="invalid_response",
                retryable=False,
                status_code=dataset_response.status_code,
                metadata={"request_id": request.request_id, "run_id": run_id},
            ) from None

        retrieved_at = utc_timestamp()
        records = [
            self._parse_record(request, cast(dict[str, Any], row), retrieved_at)
            for row in raw_rows[: request.max_results_total]
        ]
        estimated = _run_cost(data, request, request_count, run_id)
        usage = UsageEvent(
            provider="apify",
            operation="google_maps_search",
            request_count=request_count,
            estimated_cost_usd=estimated,
            metadata={
                "request_id": request.request_id,
                "run_id": run_id,
                "status": "SUCCEEDED",
                "result_count": len(records),
            },
        )
        return DiscoveryBatch(request=request, records=records, usage_events=[usage])

    @staticmethod
    def _run_data(
        payload: dict[str, Any], request: DiscoveryRequest, request_count: int
    ) -> dict[str, Any]:
        """Validate Apify's required run-envelope object."""
        data = payload.get("data")
        if not isinstance(data, dict):
            raise provider_error(
                provider="apify",
                request_id=request.request_id,
                operation="google_maps_search",
                request_count=request_count,
                kind="invalid_response",
                retryable=False,
            ) from None
        return cast(dict[str, Any], data)

    @staticmethod
    def _parse_record(
        request: DiscoveryRequest, raw: dict[str, Any], retrieved_at: str
    ) -> DiscoveryRecord:
        """Map one Google Maps row without inferring missing returned geography."""
        provider_result_id = _optional_str(raw.get("placeId")) or _optional_str(raw.get("cid"))
        source_url = _optional_str(raw.get("url")) or _optional_str(raw.get("googleMapsUrl"))
        name = _optional_str(raw.get("title"))
        website_url = _optional_str(raw.get("website"))
        city = _optional_str(raw.get("city"))
        region = _optional_str(raw.get("state")) or _optional_str(raw.get("region"))
        postal_code = _optional_str(raw.get("postalCode"))
        country_code = _optional_str(raw.get("countryCode"))
        query = _optional_str(raw.get("searchString"))
        snippet = _optional_str(raw.get("description")) or _optional_str(raw.get("categoryName"))
        parsed_identity = {
            "name": name,
            "website_url": website_url,
            "city": city,
            "region": region,
            "postal_code": postal_code,
            "country_code": country_code,
        }
        record_id = stable_raw_record_id(
            provider="apify",
            request=request,
            provider_result_id=provider_result_id,
            parsed_identity=parsed_identity,
            raw_metadata=raw,
        )
        return DiscoveryRecord(
            record_id=record_id,
            provider="apify",
            request_id=request.request_id,
            target_country_code=request.target_country_code,
            query=query,
            provider_result_id=provider_result_id,
            name=name,
            source_url=source_url,
            website_url=website_url,
            city=city,
            region=region,
            postal_code=postal_code,
            country_code=country_code,
            title=name,
            snippet=snippet,
            raw_metadata=raw,
            retrieved_at=retrieved_at,
        )


def _required_str(value: Any, request: DiscoveryRequest, request_count: int) -> str:
    """Return a required provider string or raise a sanitized invalid response."""
    if not isinstance(value, str) or not value:
        raise provider_error(
            provider="apify",
            request_id=request.request_id,
            operation="google_maps_search",
            request_count=request_count,
            kind="invalid_response",
            retryable=False,
        ) from None
    return value


def _optional_str(value: Any) -> str | None:
    """Return a provider string value or None without fabricating data."""
    return value if isinstance(value, str) and value else None


def _run_cost(
    data: dict[str, Any],
    request: DiscoveryRequest,
    request_count: int,
    run_id: str,
) -> float | None:
    """Read authenticated Apify spend or reject malformed present cost metadata."""
    value = data.get("usageTotalUsd")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise provider_error(
            provider="apify",
            request_id=request.request_id,
            operation="google_maps_search",
            request_count=request_count,
            kind="invalid_response",
            retryable=False,
            metadata={"request_id": request.request_id, "run_id": run_id},
        ) from None
    return float(value)
