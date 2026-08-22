"""Durable file-backed state helpers for resumable pipeline runs."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from leads_discovery.models import CompanyRecord, RunCheckpoint


def append_company_snapshot(path: Path, company: CompanyRecord) -> None:
    """Append and fsync one company snapshot so completed paid work survives interruption."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(company.to_dict(), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_latest_company_records(path: Path) -> dict[str, CompanyRecord]:
    """Load latest company snapshots while tolerating only a torn final JSONL append."""
    if not path.exists():
        return {}

    lines = path.read_text(encoding="utf-8").splitlines()
    latest: dict[str, CompanyRecord] = {}
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                break
            raise
        company = CompanyRecord.from_dict(payload)
        latest[company.company_id] = company
    return latest


def stage_completed(path: Path, company_id: str, stage: str) -> bool:
    """Return whether the latest persisted company snapshot marks a stage completed."""
    company = load_latest_company_records(path).get(company_id)
    return company is not None and company.stage_status.get(stage) == "completed"


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace a JSON artifact so readers never observe a partial document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON artifact as a dictionary, or return None when it does not exist."""
    if not path.exists():
        return None
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def write_checkpoint(path: Path, checkpoint: RunCheckpoint) -> None:
    """Atomically replace the run checkpoint with a fully written JSON document."""
    write_json_atomic(path, checkpoint.to_dict())


def load_checkpoint(path: Path) -> RunCheckpoint | None:
    """Load a persisted run checkpoint, or return None when no checkpoint exists."""
    payload = read_json(path)
    return None if payload is None else RunCheckpoint.from_dict(payload)
