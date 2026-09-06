"""Thin bounded HTTP adapters for M4 contact discovery, enrichment, and verification."""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, Never, cast
from urllib.parse import quote

import httpx

from leads_discovery.contacts.models import ContactRecord, VerificationStatus
from leads_discovery.discovery.base import (
    DiscoveryProviderError,
    ProviderRequestContext,
    ResponseTooLargeError,
    read_bounded_response,
    safe_transport_call,
)
from leads_discovery.models import CompanyRecord, ErrorKind, UsageEvent

_EXA_SEARCH_URL = "https://api.exa.ai/search"
_CLAY_BASE_URL = "https://api.clay.com/public/v0"
_APOLLO_PEOPLE_URL = "https://api.apollo.io/api/v1/people/match"
_INSTANTLY_VERIFY_URL = "https://api.instantly.ai/api/v2/email-verification"
_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PERSONAL_EMAIL_DOMAINS = {
    "aol.com",
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "icloud.com",
    "live.com",
    "me.com",
    "msn.com",
    "outlook.com",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
    "ymail.com",
}


class ContactProviderError(RuntimeError):
    """Expose a sanitized M4 provider failure with safe usage accounting."""

    def __init__(
        self,
        *,
        provider: str,
        operation: str,
        kind: ErrorKind,
        retryable: bool,
        status_code: int | None,
        usage_event: UsageEvent,
    ) -> None:
        """Initialize an error without retaining credentials, request bodies, or raw responses."""
        super().__init__(f"{provider} {operation} failed: {kind}")
        self.provider = provider
        self.operation = operation
        self.kind = kind
        self.retryable = retryable
        self.status_code = status_code
        self.usage_event = UsageEvent.from_dict(usage_event.to_dict())


@dataclass(slots=True)
class ExaPeopleResult:
    """Return bounded raw Exa People rows plus one authenticated usage event."""

    results: list[dict[str, Any]]
    usage_event: UsageEvent

    def __post_init__(self) -> None:
        """Detach provider result rows and usage metadata from caller-owned state."""
        self.results = deepcopy(self.results)
        self.usage_event = UsageEvent.from_dict(self.usage_event.to_dict())


@dataclass(slots=True)
class ClayStartResult:
    """Return a newly created Clay routine-run identifier and its request usage."""

    routine_run_id: str
    usage_event: UsageEvent

    def __post_init__(self) -> None:
        """Detach usage metadata from caller-owned state."""
        self.usage_event = UsageEvent.from_dict(self.usage_event.to_dict())


ClayRunStatus = Literal["pending", "complete"]


@dataclass(slots=True)
class ClayResults:
    """Return one Clay routine status read and terminal per-item data when available."""

    status: ClayRunStatus
    items: list[dict[str, Any]]
    usage_event: UsageEvent

    def __post_init__(self) -> None:
        """Detach returned item rows and usage metadata from caller-owned state."""
        self.items = deepcopy(self.items)
        self.usage_event = UsageEvent.from_dict(self.usage_event.to_dict())


@dataclass(slots=True)
class ApolloResult:
    """Return an optional work email and provider-reported credit usage when available."""

    work_email: str | None
    credits_used: float | None
    usage_event: UsageEvent

    def __post_init__(self) -> None:
        """Detach usage metadata from caller-owned state."""
        self.usage_event = UsageEvent.from_dict(self.usage_event.to_dict())


@dataclass(slots=True)
class VerificationResult:
    """Return the exact Instantly verification state and reported credits used."""

    status: VerificationStatus
    credits_used: float | None
    usage_event: UsageEvent

    def __post_init__(self) -> None:
        """Detach usage metadata from caller-owned state."""
        self.usage_event = UsageEvent.from_dict(self.usage_event.to_dict())


def _usage(
    provider: str,
    operation: str,
    *,
    request_count: int = 1,
    estimated_cost_usd: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> UsageEvent:
    """Build a safe usage event for one M4 provider operation."""
    return UsageEvent(
        provider=provider,
        operation=operation,
        request_count=request_count,
        estimated_cost_usd=estimated_cost_usd,
        metadata=metadata or {},
    )


def _raise(
    provider: str,
    operation: str,
    *,
    kind: ErrorKind,
    retryable: bool,
    status_code: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> Never:
    """Raise a sanitized provider error with one attempted-call usage event."""
    raise ContactProviderError(
        provider=provider,
        operation=operation,
        kind=kind,
        retryable=retryable,
        status_code=status_code,
        usage_event=_usage(provider, operation, metadata=metadata),
    )


def _call(
    client: httpx.Client,
    request: httpx.Request,
    *,
    provider: str,
    operation: str,
    metadata: dict[str, Any] | None = None,
) -> httpx.Response:
    """Dispatch through the shared provider transport guard and preserve M4 error contracts."""
    context = ProviderRequestContext(
        provider=provider,
        request_id=f"m4:{provider}:{operation}",
        operation=operation,
        request_count=1,
    )
    try:
        return safe_transport_call(
            lambda: client.send(request, stream=True),
            context=context,
            metadata=metadata,
        )
    except DiscoveryProviderError as exc:
        raise ContactProviderError(
            provider=exc.provider,
            operation=operation,
            kind=exc.kind,
            retryable=exc.retryable,
            status_code=exc.status_code,
            usage_event=exc.usage_event,
        ) from None


def _json_object(
    response: httpx.Response,
    *,
    provider: str,
    operation: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Decode one stream-bounded response as a JSON object or raise a sanitized error."""
    status_code = response.status_code
    try:
        payload = json.loads(read_bounded_response(response))
    except httpx.HTTPError:
        _raise(
            provider,
            operation,
            kind="transient",
            retryable=False,
            status_code=status_code,
            metadata={**(metadata or {}), "outcome_unknown": True},
        )
    except (ResponseTooLargeError, json.JSONDecodeError, UnicodeDecodeError):
        _raise(
            provider,
            operation,
            kind="invalid_response",
            retryable=False,
            status_code=status_code,
            metadata=metadata,
        )
    if not isinstance(payload, dict):
        _raise(
            provider,
            operation,
            kind="invalid_response",
            retryable=False,
            status_code=status_code,
            metadata=metadata,
        )
    return cast(dict[str, Any], payload)


def _finite_nonnegative(value: Any, *, field: str) -> float:
    """Parse a provider-reported numeric usage value and reject bool/NaN/infinity/negative data."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{field} must be a finite nonnegative number")
    return float(value)


def _reported_credits(payload: dict[str, Any]) -> float | None:
    """Read a known credit-usage field without inventing zero when usage is absent."""
    candidates: list[tuple[str, Any]] = []
    for key in ("credits_consumed", "credits_used"):
        if key in payload:
            candidates.append((key, payload[key]))
    raw_usage = payload.get("usage")
    if isinstance(raw_usage, dict):
        for key in ("credits_consumed", "credits_used"):
            if key in raw_usage:
                candidates.append((f"usage.{key}", raw_usage[key]))
    for field, value in candidates:
        if value is None:
            continue
        return _finite_nonnegative(value, field=field)
    return None


def usable_work_email(value: Any) -> str | None:
    """Return a syntactically valid non-personal email or None without guessing."""
    if not isinstance(value, str):
        return None
    email = value.strip().casefold()
    if not _EMAIL.fullmatch(email):
        return None
    domain = email.rsplit("@", 1)[1]
    if domain in _PERSONAL_EMAIL_DOMAINS:
        return None
    return email


class ExaPeopleProvider:
    """Execute one bounded Exa People Search request per accepted company."""

    def __init__(self, *, api_key: str, client: httpx.Client) -> None:
        """Store a nonempty Exa credential and caller-owned injected HTTP client."""
        if not api_key.strip():
            raise ValueError("api_key must be nonempty")
        self._api_key = api_key
        self._client = client

    def search(self, company: CompanyRecord) -> ExaPeopleResult:
        """Search Exa's people category for up to ten buying-proximate company employees."""
        metadata = {"company_id": company.company_id}
        query = (
            f"Current employees at {company.name} closest to buying operational software: "
            "Owner, President, CEO, COO, Managing Partner, General Manager, senior Sales, "
            "Operations, Commercial, Estimating, Inside Sales leaders, branch or regional managers"
        )
        request = self._client.build_request(
            "POST",
            _EXA_SEARCH_URL,
            headers={"x-api-key": self._api_key},
            json={
                "query": query,
                "category": "people",
                "type": "auto",
                "numResults": 10,
                "contents": {"highlights": True},
            },
            timeout=_REQUEST_TIMEOUT,
        )
        response = _call(
            self._client,
            request,
            provider="exa",
            operation="people_search",
            metadata=metadata,
        )
        payload = _json_object(
            response,
            provider="exa",
            operation="people_search",
            metadata=metadata,
        )
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            _raise(
                "exa",
                "people_search",
                kind="invalid_response",
                retryable=False,
                status_code=response.status_code,
                metadata=metadata,
            )
        results: list[dict[str, Any]] = []
        for raw in raw_results[:10]:
            if not isinstance(raw, dict):
                _raise(
                    "exa",
                    "people_search",
                    kind="invalid_response",
                    retryable=False,
                    status_code=response.status_code,
                    metadata=metadata,
                )
            results.append(deepcopy(raw))
        cost: float | None = None
        raw_cost = payload.get("costDollars")
        if raw_cost is not None:
            if not isinstance(raw_cost, dict):
                _raise(
                    "exa",
                    "people_search",
                    kind="invalid_response",
                    retryable=False,
                    status_code=response.status_code,
                    metadata=metadata,
                )
            total = raw_cost.get("total")
            if total is not None:
                try:
                    cost = _finite_nonnegative(total, field="costDollars.total")
                except ValueError:
                    _raise(
                        "exa",
                        "people_search",
                        kind="invalid_response",
                        retryable=False,
                        status_code=response.status_code,
                        metadata=metadata,
                    )
        event = _usage(
            "exa",
            "people_search",
            estimated_cost_usd=cost,
            metadata={**metadata, "result_count": len(results)},
        )
        return ExaPeopleResult(results=results, usage_event=event)


class ClayContactProvider:
    """Call Clay's managed Work Email function through the direct Public API."""

    def __init__(self, *, api_key: str, routine_id: str, client: httpx.Client) -> None:
        """Store the Clay credential and workspace ID for its managed Work Email function."""
        if not api_key.strip():
            raise ValueError("api_key must be nonempty")
        if not routine_id.strip():
            raise ValueError("managed Work Email function id must be nonempty")
        self._api_key = api_key
        self._routine_id = routine_id
        self._client = client

    def start(self, contacts: list[ContactRecord]) -> ClayStartResult:
        """Start one asynchronous managed Work Email run for 1..100 contacts."""
        if not 1 <= len(contacts) <= 100:
            raise ValueError("Clay managed function start requires 1..100 contacts")
        items: list[dict[str, Any]] = []
        for contact in contacts:
            inputs: dict[str, Any] = {
                "Full Name": contact.full_name,
                "Company Domain": contact.company_domain,
                "Company Name": contact.company_name,
            }
            social_profile_url = contact.linkedin_url or contact.profile_url
            if social_profile_url is not None:
                inputs["Social Profile URL"] = social_profile_url
            items.append({"id": contact.contact_id, "inputs": inputs})
        metadata = {"submitted_contacts": len(items)}
        request = self._client.build_request(
            "POST",
            f"{_CLAY_BASE_URL}/routines/{quote(self._routine_id, safe=':')}/run",
            headers={"clay-api-key": self._api_key},
            json={"items": items},
            timeout=_REQUEST_TIMEOUT,
        )
        response = _call(
            self._client,
            request,
            provider="clay",
            operation="work_email_routine_start",
            metadata=metadata,
        )
        payload = _json_object(
            response,
            provider="clay",
            operation="work_email_routine_start",
            metadata=metadata,
        )
        run_id = payload.get("routine_run_id") or payload.get("routineRunId")
        if not isinstance(run_id, str) or not run_id.strip():
            _raise(
                "clay",
                "work_email_routine_start",
                kind="invalid_response",
                retryable=False,
                status_code=response.status_code,
                metadata=metadata,
            )
        return ClayStartResult(
            routine_run_id=run_id.strip(),
            usage_event=_usage(
                "clay", "work_email_routine_start", metadata=metadata
            ),
        )

    def results(self, routine_run_id: str) -> ClayResults:
        """Read the same persisted managed-function run without creating a replacement."""
        if not routine_run_id.strip():
            raise ValueError("routine_run_id must be nonempty")
        metadata = {"routine_run_id": routine_run_id}
        request = self._client.build_request(
            "GET",
            f"{_CLAY_BASE_URL}/routines/run/{quote(routine_run_id, safe=':')}/results",
            headers={"clay-api-key": self._api_key},
            timeout=_REQUEST_TIMEOUT,
        )
        response = _call(
            self._client,
            request,
            provider="clay",
            operation="work_email_routine_results",
            metadata=metadata,
        )
        event = _usage("clay", "work_email_routine_results", metadata=metadata)
        if response.status_code == 202:
            response.close()
            return ClayResults(status="pending", items=[], usage_event=event)
        payload = _json_object(
            response,
            provider="clay",
            operation="work_email_routine_results",
            metadata=metadata,
        )
        raw_status = payload.get("status")
        status = raw_status.casefold() if isinstance(raw_status, str) else None
        if status in {"pending", "in_progress", "running", "queued"}:
            return ClayResults(status="pending", items=[], usage_event=event)
        data = payload.get("data")
        if status not in {None, "complete", "completed"} or not isinstance(data, list):
            _raise(
                "clay",
                "work_email_routine_results",
                kind="invalid_response",
                retryable=False,
                status_code=response.status_code,
                metadata=metadata,
            )
        items: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                _raise(
                    "clay",
                    "work_email_routine_results",
                    kind="invalid_response",
                    retryable=False,
                    status_code=response.status_code,
                    metadata=metadata,
                )
            items.append(deepcopy(item))
        return ClayResults(status="complete", items=items, usage_event=event)


class ApolloContactProvider:
    """Attempt one synchronous Apollo work-email fallback without personal data or phones."""

    def __init__(self, *, api_key: str, client: httpx.Client) -> None:
        """Store a nonempty Apollo API key and caller-owned injected HTTP client."""
        if not api_key.strip():
            raise ValueError("api_key must be nonempty")
        self._api_key = api_key
        self._client = client

    def enrich(self, contact: ContactRecord) -> ApolloResult:
        """Use the strongest available identity while keeping all sensitive reveal flags false."""
        body: dict[str, Any] = {
            "name": contact.full_name,
            "domain": contact.company_domain,
            "organization_name": contact.company_name,
            "reveal_personal_emails": False,
            "reveal_phone_number": False,
            "run_waterfall_email": False,
            "run_waterfall_phone": False,
        }
        if contact.linkedin_url is not None:
            body["linkedin_url"] = contact.linkedin_url
        metadata = {"contact_id": contact.contact_id, "credits_reserved": 1.0}
        request = self._client.build_request(
            "POST",
            _APOLLO_PEOPLE_URL,
            headers={"x-api-key": self._api_key},
            json=body,
            timeout=_REQUEST_TIMEOUT,
        )
        response = _call(
            self._client,
            request,
            provider="apollo",
            operation="people_enrichment",
            metadata=metadata,
        )
        payload = _json_object(
            response,
            provider="apollo",
            operation="people_enrichment",
            metadata=metadata,
        )
        try:
            credits = _reported_credits(payload)
        except ValueError:
            _raise(
                "apollo",
                "people_enrichment",
                kind="invalid_response",
                retryable=False,
                status_code=response.status_code,
                metadata=metadata,
            )
        person = payload.get("person")
        work_email = (
            usable_work_email(person.get("email")) if isinstance(person, dict) else None
        )
        event_metadata: dict[str, Any] = {
            **metadata,
            "matched": isinstance(person, dict),
        }
        if credits is not None:
            event_metadata["credits_used"] = credits
        return ApolloResult(
            work_email=work_email,
            credits_used=credits,
            usage_event=_usage(
                "apollo", "people_enrichment", metadata=event_metadata
            ),
        )


class InstantlyVerificationProvider:
    """Call only Instantly's V2 email-verification create and status endpoints."""

    def __init__(self, *, api_key: str, client: httpx.Client) -> None:
        """Store a nonempty Instantly V2 bearer token and injected HTTP client."""
        if not api_key.strip():
            raise ValueError("api_key must be nonempty")
        self._api_key = api_key
        self._client = client

    def create(self, email: str) -> VerificationResult:
        """Create one verification request for a work email that has not been submitted before."""
        request = self._client.build_request(
            "POST",
            _INSTANTLY_VERIFY_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"email": email},
            timeout=_REQUEST_TIMEOUT,
        )
        return self._request(
            operation="email_verification_create",
            request=request,
            email=email,
        )

    def get(self, email: str) -> VerificationResult:
        """Read the status of the same previously pending work-email verification."""
        request = self._client.build_request(
            "GET",
            f"{_INSTANTLY_VERIFY_URL}/{quote(email, safe='')}",
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=_REQUEST_TIMEOUT,
        )
        return self._request(
            operation="email_verification_get",
            request=request,
            email=email,
        )

    def _request(
        self, *, operation: str, request: httpx.Request, email: str
    ) -> VerificationResult:
        """Execute one verification-only request and validate status and credit usage."""
        if usable_work_email(email) is None:
            raise ValueError("Instantly verification requires a syntactically valid work email")
        metadata = {"email": email.casefold()}
        response = _call(
            self._client,
            request,
            provider="instantly",
            operation=operation,
            metadata=metadata,
        )
        payload = _json_object(
            response,
            provider="instantly",
            operation=operation,
            metadata=metadata,
        )
        status = payload.get("verification_status")
        if status not in {"verified", "invalid", "pending"}:
            _raise(
                "instantly",
                operation,
                kind="invalid_response",
                retryable=False,
                status_code=response.status_code,
                metadata=metadata,
            )
        try:
            credits = (
                None
                if payload.get("credits_used") is None
                else _finite_nonnegative(payload["credits_used"], field="credits_used")
            )
        except ValueError:
            _raise(
                "instantly",
                operation,
                kind="invalid_response",
                retryable=False,
                status_code=response.status_code,
                metadata=metadata,
            )
        event_metadata: dict[str, Any] = {**metadata, "verification_status": status}
        if credits is not None:
            event_metadata["credits_used"] = credits
        return VerificationResult(
            status=cast(VerificationStatus, status),
            credits_used=credits,
            usage_event=_usage("instantly", operation, metadata=event_metadata),
        )


def clay_item_email(item: dict[str, Any]) -> str | None:
    """Extract a work email from Clay's managed Work Email function result."""
    result = item.get("result")
    if isinstance(result, dict):
        email = usable_work_email(result.get("Work Email"))
        if email is not None:
            return email
        email = usable_work_email(result.get("work_email"))
        if email is not None:
            return email
    return usable_work_email(item.get("work_email"))