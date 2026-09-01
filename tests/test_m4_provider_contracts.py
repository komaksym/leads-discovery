"""Independent provider-wire and paid-resume contracts for M4."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from m3_factories import accepted_facts, build_company
from m4_contract_fixtures import (
    ClayRoutineScript,
    WireStub,
    call_enrich_live,
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


def _exa_one(_request: httpx.Request) -> httpx.Response:
    """Serve one current high-proximity decision maker from Exa People Search."""
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


def _email_rows() -> list[dict[str, Any]]:
    """Return one completed Clay work-email row."""
    return [{"profile_url": PROFILE, "work_email": EMAIL, "email": EMAIL}]


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


def _assert_clay_started_durably(run_dir: Path, clay: ClayRoutineScript) -> str:
    """Require one pending Clay POST and its routine_run_id in durable checkpoint state."""
    assert len(clay.posts) == 1
    assert clay.gets == []
    run_id = clay.latest_run_id
    assert run_id is not None
    checkpoint = run_dir / "contact_checkpoint.json"
    assert checkpoint.exists()
    assert run_id in checkpoint.read_text(encoding="utf-8")
    return run_id


def test_clay_post_persists_run_id_then_resume_gets_same_run_without_second_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interrupted Clay work persists POST state and resumes the same routine exclusively by GET."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "clay-resume",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    clay = ClayRoutineScript(_email_rows())

    def instantly(_request: httpx.Request) -> httpx.Response:
        """Verify the email only after the resumed Clay GET completes."""
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "verification_status": "verified",
                "credits_used": 1,
                "email": EMAIL,
            },
        )

    stub = WireStub({"exa": _exa_one, "clay": clay, "instantly": instantly})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_enrich_live(tmp_path, "clay-resume") != 0
    routine_run_id = _assert_clay_started_durably(run_dir, clay)

    clay.release_started()
    assert call_enrich_live(tmp_path, "clay-resume") == 0

    assert len(clay.posts) == 1
    assert clay.gets
    assert clay.gets[-1].url.path == (
        f"/public/v0/routines/run/{routine_run_id}/results"
    )
    assert len(stub.for_provider("exa")) == 1


def test_pending_clay_post_is_not_treated_as_synchronous_email_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending POST response cannot produce verification or a lead before a resumed GET."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "clay-pending",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    clay = ClayRoutineScript(_email_rows())
    stub = WireStub({"exa": _exa_one, "clay": clay})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_enrich_live(tmp_path, "clay-pending") != 0
    _assert_clay_started_durably(run_dir, clay)
    assert stub.for_provider("instantly") == []
    assert EMAIL not in (run_dir / "contacts.jsonl").read_text(encoding="utf-8")
    leads = run_dir / "leads.csv"
    assert not leads.exists() or EMAIL not in leads.read_text(encoding="utf-8")


def test_apollo_fallback_has_all_privacy_flags_false_and_no_webhook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apollo runs only after a resumed Clay miss and disables personal/phone/waterfall features."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "apollo-fallback",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    clay = ClayRoutineScript([])

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

    def instantly(_request: httpx.Request) -> httpx.Response:
        """Verify Apollo's existing email only."""
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "verification_status": "verified",
                "credits_used": 1,
                "email": EMAIL,
            },
        )

    stub = WireStub(
        {"exa": _exa_one, "clay": clay, "apollo": apollo, "instantly": instantly}
    )
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_enrich_live(tmp_path, "apollo-fallback") != 0
    _assert_clay_started_durably(run_dir, clay)
    assert stub.for_provider("apollo") == []

    clay.release_started()
    assert call_enrich_live(tmp_path, "apollo-fallback") == 0

    clay_indexes = [
        i for i, request in enumerate(stub.requests) if "clay" in request.url.host
    ]
    apollo_indexes = [
        i for i, request in enumerate(stub.requests) if "apollo" in request.url.host
    ]
    assert clay_indexes and apollo_indexes
    assert min(apollo_indexes) > min(clay_indexes)
    assert len(clay.posts) == 1

    apollo_requests = stub.for_provider("apollo")
    assert len(apollo_requests) == 1
    payload = json_body(apollo_requests[0])
    for flag in (
        "reveal_personal_emails",
        "reveal_phone_number",
        "run_waterfall_email",
        "run_waterfall_phone",
    ):
        assert _values_for_key(payload, flag) == [False]
    assert "webhook" not in json.dumps(payload, sort_keys=True).casefold()

    contacts = "\n".join(
        row_text(row) for row in read_jsonl(run_dir / "contacts.jsonl")
    )
    assert EMAIL in contacts


def test_unknown_inflight_apollo_outcome_is_accounted_and_never_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout after Apollo dispatch fails closed instead of risking a second charge."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "apollo-unknown",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    clay = ClayRoutineScript([])
    allow_apollo_success = False

    def apollo(request: httpx.Request) -> httpx.Response:
        """Make the first paid outcome unknowable; replay would expose the bug."""
        if not allow_apollo_success:
            raise httpx.ReadTimeout("unknown paid outcome", request=request)
        return httpx.Response(200, json={"credits_used": 1, "person": {"email": EMAIL}})

    stub = WireStub({"exa": _exa_one, "clay": clay, "apollo": apollo})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_enrich_live(tmp_path, "apollo-unknown") != 0
    _assert_clay_started_durably(run_dir, clay)
    clay.release_started()

    assert call_enrich_live(tmp_path, "apollo-unknown") != 0
    assert len(stub.for_provider("apollo")) == 1
    usage_events = run_dir / "contact_usage_events.jsonl"
    assert usage_events.exists()
    usage_text = usage_events.read_text(encoding="utf-8").casefold()
    assert "apollo" in usage_text
    assert "credit" in usage_text
    assert "1" in usage_text

    allow_apollo_success = True
    assert call_enrich_live(tmp_path, "apollo-unknown") != 0
    assert len(stub.for_provider("apollo")) == 1


@pytest.mark.parametrize("verification_status", ["verified", "invalid"])
def test_instantly_verification_post_persists_terminal_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verification_status: str,
) -> None:
    """Terminal Instantly state comes from verification_status after resumed Clay results."""
    run_id = f"instantly-{verification_status}"
    run_dir = prepare_evaluated_run(
        tmp_path,
        run_id,
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    clay = ClayRoutineScript(_email_rows())

    def instantly(request: httpx.Request) -> httpx.Response:
        """Return a deliberately distinct request status and verification status."""
        assert request.method == "POST"
        assert request.url.path == "/api/v2/email-verification"
        assert json_body(request).get("email") == EMAIL
        return httpx.Response(
            200,
            json={
                "email": EMAIL,
                "status": "completed",
                "verification_status": verification_status,
                "credits_used": 1,
            },
        )

    stub = WireStub({"exa": _exa_one, "clay": clay, "instantly": instantly})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_enrich_live(tmp_path, run_id) != 0
    _assert_clay_started_durably(run_dir, clay)
    clay.release_started()
    assert call_enrich_live(tmp_path, run_id) == 0

    instant = stub.for_provider("instantly")
    assert len(instant) == 1
    assert instant[0].method == "POST"
    assert instant[0].url.path == "/api/v2/email-verification"
    persisted = "\n".join(
        row_text(row).casefold() for row in read_jsonl(run_dir / "contacts.jsonl")
    )
    assert verification_status in persisted


def test_pending_instantly_verification_resumes_get_without_second_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending Instantly state resumes by email GET after Clay has already resumed."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "instantly-pending",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    clay = ClayRoutineScript(_email_rows())
    allow_verified = False

    def instantly(request: httpx.Request) -> httpx.Response:
        """Return pending verification before restart and verified from the persisted GET later."""
        if request.method == "POST":
            assert request.url.path == "/api/v2/email-verification"
            return httpx.Response(
                202,
                json={
                    "email": EMAIL,
                    "status": "completed",
                    "verification_status": "pending",
                    "credits_used": 1,
                },
            )
        assert request.method == "GET"
        assert request.url.path == f"/api/v2/email-verification/{EMAIL}"
        if not allow_verified:
            return httpx.Response(
                202,
                json={
                    "email": EMAIL,
                    "status": "completed",
                    "verification_status": "pending",
                    "credits_used": 0,
                },
            )
        return httpx.Response(
            200,
            json={
                "email": EMAIL,
                "status": "completed",
                "verification_status": "verified",
                "credits_used": 0,
            },
        )

    stub = WireStub({"exa": _exa_one, "clay": clay, "instantly": instantly})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_enrich_live(tmp_path, "instantly-pending") != 0
    _assert_clay_started_durably(run_dir, clay)
    clay.release_started()

    assert call_enrich_live(tmp_path, "instantly-pending") != 0
    instant = stub.for_provider("instantly")
    assert len([request for request in instant if request.method == "POST"]) == 1
    checkpoint = run_dir / "contact_checkpoint.json"
    assert EMAIL in checkpoint.read_text(encoding="utf-8")

    allow_verified = True
    assert call_enrich_live(tmp_path, "instantly-pending") == 0

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
        ("apollo", float("-inf")),
        ("apollo", "one"),
        ("instantly", -1),
        ("instantly", float("nan")),
        ("instantly", float("inf")),
        ("instantly", float("-inf")),
        ("instantly", "one"),
    ],
)
def test_malformed_apollo_and_instantly_credit_usage_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    bad_usage: Any,
) -> None:
    """Negative, non-finite, and nonnumeric provider credit usage fails closed."""
    suffix = str(bad_usage).replace(".", "-")
    run_id = f"bad-{provider}-{suffix}"
    run_dir = prepare_evaluated_run(
        tmp_path,
        run_id,
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    clay = ClayRoutineScript([] if provider == "apollo" else _email_rows())

    def apollo(_request: httpx.Request) -> httpx.Response:
        """Return malformed Apollo usage after a resumed Clay miss."""
        return httpx.Response(
            200,
            json={"credits_used": bad_usage, "person": {"email": EMAIL}},
        )

    def instantly(_request: httpx.Request) -> httpx.Response:
        """Return malformed Instantly usage after resumed Clay email discovery."""
        return httpx.Response(
            200,
            json={
                "email": EMAIL,
                "status": "completed",
                "verification_status": "verified",
                "credits_used": bad_usage,
            },
        )

    responders: dict[str, Any] = {"exa": _exa_one, "clay": clay}
    if provider == "apollo":
        responders["apollo"] = apollo
    else:
        responders["instantly"] = instantly
    stub = WireStub(responders)
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_enrich_live(tmp_path, run_id) != 0
    _assert_clay_started_durably(run_dir, clay)
    clay.release_started()
    assert call_enrich_live(tmp_path, run_id) != 0
