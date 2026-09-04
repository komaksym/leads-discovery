"""Highest-seam offline transport-safety contract for the fixed production canary."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from m4_contract_fixtures import ClayRoutineScript, WireStub, read_jsonl
from test_production_canary_offline_contract import (
    _exa_one,
    _install_contract,
    _provider,
    _rejected_company,
    _report,
    _run_canary,
)


def test_coverage_transport_safety_failure_is_provider_and_overall_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded-response safety rejection is provider failure and makes the canary fail."""
    run_id = "canary-coverage-transport-safety"
    clay = ClayRoutineScript([])

    def apollo(_request: httpx.Request) -> httpx.Response:
        monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "8")
        return httpx.Response(200, json={"credits_used": 1, "person": None})

    stub = WireStub({"exa": _exa_one, "clay": clay, "apollo": apollo})
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _rejected_company(), stub)

    assert _run_canary(tmp_path, run_id) == 2
    clay.release_started()
    assert _run_canary(tmp_path, run_id) == 1
    assert len(stub.for_provider("apollo")) == 1
    assert len(stub.for_provider("instantly")) == 0

    report = _report(run_dir)
    assert report["pipeline_outcome"] == "inconclusive"
    assert report["overall_outcome"] == "failure"
    assert "coverage_paid_outcome_unresolved" in report["safety_flags"]
    assert _provider(report, "apollo")["integration_outcome"] == "failure"

    request_count = len(stub.requests)
    assert _run_canary(tmp_path, run_id) == 1
    assert len(stub.requests) == request_count
    assert read_jsonl(run_dir / "contacts.jsonl") == []
