"""Regression contracts for normal-provider evidence used by canary coverage."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from leads_discovery.models import CompanyRecord, RunCheckpoint
from leads_discovery.pipeline.canary_provider_coverage import run_provider_coverage
from leads_discovery.pipeline.state import write_checkpoint, write_jsonl_atomic


def test_completed_normal_exa_operation_without_usage_fails_closed(tmp_path: Path) -> None:
    """A checkpoint entry alone cannot suppress coverage without authoritative usage."""
    run_id = "missing-normal-usage"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    company = CompanyRecord(
        company_id="cmp_acme",
        name="Acme Valve",
        normalized_name="acme valve",
        domain="acme.com",
        normalized_domain="acme.com",
        country="US",
    )
    company.final_decision = "accepted"
    company.final_score = 9.0
    company.stage_status["decision"] = "completed"
    write_jsonl_atomic(run_dir / "companies_evaluated.jsonl", [company.to_dict()])
    write_jsonl_atomic(run_dir / "contacts.jsonl", [])
    write_checkpoint(
        run_dir / "checkpoint.json",
        RunCheckpoint(run_id=run_id, status="completed", provider_state={"operations": {}}),
    )
    write_checkpoint(
        run_dir / "contact_checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            status="completed",
            provider_state={
                "operations": {
                    f"exa:{company.company_id}": {
                        "provider": "exa",
                        "operation": "people_search",
                        "state": "completed",
                        "contact_ids": [],
                    }
                }
            },
        ),
    )

    unavailable = cast(Any, object())
    with pytest.raises(ValueError, match="authoritative usage"):
        run_provider_coverage(
            run_dir,
            run_id=run_id,
            exa=unavailable,
            clay=unavailable,
            apollo=unavailable,
            instantly=unavailable,
        )
