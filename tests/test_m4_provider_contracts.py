"""Independent provider-wire and paid-resume contracts for M4."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from m3_factories import accepted_facts, build_company
from m4_contract_fixtures import (
    WireStub,
    call_cli,
    install_mock_http,
    json_body,
    person_result,
    prepare_evaluated_run,
    read_jsonl,
    row_text,
    set_m4_credentials,
)

EMAIL = "pat.owner@acmevalve.com"
PROFILE = "https://www.linkedin.com/in/pat-owner"


def _exa_one(request: httpx.Request) -> httpx.Response:
    """Serve one current high-proximity decision maker from Exa People Search."""
    assert request.method == "POST"
    result = person_result(
        name="Pat Owner",
        title="President and Owner",
        company="Acme Valve",
        domain="acmevalve.com",
        profile_url=PROFILE,
    )
    return httpx.Response(
        200,
        json={"results": [result], "costDollars": {"total": 0.001}},
    )


def _clay_miss(request: httpx.Request) -> httpx.Response:
    """Complete Clay without a work email so Apollo becomes eligible."""
    assert request.method in {"POST", "GET"}
    return httpx.Response(
        200,
        json={
            "id": "clay-miss-run",
            "run_id": "clay-miss-run",
            "routine_run_id": "clay-miss-run",
            "status": "completed",
            "credits_used": 1,
            "email": None,
            "work_email": None,
            "results": [],
            "data": {"results": [], "credits_used": 1},
        },
    )


def _clay_email(request: httpx.Request) -> httpx.Response:
    """Complete Clay with one work email so Apollo must not be used."""
    assert request.method in {"POST", "GET"}
    result = {
        "name": "Pat Owner",
        "profile_url": PROFILE,
        "email": EMAIL,
        "work_email": EMAIL,
    }
    return httpx.Response(
        200,
        json={
            "id": "clay-email-run",
            "run_id": "clay-email-run",
            "routine_run_id": "clay-email-run",
            "status": "completed",
            "credits_used": 1,
            "email": EMAIL,
            "work_email": EMAIL,
            "results": [result],
            "data": {"results": [result], "credits_used": 1},
        },
    )


def _values_for_key(value: Any, target: str) -> list[Any]:
    """Collect values for one exact key from arbitrarily nested JSON-like data."""
    found: list[Any] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == target:
                found.append(nested)
            found.extend(_values_for_key(nested, target))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_values_for_key(nested, target))
    return found


def test_clay_persists_routine_run_id_and_resume_get_does_not_post_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted asynchronous Clay routine resumes by GET against the same durable run."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "clay-resume",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    state = {"resume": False}

    def clay(request: httpx.Request) -> httpx.Response:
        """Start pending on POST; fail first-process polling; complete after simulated restart."""
        if request.method == "POST":
            return httpx.Response(
                202,
                json={
                    "id": "routine-123",
                    "run_id": "routine-123",
                    "routine_run_id": "routine-123",
                    "status": "pending",
                    "credits_used": 1,
                },
            )
        assert request.method == "GET"
        if not state["resume"]:
            raise httpx.ReadTimeout("simulated process interruption", request=request)
        return httpx.Response(
            200,
            json={
                "id": "routine-123",
                "run_id": "routine-123",
                "routine_run_id": "routine-123",
                "status": "completed",
                "credits_used": 1,
                "email": EMAIL,
                "work_email": EMAIL,
                "results": [{"profile_url": PROFILE, "work_email": EMAIL}],
            },
        )

    def instantly(request: httpx.Request) -> httpx.Response:
        """Verify the resumed Clay email."""
        return httpx.Response(
            200,
            json={"email": EMAIL, "status": "verified", "credits_used": 1},
        )

    stub = WireStub({"exa": _exa_one, "clay": clay, "instantly": instantly})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    first = call_cli(["enrich", "--run-id", "clay-resume", "--data-root", str(tmp_path)])
    assert first != 0
    checkpoint = run_dir / "contact_checkpoint.json"
    assert checkpoint.exists()
    assert "routine-123" in checkpoint.read_text(encoding="utf-8")

    state["resume"] = True
    assert call_cli(
        ["enrich", "--run-id", "clay-resume", "--data-root", str(tmp_path)]
    ) == 0

    clay_requests = stub.for_provider("clay")
    assert len([request for request in clay_requests if request.method == "POST"]) == 1
    gets = [request for request in clay_requests if request.method == "GET"]
    assert gets
    assert "routine-123" in str(gets[-1].url)
    assert len(stub.for_provider("exa")) == 1


def test_apollo_runs_only_after_clay_miss_with_all_privacy_flags_false_and_no_webhook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apollo fallback is bounded to work email and disables personal/phone/waterfall features."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "apollo-fallback",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )

    def apollo(request: httpx.Request) -> httpx.Response:
        """Return one normal one-credit work-email enrichment."""
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "credits_used": 1,
                "email": EMAIL,
                "person": {"email": EMAIL, "linkedin_url": PROFILE},
            },
        )

    def instantly(request: httpx.Request) -> httpx.Response:
        """Verify Apollo's existing email only."""
        return httpx.Response(
            200,
            json={"email": EMAIL, "status": "verified", "credits_used": 1},
        )

    stub = WireStub(
        {
            "exa": _exa_one,
            "clay": _clay_miss,
            "apollo": apollo,
            "instantly": instantly,
        }
    )
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_cli(
        ["enrich", "--run-id", "apollo-fallback", "--data-root", str(tmp_path)]
    ) == 0

    clay_indexes = [i for i, request in enumerate(stub.requests) if "clay" in request.url.host]
    apollo_indexes = [i for i, request in enumerate(stub.requests) if "apollo" in request.url.host]
    assert clay_indexes and apollo_indexes
    assert min(apollo_indexes) > min(clay_indexes)
    apollo_requests = stub.for_provider("apollo")
    assert len(apollo_requests) == 1
    payload = json_body(apollo_requests[0])
    for flag in (
        "reveal_personal_emails",
        "reveal_phone_number",
        "run_waterfall_email",
        "run_waterfall_phone",
    ):
        values = _values_for_key(payload, flag)
        assert values == [False]
    assert "webhook" not in json.dumps(payload, sort_keys=True).casefold()

    contacts = "\n".join(row_text(row) for row in read_jsonl(run_dir / "contacts.jsonl"))
    assert EMAIL in contacts


def test_unknown_inflight_apollo_outcome_is_accounted_and_never_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout after paid Apollo dispatch fails closed instead of risking a duplicate charge."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "apollo-unknown",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    state = {"resume": False}

    def apollo(request: httpx.Request) -> httpx.Response:
        """Make the first paid outcome unknowable; a replay would succeed and expose the bug."""
        if not state["resume"]:
            raise httpx.ReadTimeout("unknown paid outcome", request=request)
        return httpx.Response(
            200,
            json={"credits_used": 1, "person": {"email": EMAIL}},
        )

    stub = WireStub({"exa": _exa_one, "clay": _clay_miss, "apollo": apollo})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_cli(
        ["enrich", "--run-id", "apollo-unknown", "--data-root", str(tmp_path)]
    ) != 0
    assert len(stub.for_provider("apollo")) == 1
    usage_events = run_dir / "contact_usage_events.jsonl"
    assert usage_events.exists()
    usage_text = usage_events.read_text(encoding="utf-8").casefold()
    assert "apollo" in usage_text
    assert "credit" in usage_text
    assert "1" in usage_text
    contacts = run_dir / "contacts.jsonl"
    assert contacts.exists()
    assert "pat owner" in contacts.read_text(encoding="utf-8").casefold()

    state["resume"] = True
    assert call_cli(
        ["enrich", "--run-id", "apollo-unknown", "--data-root", str(tmp_path)]
    ) != 0
    assert len(stub.for_provider("apollo")) == 1


@pytest.mark.parametrize("status", ["verified", "invalid"])
def test_instantly_uses_only_verification_post_and_persists_terminal_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """Terminal verification statuses use only the allowed POST endpoint and persist exactly."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        f"instantly-{status}",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )

    def instantly(request: httpx.Request) -> httpx.Response:
        """Return one requested terminal verification state."""
        assert request.method == "POST"
        assert request.url.path == "/api/v2/email-verification"
        body = json_body(request)
        assert body.get("email") == EMAIL
        return httpx.Response(
            200,
            json={"email": EMAIL, "status": status, "credits_used": 1},
        )

    stub = WireStub({"exa": _exa_one, "clay": _clay_email, "instantly": instantly})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_cli(
        ["enrich", "--run-id", f"instantly-{status}", "--data-root", str(tmp_path)]
    ) == 0

    instant = stub.for_provider("instantly")
    assert len(instant) == 1
    assert instant[0].method == "POST"
    assert instant[0].url.path == "/api/v2/email-verification"
    persisted = "\n".join(row_text(row).casefold() for row in read_jsonl(run_dir / "contacts.jsonl"))
    assert status in persisted


def test_persisted_pending_instantly_verification_resumes_get_without_second_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending verification is durable and resumes with the exact email-specific GET endpoint."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "instantly-pending",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    state = {"resume": False}

    def instantly(request: httpx.Request) -> httpx.Response:
        """Return pending before restart and verified from the persisted GET afterwards."""
        if request.method == "POST":
            assert request.url.path == "/api/v2/email-verification"
            return httpx.Response(
                202,
                json={"email": EMAIL, "status": "pending", "credits_used": 1},
            )
        assert request.method == "GET"
        assert request.url.path == f"/api/v2/email-verification/{EMAIL}"
        if not state["resume"]:
            raise httpx.ReadTimeout("simulated interruption", request=request)
        return httpx.Response(
            200,
            json={"email": EMAIL, "status": "verified", "credits_used": 0},
        )

    stub = WireStub({"exa": _exa_one, "clay": _clay_email, "instantly": instantly})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_cli(
        ["enrich", "--run-id", "instantly-pending", "--data-root", str(tmp_path)]
    ) != 0
    checkpoint = run_dir / "contact_checkpoint.json"
    assert checkpoint.exists()
    assert EMAIL in checkpoint.read_text(encoding="utf-8")

    state["resume"] = True
    assert call_cli(
        ["enrich", "--run-id", "instantly-pending", "--data-root", str(tmp_path)]
    ) == 0

    instant = stub.for_provider("instantly")
    posts = [request for request in instant if request.method == "POST"]
    gets = [request for request in instant if request.method == "GET"]
    assert len(posts) == 1
    assert gets
    assert gets[-1].url.path == f"/api/v2/email-verification/{EMAIL}"
    allowed = {
        "/api/v2/email-verification",
        f"/api/v2/email-verification/{EMAIL}",
    }
    assert {request.url.path for request in instant} <= allowed


@pytest.mark.parametrize(
    ("provider", "bad_usage"),
    [
        ("apollo", -1),
        ("apollo", float("nan")),
        ("apollo", float("inf")),
        ("apollo", "one"),
        ("instantly", -1),
        ("instantly", float("nan")),
        ("instantly", float("inf")),
        ("instantly", "one"),
    ],
)
def test_malformed_apollo_and_instantly_credit_usage_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    bad_usage: Any,
) -> None:
    """Negative, non-finite, and nonnumeric provider credit usage is never persisted as valid."""
    run_id = f"bad-{provider}-{str(bad_usage).replace('.', '-') }"
    prepare_evaluated_run(
        tmp_path,
        run_id,
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )

    def apollo(request: httpx.Request) -> httpx.Response:
        """Return malformed Apollo usage after a normal Clay miss."""
        return httpx.Response(
            200,
            json={"credits_used": bad_usage, "person": {"email": EMAIL}},
        )

    def instantly(request: httpx.Request) -> httpx.Response:
        """Return malformed Instantly usage for a normal Clay email."""
        return httpx.Response(
            200,
            json={"email": EMAIL, "status": "verified", "credits_used": bad_usage},
        )

    responders: dict[str, Any] = {"exa": _exa_one}
    if provider == "apollo":
        responders.update({"clay": _clay_miss, "apollo": apollo})
    else:
        responders.update({"clay": _clay_email, "instantly": instantly})
    stub = WireStub(responders)
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_cli(["enrich", "--run-id", run_id, "--data-root", str(tmp_path)]) != 0
