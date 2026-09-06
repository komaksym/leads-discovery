"""Regression coverage for normal-M4 poll-ceiling report scope."""

from __future__ import annotations

from pathlib import Path

import pytest
from m4_contract_fixtures import ClayRoutineScript, WireStub
from test_production_canary_offline_contract import (
    _EMAIL,
    _accepted_company,
    _exa_one,
    _install_contract,
    _provider,
    _report,
    _run_canary,
)

from leads_discovery import production_canary


def test_budget_pause_does_not_broaden_poll_ceiling_inconclusive_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only pending polling may defer missing Apollo coverage; budget pauses stay failed."""
    run_id = "normal-budget-report-scope"
    clay = ClayRoutineScript([{"work_email": _EMAIL}])
    stub = WireStub({"exa": _exa_one, "clay": clay})
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _accepted_company(), stub)
    sleeps: list[float] = []

    def release_clay(delay: float) -> None:
        sleeps.append(delay)
        clay.release_started()

    monkeypatch.setattr(production_canary, "sleep", release_clay, raising=False)
    monkeypatch.setattr(production_canary, "_INSTANTLY_CALL_CAP", "0")

    assert _run_canary(tmp_path, run_id) == 1
    assert len(clay.posts) == 1
    assert len(clay.gets) == 1
    assert stub.for_provider("instantly") == []

    report = _report(run_dir)
    assert report["overall_outcome"] == "failure"
    assert "contact_budget_blocked" in report["safety_flags"]
    assert _provider(report, "apollo")["integration_outcome"] == "failure"
