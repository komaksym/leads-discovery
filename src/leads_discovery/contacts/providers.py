"""Concrete M4 people-discovery provider built on the shared request boundary."""

from __future__ import annotations

import math
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import quote

import httpx

from leads_discovery.contacts.models import ContactRecord, VerificationStatus
from leads_discovery.discovery.base import (
    classify_http_status,
    provider_error,
    request_json,
    safe_transport_call,
)
from leads_discovery.models import CompanyRecord, ErrorKind, UsageEvent

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
                    "Operations, Commercial, Estimating, Inside Sales leaders, "
                    "branch or regional managers"
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




_CLAY_BASE_URL = "https://api.clay.com/public/v0"
_APOLLO_PEOPLE_URL = "https://api.apollo.io/api/v1/people/match"
_INSTANTLY_VERIFY_URL = "https://api.instantly.ai/api/v2/email-verification"
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PERSONAL_EMAIL_DOMAINS = frozenset(
    {
        "aol.com",
        "gmail.com",
        "googlemail.com",
        "hotmail.com",
        "icloud.com",
        "live.com",
        "outlook.com",
        "proton.me",
        "protonmail.com",
        "yahoo.com",
    }
)


class ContactProviderError(RuntimeError):
    """Sanitized contact-provider failure with authoritative request usage."""

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
        super().__init__(f"{provider} {operation} failed: {kind}")
        self.provider = provider
        self.operation = operation
        self.kind = kind
        self.retryable = retryable
        self.status_code = status_code
        self.usage_event = UsageEvent.from_dict(usage_event.to_dict())


@dataclass(frozen=True, slots=True)
class ClayStartResult:
    routine_run_id: str
    usage_event: UsageEvent


@dataclass(frozen=True, slots=True)
class ClayResults:
    status: Literal["pending", "complete"]
    items: list[dict[str, Any]]
    usage_event: UsageEvent


@dataclass(frozen=True, slots=True)
class ApolloResult:
    work_email: str | None
    credits_used: float
    usage_event: UsageEvent


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: VerificationStatus
    usage_event: UsageEvent


def _contact_error(
    event: UsageEvent,
    *,
    kind: ErrorKind,
    retryable: bool,
    status_code: int | None = None,
) -> ContactProviderError:
    return ContactProviderError(
        provider=event.provider,
        operation=event.operation,
        kind=kind,
        retryable=retryable,
        status_code=status_code,
        usage_event=event,
    )


def _dispatch(
    call: Any,
    *,
    provider: str,
    operation: str,
    metadata: dict[str, Any],
) -> tuple[httpx.Response, UsageEvent]:
    event = UsageEvent(
        provider=provider,
        operation=operation,
        request_count=1,
        metadata=deepcopy(metadata),
    )
    try:
        response = cast(httpx.Response, call())
    except httpx.HTTPError:
        raise _contact_error(event, kind="transient", retryable=False) from None
    if not 200 <= response.status_code < 300:
        kind, retryable = classify_http_status(response.status_code)
        raise _contact_error(
            event,
            kind=kind,
            retryable=retryable,
            status_code=response.status_code,
        )
    return response, event


def _json_object(response: httpx.Response, event: UsageEvent) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise _contact_error(
            event,
            kind="invalid_response",
            retryable=False,
            status_code=response.status_code,
        ) from None
    if not isinstance(payload, dict):
        raise _contact_error(
            event,
            kind="invalid_response",
            retryable=False,
            status_code=response.status_code,
        )
    return cast(dict[str, Any], payload)


def _nonnegative(value: object, *, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a nonnegative finite number")
    return float(value)


def usable_work_email(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    email = value.strip().casefold()
    if not _EMAIL.fullmatch(email):
        return None
    if email.rsplit("@", 1)[1] in _PERSONAL_EMAIL_DOMAINS:
        return None
    return email


def _reported_credits(payload: dict[str, Any]) -> float | None:
    candidates: list[object] = [payload.get("credits_consumed"), payload.get("credits_used")]
    raw_usage = payload.get("usage")
    if isinstance(raw_usage, dict):
        candidates.extend([raw_usage.get("credits_consumed"), raw_usage.get("credits_used")])
    for value in candidates:
        if value is not None:
            return _nonnegative(value, name="credits_used")
    return None


class ClayContactProvider:
    """Start and poll one configured Clay work-email routine."""

    def __init__(self, *, api_key: str, routine_id: str, client: httpx.Client) -> None:
        if not api_key.strip() or not routine_id.strip():
            raise ValueError("Clay api_key and routine_id must be nonempty")
        self._api_key = api_key
        self._routine_id = routine_id
        self._client = client

    def start(self, contacts: list[ContactRecord]) -> ClayStartResult:
        if not 1 <= len(contacts) <= 100:
            raise ValueError("Clay routine start requires 1..100 contacts")
        items = [
            {
                "id": contact.contact_id,
                "inputs": {
                    "full_name": contact.full_name,
                    "company_name": contact.company_name,
                    "company_domain": contact.company_domain,
                    "linkedin_url": contact.linkedin_url,
                    "profile_url": contact.profile_url,
                },
            }
            for contact in contacts
        ]
        response, event = _dispatch(
            lambda: self._client.post(
                f"{_CLAY_BASE_URL}/routines/{quote(self._routine_id, safe=':')}/run",
                headers={"clay-api-key": self._api_key},
                json={"items": items},
                timeout=_REQUEST_TIMEOUT,
            ),
            provider="clay",
            operation="work_email_routine_start",
            metadata={"submitted_contacts": len(items)},
        )
        payload = _json_object(response, event)
        run_id = payload.get("routine_run_id") or payload.get("routineRunId")
        if not isinstance(run_id, str) or not run_id.strip():
            raise _contact_error(
                event,
                kind="invalid_response",
                retryable=False,
                status_code=response.status_code,
            )
        return ClayStartResult(run_id.strip(), event)

    def results(self, routine_run_id: str) -> ClayResults:
        if not routine_run_id.strip():
            raise ValueError("routine_run_id must be nonempty")
        response, event = _dispatch(
            lambda: self._client.get(
                f"{_CLAY_BASE_URL}/routines/run/{quote(routine_run_id, safe=':')}/results",
                headers={"clay-api-key": self._api_key},
                timeout=_REQUEST_TIMEOUT,
            ),
            provider="clay",
            operation="work_email_routine_results",
            metadata={"routine_run_id": routine_run_id},
        )
        if response.status_code == 202:
            return ClayResults("pending", [], event)
        payload = _json_object(response, event)
        raw_status = payload.get("status")
        status = raw_status.casefold() if isinstance(raw_status, str) else None
        if status in {"pending", "in_progress", "running", "queued"}:
            return ClayResults("pending", [], event)
        data = payload.get("data")
        if status not in {None, "complete", "completed"} or not isinstance(data, list):
            raise _contact_error(
                event,
                kind="invalid_response",
                retryable=False,
                status_code=response.status_code,
            )
        if any(not isinstance(item, dict) for item in data):
            raise _contact_error(
                event,
                kind="invalid_response",
                retryable=False,
                status_code=response.status_code,
            )
        return ClayResults(
            "complete",
            [deepcopy(cast(dict[str, Any], item)) for item in data],
            event,
        )


class ApolloContactProvider:
    """Use Apollo only as a work-email fallback."""

    def __init__(self, *, api_key: str, client: httpx.Client) -> None:
        if not api_key.strip():
            raise ValueError("api_key must be nonempty")
        self._api_key = api_key
        self._client = client

    def enrich(self, contact: ContactRecord) -> ApolloResult:
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
        response, event = _dispatch(
            lambda: self._client.post(
                _APOLLO_PEOPLE_URL,
                headers={"x-api-key": self._api_key},
                json=body,
                timeout=_REQUEST_TIMEOUT,
            ),
            provider="apollo",
            operation="people_enrichment",
            metadata={"contact_id": contact.contact_id, "credits_used": 1.0},
        )
        payload = _json_object(response, event)
        try:
            credits_used = _reported_credits(payload)
        except ValueError:
            raise _contact_error(
                event,
                kind="invalid_response",
                retryable=False,
                status_code=response.status_code,
            ) from None
        credits = 1.0 if credits_used is None else credits_used
        event.metadata["credits_used"] = credits
        person = payload.get("person")
        email = usable_work_email(person.get("email")) if isinstance(person, dict) else None
        return ApolloResult(email, credits, event)


class InstantlyVerificationProvider:
    """Create or poll verification for one work email."""

    def __init__(self, *, api_key: str, client: httpx.Client) -> None:
        if not api_key.strip():
            raise ValueError("api_key must be nonempty")
        self._api_key = api_key
        self._client = client

    def create(self, email: str) -> VerificationResult:
        return self._request(
            "email_verification_create",
            email,
            lambda: self._client.post(
                _INSTANTLY_VERIFY_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"email": email},
                timeout=_REQUEST_TIMEOUT,
            ),
        )

    def get(self, email: str) -> VerificationResult:
        return self._request(
            "email_verification_get",
            email,
            lambda: self._client.get(
                f"{_INSTANTLY_VERIFY_URL}/{quote(email, safe='')}",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=_REQUEST_TIMEOUT,
            ),
        )

    def _request(self, operation: str, email: str, call: Any) -> VerificationResult:
        normalized = usable_work_email(email)
        if normalized is None:
            raise ValueError("Instantly verification requires a valid work email")
        response, event = _dispatch(
            call,
            provider="instantly",
            operation=operation,
            metadata={"email": normalized},
        )
        payload = _json_object(response, event)
        status = payload.get("verification_status")
        if status not in {"verified", "invalid", "pending"}:
            raise _contact_error(
                event,
                kind="invalid_response",
                retryable=False,
                status_code=response.status_code,
            )
        event.metadata["verification_status"] = status
        return VerificationResult(cast(VerificationStatus, status), event)


def clay_item_email(item: dict[str, Any]) -> str | None:
    result = item.get("result")
    if isinstance(result, dict):
        email = usable_work_email(result.get("work_email"))
        if email is not None:
            return email
    return usable_work_email(item.get("work_email"))


__all__ = [
    "ApolloContactProvider",
    "ApolloResult",
    "ClayContactProvider",
    "ClayResults",
    "ClayStartResult",
    "ContactProviderError",
    "ExaPeopleProvider",
    "ExaPeopleResult",
    "InstantlyVerificationProvider",
    "VerificationResult",
    "clay_item_email",
    "usable_work_email",
]
