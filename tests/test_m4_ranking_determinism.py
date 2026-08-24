"""Focused deterministic-ranking boundary for M4 contact selection."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from m3_factories import accepted_facts, build_company
from m4_contract_fixtures import (
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


def _ordered_names(path: Path) -> list[str]:
    """Extract the persisted contact-name order without assuming a private row schema."""
    ordered: list[str] = []
    for row in read_jsonl(path):
        text = row_text(row).casefold()
        matches = [name for name in _NAMES if name.casefold() in text]
        assert len(matches) == 1
        ordered.append(matches[0])
    return ordered


def test_equal_proximity_ranking_is_independent_of_people_result_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rank-three ties retain deterministically without becoming paid-enrichment eligible."""
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

    stub = WireStub({"exa": exa})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_enrich_live(tmp_path, "tie-forward") == 0
    assert call_enrich_live(tmp_path, "tie-reverse") == 0

    forward = _ordered_names(forward_dir / "contacts.jsonl")
    reverse = _ordered_names(reverse_dir / "contacts.jsonl")
    assert len(forward) == 3
    assert forward == reverse
    assert set(forward) == set(_NAMES)
    assert exa_calls == 2
    assert stub.for_provider("clay") == []
    assert stub.for_provider("apollo") == []
