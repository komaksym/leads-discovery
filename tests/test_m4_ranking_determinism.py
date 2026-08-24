"""Focused deterministic-ranking boundary for M4 contact selection."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from m3_factories import accepted_facts, build_company
from m4_contract_fixtures import (
    ClayRoutineScript,
    WireStub,
    call_enrich_live,
    install_mock_http,
    person_result,
    prepare_evaluated_run,
    read_jsonl,
    row_text,
    set_m4_credentials,
)

_NAMES = ("Amy Manager", "Mika Manager", "Zed Manager")


def _apollo_miss(_request: httpx.Request) -> httpx.Response:
    """Return a deterministic one-credit Apollo miss."""
    return httpx.Response(
        200,
        json={"status": "completed", "credits_used": 1, "person": {"email": None}},
    )


def _ordered_names(path: Path) -> list[str]:
    """Extract the persisted contact-name order without assuming a private row schema."""
    ordered: list[str] = []
    for row in read_jsonl(path):
        text = row_text(row).casefold()
        matches = [name for name in _NAMES if name.casefold() in text]
        assert len(matches) == 1
        ordered.append(matches[0])
    return ordered


def _run_tied_order(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
    candidates: list[dict[str, object]],
) -> list[str]:
    """Run one tied ordering through durable Clay start/resume and return contact order."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        run_id,
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )

    def exa(_request: httpx.Request) -> httpx.Response:
        """Serve the requested tied source ordering."""
        return httpx.Response(
            200,
            json={"results": candidates, "costDollars": {"total": 0.001}},
        )

    clay = ClayRoutineScript([])
    stub = WireStub({"exa": exa, "clay": clay, "apollo": _apollo_miss})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_enrich_live(tmp_path, run_id) != 0
    assert len(clay.posts) == 1
    routine_run_id = clay.latest_run_id
    assert routine_run_id is not None
    assert routine_run_id in (run_dir / "contact_checkpoint.json").read_text(encoding="utf-8")
    clay.release_started()
    assert call_enrich_live(tmp_path, run_id) == 0
    assert len(clay.posts) == 1
    assert clay.gets
    assert clay.gets[-1].url.path == (
        f"/public/v0/routines/run/{routine_run_id}/results"
    )
    return _ordered_names(run_dir / "contacts.jsonl")


def test_equal_proximity_ranking_is_independent_of_people_result_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider ordering cannot decide ties, and no rigid owner title is required."""
    candidates = [
        person_result(
            name=name,
            title="Operations Manager",
            company="Acme Valve",
            domain="acmevalve.com",
            profile_url=f"https://www.linkedin.com/in/{name.split()[0].casefold()}-manager",
        )
        for name in _NAMES
    ]

    forward = _run_tied_order(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        run_id="tie-forward",
        candidates=candidates,
    )
    reverse = _run_tied_order(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        run_id="tie-reverse",
        candidates=list(reversed(candidates)),
    )

    assert len(forward) == 3
    assert forward == reverse
    assert set(forward) == set(_NAMES)
