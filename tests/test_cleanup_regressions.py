"""Focused regressions for repository cleanup safety boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from leads_discovery.models import DiscoveryRecord, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.m2_batch import M2BatchConfig, run_m2_batch
from leads_discovery.pipeline.state import append_jsonl, append_usage_event, write_checkpoint


def _seed_research_run(tmp_path: Path, run_id: str) -> Path:
    """Seed a completed discovery stage with one deterministic company and prior Exa spend."""
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
            estimated_cost_usd=0.90,
        ),
    )
    write_checkpoint(
        run_dir / "checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            provider_state={"operations": {}, "stages": {"discovery": "completed"}},
        ),
    )
    return run_dir


def test_exa_reservation_blocks_dispatch_before_ceiling_can_be_overshot(tmp_path: Path) -> None:
    """Admission must include the next Exa request reservation, not only already-spent cost."""
    run_id = "exa-reservation"
    _seed_research_run(tmp_path, run_id)

    class BombResearcher:
        def research(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("Exa dispatch must be rejected before crossing the budget")

    checkpoint = run_m2_batch(
        M2BatchConfig(
            run_id=run_id,
            data_root=tmp_path,
            max_candidates=1,
            max_extracted=1,
            deepseek_budget_usd=1.0,
            exa_budget_usd=1.0,
            exa_request_reservation_usd=0.20,
            execute_live=True,
        ),
        discovery={},
        researcher=BombResearcher(),  # type: ignore[arg-type]
        extractor=object(),  # type: ignore[arg-type]
    )

    assert checkpoint.status == "paused_budget"
    assert checkpoint.pause_reason == "exa_budget_exhausted_or_unknown"
    persisted = json.loads((tmp_path / run_id / "checkpoint.json").read_text())
    assert persisted["status"] == "paused_budget"
