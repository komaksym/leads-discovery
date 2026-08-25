"""CLI contracts for offline behavior, live composition, exit states, and output safety."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from m3_factories import accepted_facts, build_company, write_run_inputs

import leads_discovery.cli as cli
from leads_discovery.models import RunCheckpoint
from leads_discovery.pipeline.m2_batch import (
    LiveM2Result,
    M2BatchConfig,
    MissingProviderCredentials,
)
from leads_discovery.pipeline.state import load_checkpoint

_PROVIDER_KEYS = {"EXA_API_KEY", "APIFY_TOKEN", "DEEPSEEK_API_KEY"}


def _call(argv: Sequence[str]) -> int:
    """Invoke the public CLI and normalize parser exits."""
    try:
        return cli.main(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1


def _summary(capsys: pytest.CaptureFixture[str]) -> tuple[dict[str, Any], str]:
    """Parse the single sanitized stdout JSON object."""
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload, captured.out + captured.err


def _block_credential_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail local/dry paths if provider credentials are inspected."""
    env_type = type(os.environ)
    original_get = env_type.get
    original_getitem = env_type.__getitem__

    def guarded_get(env: Any, key: str, default: str | None = None) -> str | None:
        if key in _PROVIDER_KEYS:
            raise AssertionError(f"credential read forbidden: {key}")
        return original_get(env, key, default)

    def guarded_getitem(env: Any, key: str) -> str:
        if key in _PROVIDER_KEYS:
            raise AssertionError(f"credential read forbidden: {key}")
        return original_getitem(env, key)

    monkeypatch.setattr(env_type, "get", guarded_get)
    monkeypatch.setattr(env_type, "__getitem__", guarded_getitem)


def _checkpoint(
    config: M2BatchConfig,
    *,
    status: str,
    include_company: bool = True,
    pending: str | None = None,
    stage: str | None = None,
    reason: str | None = None,
) -> RunCheckpoint:
    """Persist coherent fake M2 state for CLI-level orchestration tests."""
    companies = [build_company(facts=accepted_facts())] if include_company else []
    write_run_inputs(
        config.data_root,
        config.run_id,
        companies,
        checkpoint_status=status,
        pending_company_id=pending,
        pending_stage=stage,
        pause_reason=reason,
    )
    checkpoint = load_checkpoint(config.data_root / config.run_id / "checkpoint.json")
    assert checkpoint is not None
    return checkpoint


def _live_args(tmp_path: Path, run_id: str) -> list[str]:
    """Return the smallest fully bounded live CLI invocation."""
    return [
        "run",
        "--run-id",
        run_id,
        "--data-root",
        str(tmp_path),
        "--exa-budget-usd",
        "1.00",
        "--exa-request-reservation-usd",
        "0.10",
        "--deepseek-budget-usd",
        "1.00",
        "--execute-live",
    ]


def test_run_dry_is_offline_and_reads_no_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dry run returns before credentials, filesystem mutation, or live composition."""
    _block_credential_reads(monkeypatch)

    def bomb(_config: M2BatchConfig) -> LiveM2Result:
        raise AssertionError("dry run must not compose live providers")

    monkeypatch.setattr(cli, "run_live_m2", bomb)
    code = _call(
        [
            "run",
            "--run-id",
            "dry",
            "--data-root",
            str(tmp_path),
            "--deepseek-budget-usd",
            "1.00",
        ]
    )
    payload, _ = _summary(capsys)
    assert code == 0
    assert payload["status"] == "dry_run"
    assert not (tmp_path / "dry").exists()


def test_score_and_calibrate_are_offline_and_do_not_mutate_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Local commands read no credentials and leave both usage artifacts unchanged."""
    _block_credential_reads(monkeypatch)
    run_dir = write_run_inputs(tmp_path, "local", [build_company(facts=accepted_facts())])
    usage_before = (run_dir / "usage.json").read_bytes()
    ledger_before = (run_dir / "usage_events.jsonl").read_bytes()

    assert _call(["score", "--run-id", "local", "--data-root", str(tmp_path)]) == 0
    _summary(capsys)
    assert (run_dir / "usage.json").read_bytes() == usage_before
    assert (run_dir / "usage_events.jsonl").read_bytes() == ledger_before

    labels = tmp_path / "labels.csv"
    labels.write_text("company_id,manual_label\ncmp_contract,A\n", encoding="utf-8")
    assert _call(
        [
            "calibrate",
            "--run-id",
            "local",
            "--data-root",
            str(tmp_path),
            "--labels",
            str(labels),
        ]
    ) == 0
    _summary(capsys)
    assert (run_dir / "usage.json").read_bytes() == usage_before
    assert (run_dir / "usage_events.jsonl").read_bytes() == ledger_before


@pytest.mark.parametrize(
    "argv",
    [
        ["score", "--run-id", "x", "--exa-budget-usd", "1"],
        ["calibrate", "--run-id", "x", "--labels", "x.csv", "--deepseek-budget-usd", "1"],
        ["run", "--run-id", "x", "--deepseek-budget-usd", "1", "--budget-usd", "3"],
        ["score", "--run-id", "../escape"],
    ],
)
def test_invalid_or_forbidden_cli_inputs_exit_one(argv: list[str]) -> None:
    """Invalid state/input is reported with exit code one."""
    assert _call(argv) == 1


def test_completed_live_run_maps_bounded_config_and_evaluates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Successful live composition forwards caps/budgets and completes M3."""
    captured: dict[str, M2BatchConfig] = {}

    def fake(config: M2BatchConfig) -> LiveM2Result:
        captured["config"] = config
        return LiveM2Result(
            checkpoint=_checkpoint(config, status="completed"),
            apify_enabled=config.include_apify,
        )

    monkeypatch.setattr(cli, "run_live_m2", fake)
    argv = _live_args(tmp_path, "complete")
    argv[argv.index("--execute-live"):argv.index("--execute-live")] = [
        "--max-candidates",
        "17",
        "--max-evaluated",
        "7",
        "--include-apify",
        "--apify-budget-usd",
        ".20",
    ]
    code = _call(argv)
    payload, _ = _summary(capsys)
    config = captured["config"]

    assert code == 0
    assert config.max_candidates == 17
    assert config.max_extracted == 7
    assert config.include_apify is True
    assert config.apify_budget_usd == pytest.approx(0.20)
    assert config.exa_budget_usd == pytest.approx(1.00)
    assert config.exa_request_reservation_usd == pytest.approx(0.10)
    assert config.deepseek_budget_usd == pytest.approx(1.00)
    assert payload["status"] == "completed"
    checkpoint = load_checkpoint(tmp_path / "complete" / "checkpoint.json")
    assert checkpoint is not None
    assert checkpoint.provider_state["stages"]["evaluation"] == "completed"
    assert checkpoint.provider_state["stages"]["m3_pipeline"] == "completed"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("paused_budget", "deepseek_budget_exhausted"),
        ("paused_unknown", "unknown_in_flight:extraction:cmp_contract"),
    ],
)
def test_paid_pause_exports_completed_work_and_returns_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    reason: str,
) -> None:
    """Free derived work is exported while the original paid pause survives."""

    def fake(config: M2BatchConfig) -> LiveM2Result:
        return LiveM2Result(
            checkpoint=_checkpoint(
                config,
                status=status,
                pending="cmp_next",
                stage="extraction",
                reason=reason,
            ),
            apify_enabled=False,
        )

    monkeypatch.setattr(cli, "run_live_m2", fake)
    run_id = status.replace("_", "-")
    assert _call(_live_args(tmp_path, run_id)) == 2
    _summary(capsys)
    checkpoint = load_checkpoint(tmp_path / run_id / "checkpoint.json")
    assert checkpoint is not None
    assert checkpoint.status == status
    assert checkpoint.pending_company_id == "cmp_next"
    assert checkpoint.pending_stage == "extraction"
    assert checkpoint.pause_reason == reason


def test_failed_paid_run_does_not_evaluate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Failed paid state returns one and publishes no M3 artifact."""

    def fake(config: M2BatchConfig) -> LiveM2Result:
        return LiveM2Result(
            checkpoint=_checkpoint(
                config,
                status="failed",
                include_company=False,
                reason="authentication",
            ),
            apify_enabled=False,
        )

    monkeypatch.setattr(cli, "run_live_m2", fake)
    assert _call(_live_args(tmp_path, "failed")) == 1
    _summary(capsys)
    assert not (tmp_path / "failed" / "companies_evaluated.jsonl").exists()


def test_missing_required_live_credentials_returns_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing live credentials map to one sanitized CLI failure."""

    def missing(_config: M2BatchConfig) -> LiveM2Result:
        raise MissingProviderCredentials

    monkeypatch.setattr(cli, "run_live_m2", missing)
    assert _call(_live_args(tmp_path, "missing-creds")) == 1
    payload, _ = _summary(capsys)
    assert payload["reason"] == "required_provider_credentials_missing"


def test_programming_errors_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unexpected defects remain visible instead of becoming generic operation failures."""

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("programming defect")

    monkeypatch.setattr(cli, "evaluate_run", broken)
    with pytest.raises(RuntimeError, match="programming defect"):
        cli.main(["score", "--run-id", "valid-run"])
