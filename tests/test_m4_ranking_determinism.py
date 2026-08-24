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


def _resume_one_clay_run(
    *,
    tmp_path: Path,
    run_dir: Path,
    run_id: str,
    clay: ClayRoutineScript,
    expected_post_count: int,
) -> None:
    """Require one newly started routine to become durable and complete only after resume."""
    assert call_enrich_live(tmp_path, run_id) != 0
    assert len(clay.posts) == expected_post_count
    routine_run_id = clay.latest_run_id
    assert routine_run_id is not None
    checkpoint = run_dir / "contact_checkpoint.json"
    assert routine_run_id in checkpoint.read_text(encoding="utf-8")
    clay.release_started()
    assert call_enrich_live(tmp_path, run_id) == 0
    assert len(clay.posts) == expected_post_count
    assert clay.gets[-1].url.path == (
        f"/public/v0/routines/run/{routine_run_id}/results"
    )


def test_equal_proximity_ranking_is_independent_of_people_result_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider ordering cannot decide ties, and no rigid owner title is required."""
    forward_dir = prepare_evaluated_run(
        tmp_path,
        "tie-forward",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    reverse_dir = prepare_evaluated_run(
        tmp_path,
        "tie-reverse",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
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
    exa_calls = 0

    def exa(_request: httpx.Request) -> httpx.Response:
        """Serve identical tied candidates in opposite source orders across the two runs."""
        nonlocal exa_calls
        exa_calls += 1
        results = candidates if exa_calls == 1 else list(reversed(candidates))
        return httpx.Response(
            200,
            json={"results": results, "costDollars": {"total": 0.001}},
        )

    clay = ClayRoutineScript([])
    stub = WireStub({"exa": exa, "clay": clay, "apollo": _apollo_miss})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    _resume_one_clay_run(
        tmp_path=tmp_path,
        run_dir=forward_dir,
        run_id="tie-forward",
        clay=clay,
        expected_post_count=1,
    )
    _resume_one_clay_run(
        tmp_path=tmp_path,
        run_dir=reverse_dir,
        run_id="tie-reverse",
        clay=clay,
        expected_post_count=2,
    )

    forward = _ordered_names(forward_dir / "contacts.jsonl")
    reverse = _ordered_names(reverse_dir / "contacts.jsonl")
    assert len(forward) == 3
    assert forward == reverse
    assert set(forward) == set(_NAMES)
    assert exa_calls == 2
