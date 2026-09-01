"""Shared contracts and sanitized helpers for synchronous discovery providers."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

import httpx

from leads_discovery.models import (
    DiscoveryBatch,
    DiscoveryProviderName,
    DiscoveryRequest,
    ErrorKind,
    UsageEvent,
)

_DEFAULT_MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024


class DiscoveryProvider(Protocol):
    """Define the synchronous discovery interface used by the M2 runner."""

    def search(self, request: DiscoveryRequest) -> DiscoveryBatch:
        """Execute one validated bounded discovery request."""
        ...


class DiscoveryProviderError(RuntimeError):
    """Expose a sanitized provider failure plus safe accounting metadata."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        kind: ErrorKind,
        request_id: str,
        retryable: bool,
        status_code: int | None,
        usage_event: UsageEvent,
    ) -> None:
        """Initialize a provider error without retaining unsafe response or request content."""
        super().__init__(message)
        self.provider = provider
        self.kind = kind
        self.request_id = request_id
        self.retryable = retryable
        self.status_code = status_code
        self.usage_event = UsageEvent.from_dict(usage_event.to_dict())


@dataclass(frozen=True, slots=True)
class ProviderRequestContext:
    """Carry stable sanitized identity and accounting for one provider request."""

    provider: str
    request_id: str
    operation: str
    request_count: int

    def with_request_count(self, request_count: int) -> ProviderRequestContext:
        """Return the same request identity with an updated attempted-call count."""
        return ProviderRequestContext(
            self.provider,
            self.request_id,
            self.operation,
            request_count,
        )

    def error(
        self,
        *,
        kind: ErrorKind,
        retryable: bool,
        status_code: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DiscoveryProviderError:
        """Build a sanitized error without repeating the request identity fields."""
        return provider_error(
            provider=self.provider,
            request_id=self.request_id,
            operation=self.operation,
            request_count=self.request_count,
            kind=kind,
            retryable=retryable,
            status_code=status_code,
            metadata=metadata,
        )


class ResponseTooLargeError(ValueError):
    """Signal that a provider response crossed the configured byte ceiling."""


class ResponseReadError(RuntimeError):
    """Signal a sanitized failure while streaming an already-received response."""


def utc_timestamp() -> str:
    """Return one timezone-aware UTC timestamp for a provider operation."""
    return datetime.now(UTC).isoformat()


def _http_response_limit() -> int:
    """Return the configurable maximum provider response bytes."""
    raw = os.getenv("LEADS_MAX_HTTP_RESPONSE_BYTES")
    if raw is None:
        return _DEFAULT_MAX_HTTP_RESPONSE_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("LEADS_MAX_HTTP_RESPONSE_BYTES must be a positive integer") from exc
    if value <= 0:
        raise ValueError("LEADS_MAX_HTTP_RESPONSE_BYTES must be a positive integer")
    return value


def _declared_response_size(response: httpx.Response) -> int | None:
    """Parse a declared response size, treating malformed lengths as unknown."""
    raw_length = response.headers.get("content-length")
    if raw_length is None:
        return None
    try:
        return int(raw_length)
    except ValueError:
        return None


def _enforce_declared_response_limit(response: httpx.Response, limit: int) -> None:
    """Reject an oversized declared response before consuming body bytes."""
    declared = _declared_response_size(response)
    if declared is not None and declared > limit:
        response.close()
        raise ResponseTooLargeError("provider response exceeds byte limit")


def read_bounded_response(response: httpx.Response) -> bytes:
    """Read one response incrementally and abort as soon as its hard byte limit is exceeded."""
    limit = _http_response_limit()
    _enforce_declared_response_limit(response, limit)

    chunks: list[bytes] = []
    size = 0
    try:
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > limit:
                raise ResponseTooLargeError("provider response exceeds byte limit")
            chunks.append(chunk)
    except httpx.HTTPError:
        raise ResponseReadError("provider response read failed") from None
    finally:
        response.close()
    return b"".join(chunks)


def classify_http_status(status_code: int) -> tuple[ErrorKind, bool]:
    """Map an HTTP status to the frozen M2 provider failure taxonomy."""
    if status_code in {401, 403}:
        return "authentication", False
    if status_code == 402:
        return "budget_exhausted", False
    if status_code in {400, 422}:
        return "invalid_request", False
    if status_code in {408, 429}:
        return "rate_limited", True
    if 500 <= status_code <= 599:
        return "transient", True
    if 400 <= status_code <= 499:
        return "permanent", False
    return "invalid_response", False


def validate_common_request(request: DiscoveryRequest, provider: DiscoveryProviderName) -> None:
    """Reject wrong-provider, geography, query, and result-cap inputs before HTTP work."""
    if request.provider != provider:
        raise ValueError(f"request provider must be {provider}")
    if request.target_country_code not in {"US", "CA"}:
        raise ValueError("target country must be US or CA")
    if (
        not isinstance(request.queries, tuple)
        or not request.queries
        or any(not isinstance(query, str) or not query.strip() for query in request.queries)
    ):
        raise ValueError("queries must be a nonempty tuple of nonblank strings")
    if (
        isinstance(request.max_results_total, bool)
        or not isinstance(request.max_results_total, int)
        or not 1 <= request.max_results_total <= 100
    ):
        raise ValueError("max_results_total must be an integer in 1..100")
    if (
        isinstance(request.max_results_per_query, bool)
        or not isinstance(request.max_results_per_query, int)
        or not 1 <= request.max_results_per_query <= 100
    ):
        raise ValueError("max_results_per_query must be an integer in 1..100")
    if request.max_results_per_query * len(request.queries) < request.max_results_total:
        raise ValueError("per-query cap cannot satisfy total result cap")


def validation_error(
    *, provider: str, request_id: str, message: str, operation: str
) -> DiscoveryProviderError:
    """Build a sanitized zero-attempt invalid-request error."""
    return DiscoveryProviderError(
        message,
        provider=provider,
        kind="invalid_request",
        request_id=request_id,
        retryable=False,
        status_code=None,
        usage_event=UsageEvent(
            provider=provider,
            operation=operation,
            request_count=0,
            metadata={"request_id": request_id},
        ),
    )


def provider_error(
    *,
    provider: str,
    request_id: str,
    operation: str,
    request_count: int,
    kind: ErrorKind,
    retryable: bool,
    status_code: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> DiscoveryProviderError:
    """Build a sanitized attempted-call provider error with safe usage metadata."""
    return DiscoveryProviderError(
        f"{provider} {operation} failed: {kind}",
        provider=provider,
        kind=kind,
        request_id=request_id,
        retryable=retryable,
        status_code=status_code,
        usage_event=UsageEvent(
            provider=provider,
            operation=operation,
            request_count=request_count,
            metadata=metadata or {"request_id": request_id},
        ),
    )


def _decode_bounded_json(
    response: httpx.Response,
    context: ProviderRequestContext,
    *,
    metadata: dict[str, Any] | None = None,
) -> Any:
    """Read and decode bounded JSON without leaking provider-controlled text."""
    try:
        body = read_bounded_response(response)
        return json.loads(body)
    except (ResponseTooLargeError, ResponseReadError, json.JSONDecodeError, UnicodeDecodeError):
        raise context.error(
            kind="invalid_response",
            retryable=False,
            status_code=response.status_code,
            metadata=metadata,
        ) from None


def request_json(
    response: httpx.Response,
    *,
    provider: str,
    request_id: str,
    operation: str,
    request_count: int,
) -> dict[str, Any]:
    """Decode a stream-bounded provider response as one JSON object."""
    context = ProviderRequestContext(provider, request_id, operation, request_count)
    payload = _decode_bounded_json(response, context)
    if not isinstance(payload, dict):
        raise context.error(
            kind="invalid_response",
            retryable=False,
            status_code=response.status_code,
        ) from None
    return cast(dict[str, Any], payload)


def stable_raw_record_id(
    *,
    provider: DiscoveryProviderName,
    request: DiscoveryRequest,
    provider_result_id: str | None,
    parsed_identity: dict[str, Any],
    raw_metadata: dict[str, Any],
) -> str:
    """Build the deterministic frozen raw-row ID while excluding retrieval time and secrets."""
    if provider_result_id:
        identity: Any = [provider, request.request_id, provider_result_id]
    else:
        identity = [provider, request.request_id, parsed_identity, raw_metadata]
    encoded = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "raw_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def safe_transport_call(
    call: Callable[[], httpx.Response],
    *,
    context: ProviderRequestContext | None = None,
    provider: str | None = None,
    request_id: str | None = None,
    operation: str | None = None,
    request_count: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> httpx.Response:
    """Run one streamed dispatch and enforce the stable provider request boundary."""
    if context is None:
        if provider is None or request_id is None or operation is None or request_count is None:
            raise TypeError("provider request context is required")
        context = ProviderRequestContext(provider, request_id, operation, request_count)
    elif any(value is not None for value in (provider, request_id, operation, request_count)):
        raise TypeError("pass either context or provider request identity fields")

    try:
        response = call()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise context.error(
            kind="transient",
            retryable=True,
            metadata={**(metadata or {"request_id": context.request_id}), "safe_to_retry": True},
        ) from None
    except httpx.HTTPError:
        raise context.error(
            kind="transient",
            retryable=False,
            metadata={**(metadata or {"request_id": context.request_id}), "outcome_unknown": True},
        ) from None
    try:
        _enforce_declared_response_limit(response, _http_response_limit())
    except ResponseTooLargeError:
        raise context.error(
            kind="invalid_response",
            retryable=False,
            status_code=response.status_code,
            metadata=metadata,
        ) from None
    status_code = response.status_code
    if not 200 <= status_code < 300:
        response.close()
        kind, retryable = classify_http_status(status_code)
        raise context.error(
            kind=kind,
            retryable=retryable,
            status_code=status_code,
            metadata=metadata,
        ) from None
    return response


def request_json_at_boundary(
    client: httpx.Client,
    request: httpx.Request,
    *,
    context: ProviderRequestContext,
    metadata: dict[str, Any] | None = None,
) -> tuple[Any, int]:
    """Dispatch one streamed request and return bounded JSON plus its successful status."""
    response = safe_transport_call(
        lambda: client.send(request, stream=True),
        context=context,
        metadata=metadata,
    )
    return _decode_bounded_json(response, context, metadata=metadata), response.status_code
