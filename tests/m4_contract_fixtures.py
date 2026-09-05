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
ClayResultFactory = Callable[[httpx.Request, str], list[dict[str, Any]]]

_M4_PROVIDER_KEYS = {
    "EXA_API_KEY": "exa-contract-key",
    "CLAY_PUBLIC_API_KEY": "clay-contract-key",
    "CLAY_WORK_EMAIL_FUNCTION_ID": "function:t_work_email_contract",
    "APOLLO_API_KEY": "apollo-contract-key",
    "INSTANTLY_API_KEY": "instantly-contract-key",
}


def call_cli(argv: Sequence[str]) -> int:
    """Invoke the public CLI exactly as an in-process command."""
    return cli.main(argv)


def call_enrich_live(
    data_root: Path,
    run_id: str,
    *,
    exa_people_budget_usd: float = 1.0,
) -> int:
    """Invoke M4 through its explicit live surface while HTTP remains in-memory."""
    return call_cli(
        [
            "enrich",
            "--run-id",
            run_id,
            "--data-root",
            str(data_root),
            "--exa-people-budget-usd",
            str(exa_people_budget_usd),
            "--execute-live",
        ]
    )


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
    """Install inert fake credentials/config so provider composition never needs real secrets."""
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
    """Read one UTF-8 CSV artifact and reject malformed missing cells."""
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            if any(key is None or value is None for key, value in raw.items()):
                raise AssertionError(f"malformed CSV row in {path.name}")
            rows.append({str(key): str(value) for key, value in raw.items()})
    return rows


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
    """Return an Exa People row with canonical current-employment history."""
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
    work_history = {
        "company": {"name": company, "domain": domain},
        "title": title,
        "dates": {"to": None if current else "2025-01-01"},
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
        "workHistory": [work_history],
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


def _submitted_item_ids(request: httpx.Request) -> list[str]:
    """Extract stable item IDs from list-shaped Clay POST payload sections."""
    payload = json.loads(request.content or b"{}")
    found: list[str] = []

    def visit(value: Any) -> None:
        """Collect IDs only from dictionary items carried inside submitted lists."""
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    item_id = item.get("id")
                    if isinstance(item_id, str) and item_id:
                        found.append(item_id)
                visit(item)
        elif isinstance(value, dict):
            for nested in value.values():
                visit(nested)

    visit(payload)
    return found


class ClayRoutineScript:
    """Model Clay's asynchronous POST-start then later GET-results lifecycle."""

    def __init__(self, results: ClayResultFactory | list[dict[str, Any]]) -> None:
        """Store deterministic completed results for each started routine run."""
        self._results = results
        self._started: dict[str, list[dict[str, Any]]] = {}
        self._released: set[str] = set()
        self.posts: list[httpx.Request] = []
        self.gets: list[httpx.Request] = []

    @property
    def latest_run_id(self) -> str | None:
        """Return the most recently started routine run identifier."""
        if not self._started:
            return None
        return next(reversed(self._started))

    def release_started(self) -> None:
        """Allow later invocations to retrieve every currently started routine."""
        self._released.update(self._started)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Start routines by POST and expose results only through the canonical GET path."""
        if request.method == "POST":
            run_id = f"routine-contract-{len(self.posts) + 1}"
            generated = (
                self._results(request, run_id)
                if callable(self._results)
                else self._results
            )
            rows = [dict(row) for row in generated]
            submitted_ids = _submitted_item_ids(request)
            if rows:
                assert len(submitted_ids) >= len(rows), (
                    "Clay POST must carry one stable item id for each returned result row"
                )
                for row, item_id in zip(rows, submitted_ids, strict=False):
                    row.setdefault("id", item_id)
            self.posts.append(request)
            self._started[run_id] = rows
            return httpx.Response(
                202,
                json={"routine_run_id": run_id, "status": "pending"},
            )

        assert request.method == "GET"
        self.gets.append(request)
        run_id = request.url.path.split("/")[-2]
        assert request.url.path == f"/public/v0/routines/run/{run_id}/results"
        assert run_id in self._started
        if run_id not in self._released:
            return httpx.Response(
                202,
                json={"routine_run_id": run_id, "status": "pending"},
            )
        rows = self._started[run_id]
        return httpx.Response(
            200,
            json={
                "routine_run_id": run_id,
                "status": "completed",
                "credits_used": 1,
                "data": rows,
            },
        )


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
            if provider not in host:
                continue
            responder = self._responders.get(provider)
            if responder is None:
                raise AssertionError(
                    f"unexpected {provider} request: {request.method} {request.url}"
                )
            return responder(request)
        raise AssertionError(f"unexpected network target: {request.method} {request.url}")

    def for_provider(self, provider: str) -> list[httpx.Request]:
        """Return recorded requests whose hostname belongs to one provider."""
        needle = provider.casefold()
        return [
            request
            for request in self.requests
            if needle in request.url.host.casefold()
        ]


def install_mock_http(monkeypatch: pytest.MonkeyPatch, stub: WireStub) -> None:
    """Route explicit live HTTP clients through MockTransport without weakening network guards."""
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
