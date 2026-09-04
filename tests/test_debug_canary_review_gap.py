"""Temporary diagnostic for the offline canary review-gap contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from m4_contract_fixtures import ClayRoutineScript, WireStub
from test_production_canary_offline_review_gaps import (
    _EMAIL,
    _PROFILE,
    _canonical_and_normal_snapshot,
    _exa_one,
    _install_contract,
    _rejected_company,
    _run_canary,
    _terminal_instantly,
)

from leads_discovery import production_canary
from leads_discovery.pipeline.canary_provider_coverage import run_live_provider_coverage
from leads_discovery.pipeline.state import read_json


def test_diagnose_snapshot_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "canary-review-snapshot-diagnostic"
    clay = ClayRoutineScript([])

    def apollo(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "credits_used": 1,
                "person": {"email": _EMAIL, "linkedin_url": _PROFILE},
            },
        )

    stub = WireStub(
        {
            "exa": _exa_one,
            "clay": clay,
            "apollo": apollo,
            "instantly": _terminal_instantly,
        }
    )
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _rejected_company(), stub)
    failures: list[str] = []
    changes: list[set[str]] = []

    def coverage_with_diagnostics(data_root: Path, *, run_id: str) -> Any:
        try:
            before = _canonical_and_normal_snapshot(data_root / run_id)
            summary = run_live_provider_coverage(data_root, run_id=run_id)
            after = _canonical_and_normal_snapshot(data_root / run_id)
        except Exception as error:
            failures.append(f"{type(error).__name__}: {error}")
            raise
        changes.append({name for name in before if after[name] != before[name]})
        return summary

    monkeypatch.setattr(
        production_canary,
        "run_live_provider_coverage",
        coverage_with_diagnostics,
    )
    code = _run_canary(tmp_path, run_id)
    private = read_json(run_dir / "canary_paid_checkpoint.json")
    assert code == 2, {"failures": failures, "changes": changes, "private": private}
    assert changes == [set()]
