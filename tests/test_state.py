from __future__ import annotations

from pathlib import Path

from leads_discovery.models import CompanyRecord, RunCheckpoint
from leads_discovery.pipeline.state import (
    append_company_snapshot,
    load_checkpoint,
    load_latest_company_records,
    stage_completed,
    write_checkpoint,
)


def test_latest_company_snapshot_drives_resume_state(tmp_path: Path) -> None:
    """The newest snapshot for a company must control whether a stage resumes."""
    path = tmp_path / "companies.jsonl"
    pending = CompanyRecord(
        company_id="acme",
        name="Acme PVF",
        stage_status={"research": "pending"},
    )
    completed = CompanyRecord(
        company_id="acme",
        name="Acme PVF",
        stage_status={"research": "completed"},
    )

    append_company_snapshot(path, pending)
    append_company_snapshot(path, completed)

    latest = load_latest_company_records(path)
    assert latest["acme"].stage_status["research"] == "completed"
    assert stage_completed(path, "acme", "research") is True
    assert stage_completed(path, "acme", "extract") is False


def test_checkpoint_write_replaces_previous_state_atomically(tmp_path: Path) -> None:
    """Checkpoint replacement must leave one valid latest JSON document."""
    path = tmp_path / "checkpoint.json"

    write_checkpoint(path, RunCheckpoint(run_id="calibration-001", status="running"))
    write_checkpoint(
        path,
        RunCheckpoint(
            run_id="calibration-001",
            status="paused_budget",
            pending_company_id="acme",
            pending_stage="extract",
            pause_reason="deepseek_budget_exhausted",
        ),
    )

    checkpoint = load_checkpoint(path)
    assert checkpoint is not None
    assert checkpoint.status == "paused_budget"
    assert checkpoint.pending_company_id == "acme"
    assert checkpoint.pending_stage == "extract"
    assert list(tmp_path.glob("checkpoint.json.*.tmp")) == []


def test_loader_ignores_only_a_torn_final_jsonl_record(tmp_path: Path) -> None:
    """A crash during the final append must not hide earlier completed snapshots."""
    path = tmp_path / "companies.jsonl"
    append_company_snapshot(path, CompanyRecord(company_id="acme", name="Acme PVF"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"company_id":"broken"')

    latest = load_latest_company_records(path)

    assert list(latest) == ["acme"]


def test_atomic_json_helpers_persist_usage_payload(tmp_path: Path) -> None:
    """Generic atomic JSON persistence must support usage and future run artifacts."""
    from leads_discovery.pipeline.state import read_json, write_json_atomic

    path = tmp_path / "usage.json"
    payload = {"total": {"request_count": 3, "estimated_cost_usd": 0.05}}

    write_json_atomic(path, payload)

    assert read_json(path) == payload
