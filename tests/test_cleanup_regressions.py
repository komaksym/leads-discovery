"""Focused regressions for repository-cleanup safety boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from leads_discovery.models import DiscoveryRecord, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.m2_batch import M2BatchConfig, resolve_m2_paths, run_m2_batch
from leads_discovery.pipeline.state import append_jsonl, append_usage_event, write_checkpoint
from leads_discovery.research.evidence import ExaEvidenceResearcher


def _seed_research_run(tmp_path: Path, run_id: str, spend: float) -> None:
    """Seed completed discovery with one company and known prior Exa spend."""
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    record = DiscoveryRecord(
        record_id="raw_000000000000000000000001",
        provider="exa",
        request_id="exa:us:test:v1",
        target_country_code="US",
        query="pvf distributor",
        provider_result_id="provider-1",
        name="Acme PVF",
        source_url="https://acme.test/source",
        website_url="https://acme.test",
        city="Houston",
        region="TX",
        postal_code="77001",
        country_code="US",
        title="Acme PVF",
        snippet="Industrial pipe valves fittings distributor",
        raw_metadata={"id": "provider-1"},
        retrieved_at="2026-08-25T00:00:00+00:00",
    )
    append_jsonl(run_dir / "companies_raw.jsonl", record.to_dict())
    append_usage_event(
        run_dir / "usage_events.jsonl",
        UsageEvent(
            provider="exa",
            operation="company_search",
            request_count=1,
            estimated_cost_usd=spend,
        ),
    )
    write_checkpoint(
        run_dir / "checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            provider_state={"operations": {}, "stages": {"discovery": "completed"}},
        ),
    )


class _BombExtractor:
    """Prove the reservation tests never reach structured extraction."""

    def reservation_cost_usd(self, *_args: Any) -> float:
        raise AssertionError("empty research must stop before extraction")

    def extract(self, *_args: Any) -> Any:
        raise AssertionError("empty research must stop before extraction")


@pytest.mark.parametrize(
    ("prior_spend", "expected_calls"),
    [
        pytest.param(0.70, 3, id="below-budget"),
        pytest.param(0.80, 3, id="exact-budget"),
        pytest.param(0.81, 0, id="above-budget"),
        pytest.param(0.90, 0, id="near-limit-plus-reservation"),
    ],
)
def test_exa_admission_includes_next_request_reservation(
    tmp_path: Path,
    prior_spend: float,
    expected_calls: int,
) -> None:
    """Dispatch only when known spend plus one request reservation fits the ceiling."""
    run_id = f"exa-reservation-{int(prior_spend * 100)}"
    _seed_research_run(tmp_path, run_id, prior_spend)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"results": [], "costDollars": {"total": 0.0}},
        )

    config = M2BatchConfig(
        run_id=run_id,
        data_root=tmp_path,
        max_candidates=1,
        max_extracted=1,
        deepseek_budget_usd=1.0,
        exa_budget_usd=1.0,
        exa_request_reservation_usd=0.20,
        execute_live=True,
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        checkpoint = run_m2_batch(
            config,
            discovery={},
            researcher=ExaEvidenceResearcher(api_key="test", client=client),
            extractor=_BombExtractor(),  # type: ignore[arg-type]
        )

    assert calls == expected_calls
    if expected_calls == 0:
        assert checkpoint.status == "paused_budget"
        assert checkpoint.pause_reason == "exa_budget_exhausted_or_unknown"
    else:
        assert checkpoint.status == "completed"
        assert checkpoint.pause_reason == "empty_evidence"


@pytest.mark.parametrize(
    ("budget", "reservation"),
    [
        pytest.param(None, 0.20, id="missing-budget"),
        pytest.param(1.0, None, id="missing-reservation"),
        pytest.param(1.0, 0.0, id="zero-reservation"),
        pytest.param(1.0, float("inf"), id="nonfinite-reservation"),
    ],
)
def test_live_exa_requires_bounded_budget_and_per_request_reservation(
    tmp_path: Path,
    budget: float | None,
    reservation: float | None,
) -> None:
    """Live Exa work must fail configuration when one request cannot be bounded safely."""
    config = M2BatchConfig(
        run_id="bounded-exa",
        data_root=tmp_path,
        deepseek_budget_usd=1.0,
        exa_budget_usd=budget,
        exa_request_reservation_usd=reservation,
        execute_live=True,
    )
    with pytest.raises(ValueError):
        resolve_m2_paths(config)
