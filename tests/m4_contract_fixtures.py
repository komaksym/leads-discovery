"""Test-only black-box fixtures for the independent M4 contract suite."""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from m3_factories import write_run_inputs

import leads_discovery.cli as cli
from leads_discovery.models import CompanyRecord
from leads_discovery.pipeline.evaluation import EvaluationConfig, evaluate_run

Responder = Callable[[httpx.Request], httpx.Response]

_M4_PROVIDER_KEYS = {
    "EXA_API_KEY": "exa-contract-key",
    "CLAY_API_KEY": "clay-contract-key",
    "APOLLO_API_KEY": "apollo-contract-key",
    "INSTANTLY_API_KEY": "instantly-contract-key",
}


def call_cli(argv: Sequence[str]) -> int:
    """Invoke the public CLI exactly as an in-process command."""
    return cli.main(argv)


def prepare_evaluated_run(
    data_root: Path,
    run_id: str,
    companies: Iterable[CompanyRecord],
) -> Path:
    """Persist M2 inputs and derive the authoritative M3 evaluated artifact."""
    run_dir = write_run_inputs(data_root, run_id, companies)
    evaluate_run(EvaluationConfig(run_id=run_id, data_root=data_root, max_evaluated=20))
    return run_dir


def set_m4_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install inert fake credentials so provider composition never needs real secrets."""
    for key, value in _M4_PROVIDER_KEYS.items():
        monkeypatch.setenv(key, value)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read one strict JSONL artifact into dictionaries."""
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise AssertionError(f"expected JSON object in {path.name}")
        rows.append(payload)
    return rows


def read_csv(path: Path) -> list[dict[str, str]]:
    """Read one UTF-8 CSV artifact with the standard library parser."""
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def json_body(request: httpx.Request) -> dict[str, Any]:
    """Decode one provider request body and require a JSON object."""
    payload = json.loads(request.content or b"{}")
    if not isinstance(payload, dict):
        raise AssertionError("provider request body must be a JSON object")
    return payload


def row_text(row: Mapping[str, Any]) -> str:
    """Return deterministic searchable text for one persisted row."""
    return json.dumps(dict(row), sort_keys=True, ensure_ascii=False, default=str)


def person_result(
    *,
    name: str,
    title: str,
    company: str,
    domain: str,
    profile_url: str | None,
    current: bool = True,
    person_id: str | None = None,
) -> dict[str, Any]:
    """Return a redundant People Search row usable by thin provider adapters."""
    pid = person_id or name.casefold().replace(" ", "-")
    current_company = company if current else "Former Employer LLC"
    employment = {
        "company": company,
        "company_name": company,
        "title": title,
        "current": current,
        "is_current": current,
        "end_date": None if current else "2025-01-01",
    }
    properties: dict[str, Any] = {
        "name": name,
        "fullName": name,
        "title": title,
        "headline": f"{title} at {company}",
        "company": company,
        "companyName": company,
        "currentCompany": {"name": current_company, "domain": domain},
        "current_company": {"name": current_company, "domain": domain},
        "employment": [employment],
        "workExperience": [employment],
        "profileUrl": profile_url,
        "linkedinUrl": profile_url,
    }
    return {
        "id": pid,
        "url": profile_url,
        "title": f"{name} — {title} at {company}",
        "author": name,
        "name": name,
        "text": f"{name}\n{title}\n{company}\n{domain}",
        "highlights": [f"{name} is {title} at {company}."],
        "entities": [
            {
                "id": pid,
                "type": "person",
                "name": name,
                "properties": properties,
                **properties,
            }
        ],
        **properties,
    }


class WireStub:
    """Route every attempted provider request through one in-memory script."""

    def __init__(self, responders: Mapping[str, Responder]) -> None:
        """Store provider responders and initialize the ordered request log."""
        self._responders = dict(responders)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record one request and dispatch it by provider hostname."""
        self.requests.append(request)
        host = request.url.host.casefold()
        for provider in ("exa", "clay", "apollo", "instantly"):
            if provider in host:
                responder = self._responders.get(provider)
                if responder is None:
                    raise AssertionError(f"unexpected {provider} request: {request.method} {request.url}")
                return responder(request)
        raise AssertionError(f"unexpected network target: {request.method} {request.url}")

    def for_provider(self, provider: str) -> list[httpx.Request]:
        """Return recorded requests whose hostname belongs to one provider."""
        needle = provider.casefold()
        return [request for request in self.requests if needle in request.url.host.casefold()]


def install_mock_http(monkeypatch: pytest.MonkeyPatch, stub: WireStub) -> None:
    """Route normal httpx clients and convenience calls through MockTransport."""
    sync_client = httpx.Client
    async_client = httpx.AsyncClient
    transport = httpx.MockTransport(stub)

    def make_client(*args: Any, **kwargs: Any) -> httpx.Client:
        """Create one synchronous client whose transport cannot reach the network."""
        options = dict(kwargs)
        options["transport"] = transport
        return sync_client(*args, **options)

    def make_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        """Create one asynchronous client whose transport cannot reach the network."""
        options = dict(kwargs)
        options["transport"] = transport
        return async_client(*args, **options)

    def routed_request(
        method: str,
        url: str | httpx.URL,
        **kwargs: Any,
    ) -> httpx.Response:
        """Route one module-level httpx request through the same in-memory transport."""
        with sync_client(transport=transport) as client:
            return client.request(method, url, **kwargs)

    def routed_get(url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        """Route one module-level GET through the same in-memory transport."""
        return routed_request("GET", url, **kwargs)

    def routed_post(url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        """Route one module-level POST through the same in-memory transport."""
        return routed_request("POST", url, **kwargs)

    monkeypatch.setattr(httpx, "Client", make_client)
    monkeypatch.setattr(httpx, "AsyncClient", make_async_client)
    monkeypatch.setattr(httpx, "request", routed_request)
    monkeypatch.setattr(httpx, "get", routed_get)
    monkeypatch.setattr(httpx, "post", routed_post)
