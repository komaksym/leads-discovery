"""Regression contracts for normal-M4 async polling across canary invocations."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from m4_contract_fixtures import ClayRoutineScript, WireStub, json_body
from test_production_canary_offline_contract import (
    _EMAIL,
    _accepted_company,
    _exa_one,
    _install_contract,
    _run_canary,
)

from leads_discovery import production_canary


def test_pending_clay_read_ceiling_survives_canary_process_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted Clay run gets at most three status GET attempts for its lifetime."""
    run_id = "normal-clay-lifetime-ceiling"
    clay = ClayRoutineScript([{"work_email": _EMAIL}])
    stub = WireStub({"exa": _exa_one, "clay": clay})
    _install_contract(monkeypatch, tmp_path, run_id, _accepted_company(), stub)
    monkeypatch.setattr(production_canary, "sleep", lambda _delay: None, raising=False)

    assert _run_canary(tmp_path, run_id) == 2
    assert len(clay.posts) == 1
    assert len(clay.gets) == 3

    assert _run_canary(tmp_path, run_id) == 2
    assert len(clay.posts) == 1
    assert len(clay.gets) == 3


def test_pending_instantly_read_ceiling_survives_canary_process_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted Instantly verification gets three GET attempts without another POST."""
    run_id = "normal-instantly-lifetime-ceiling"
    clay = ClayRoutineScript([{"work_email": _EMAIL}])
    instantly_posts: list[httpx.Request] = []
    instantly_gets: list[httpx.Request] = []

    def instantly(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/email-verification"
        if request.method == "POST":
            instantly_posts.append(request)
            assert json_body(request)["email"] == _EMAIL
            return httpx.Response(
                202,
                json={
                    "email": _EMAIL,
                    "verification_status": "pending",
                    "credits_used": 1,
                },
            )
        assert request.method == "GET"
        instantly_gets.append(request)
        return httpx.Response(
            202,
            json={
                "email": _EMAIL,
                "verification_status": "pending",
                "credits_used": 0,
            },
        )

    stub = WireStub({"exa": _exa_one, "clay": clay, "instantly": instantly})
    _install_contract(monkeypatch, tmp_path, run_id, _accepted_company(), stub)
    sleeps = 0

    def release_clay_then_continue(_delay: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            clay.release_started()

    monkeypatch.setattr(
        production_canary,
        "sleep",
        release_clay_then_continue,
        raising=False,
    )

    assert _run_canary(tmp_path, run_id) == 2
    assert len(clay.posts) == 1
    assert len(instantly_posts) == 1
    assert len(instantly_gets) == 3

    assert _run_canary(tmp_path, run_id) == 2
    assert len(clay.posts) == 1
    assert len(instantly_posts) == 1
    assert len(instantly_gets) == 3
