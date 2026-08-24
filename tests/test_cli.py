"""Frozen-contract tests for M3 CLI orchestration, exit codes, budgets, and offline boundaries."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import leads_discovery.cli as cli
import pytest
from m3_factories import accepted_facts, build_company, write_run_inputs

import leads_discovery.discovery as discovery_module
import leads_discovery.pipeline.m2_batch as m2_module
import leads_discovery.research as research_module
from leads_discovery.models import RunCheckpoint
from leads_discovery.pipeline.m2_batch import M2BatchConfig
from leads_discovery.pipeline.state import load_checkpoint

_PROVIDER_KEYS = {"EXA_API_KEY", "APIFY_TOKEN", "DEEPSEEK_API_KEY"}


def _call(argv: Sequence[str]) -> int:
    """Invoke the public CLI entry and normalize parser exits to an integer."""
    try:
        return cli.main(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1


def _summary(capsys: pytest.CaptureFixture[str]) -> tuple[dict[str, Any], str]:
    """Parse the single stdout JSON object and retain all process text for leak checks."""
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload, captured.out + captured.err


def _block_credential_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail local/dry paths if any provider credential key is inspected."""
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


class _BombProvider:
    """Provider constructor that fails if any local/dry path instantiates it."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("provider client must not be instantiated")


class _DummyProvider:
    """No-op provider adapter accepted by the fake M2 runner."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class _DummyHttp:
    """Context-manager HTTP client that never performs network I/O."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _DummyHttp:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


def _patch_providers(monkeypatch: pytest.MonkeyPatch, cls: type[Any]) -> None:
    """Patch provider constructors at package and CLI composition boundaries."""
    for module, names in (
        (discovery_module, ["ExaDiscoveryProvider", "ApifyDiscoveryProvider"]),
        (research_module, ["ExaEvidenceResearcher", "DeepSeekExtractor"]),
        (cli, [
            "ExaDiscoveryProvider",
            "ApifyDiscoveryProvider",
            "ExaEvidenceResearcher",
            "DeepSeekExtractor",
        ]),
    ):
        for name in names:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, cls)


def _patch_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace composition-time HTTP client construction with a zero-network fake."""
    monkeypatch.setattr(httpx, "Client", _DummyHttp)
    if hasattr(cli, "httpx"):
        monkeypatch.setattr(cli.httpx, "Client", _DummyHttp)


def _patch_m2(monkeypatch: pytest.MonkeyPatch, fake: Callable[..., RunCheckpoint]) -> None:
    """Patch the frozen M2 runner at both source and likely imported alias."""
    monkeypatch.setattr(m2_module, "run_m2_batch", fake)
    if hasattr(cli, "run_m2_batch"):
        monkeypatch.setattr(cli, "run_m2_batch", fake)


def _checkpoint(
    config: M2BatchConfig,
    *,
    status: str,
    include_company: bool = True,
    pending: str | None = None,
    stage: str | None = None,
    reason: str | None = None,
) -> RunCheckpoint:
    """Persist coherent fake M2 state and return the exact durable checkpoint."""
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


def test_run_dry_run_is_authorized_offline_zero_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without execute-live, run returns 0 before credentials, clients, files, or providers."""
    _block_credential_reads(monkeypatch)
    _patch_providers(monkeypatch, _BombProvider)
    code = _call(
        [
            "run", "--run-id", "dry", "--data-root", str(tmp_path),
            "--deepseek-budget-usd", "1.00",
        ]
    )
    payload, _ = _summary(capsys)
    assert code == 0
    assert "dry" in json.dumps(payload).lower()
    assert not (tmp_path / "dry").exists()


def test_score_and_calibrate_are_offline_and_do_not_mutate_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Local commands read no credentials, create no providers/network, and emit no usage."""
    _block_credential_reads(monkeypatch)
    _patch_providers(monkeypatch, _BombProvider)
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
        ["calibrate", "--run-id", "local", "--data-root", str(tmp_path), "--labels", str(labels)]
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
    """Local provider flags, aggregate budgets, and invalid state/input use exit code 1."""
    assert _call(argv) == 1


def test_completed_live_run_maps_cap_and_independent_budgets_then_evaluates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Full success forwards three independent ceilings, maps max-evaluated, and returns 0."""
    captured: dict[str, Any] = {}

    def fake(config: M2BatchConfig, **_kwargs: Any) -> RunCheckpoint:
        """Capture M2 config and persist one completed extraction."""
        captured["config"] = config
        return _checkpoint(config, status="completed")

    _patch_m2(monkeypatch, fake)
    _patch_providers(monkeypatch, _DummyProvider)
    _patch_http(monkeypatch)
    monkeypatch.setenv("EXA_API_KEY", "exa-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv("APIFY_TOKEN", "apify-secret")

    code = _call(
        [
            "run", "--run-id", "complete", "--data-root", str(tmp_path),
            "--max-candidates", "17", "--max-evaluated", "7",
            "--include-apify", "--apify-budget-usd", ".20",
            "--exa-budget-usd", "1.25", "--deepseek-budget-usd", "2.50",
            "--execute-live",
        ]
    )
    payload, process_text = _summary(capsys)
    config = captured["config"]
    assert isinstance(config, M2BatchConfig)
    assert config.max_candidates == 17
    assert config.max_extracted == 7
    assert config.include_apify is True
    assert config.apify_budget_usd == pytest.approx(.20)
    assert config.exa_budget_usd == pytest.approx(1.25)
    assert config.deepseek_budget_usd == pytest.approx(2.50)
    assert not hasattr(config, "budget_usd")
    assert code == 0
    assert all(
        secret not in process_text
        for secret in ("exa-secret", "deepseek-secret", "apify-secret")
    )
    assert payload
    run_dir = tmp_path / "complete"
    assert (run_dir / "companies_evaluated.jsonl").exists()
    checkpoint = load_checkpoint(run_dir / "checkpoint.json")
    assert checkpoint is not None and checkpoint.status == "completed"
    assert checkpoint.provider_state["stages"]["evaluation"] == "completed"
    assert checkpoint.provider_state["stages"]["m3_pipeline"] == "completed"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("paused_budget", "deepseek_budget_exhausted"),
        ("paused_unknown", "unknown_in_flight:extraction:cmp_contract"),
    ],
)
def test_paid_pause_evaluates_completed_extractions_and_returns_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    reason: str,
) -> None:
    """Completed free work is exported after budget/unknown pauses while pause state survives."""
    def fake(config: M2BatchConfig, **_kwargs: Any) -> RunCheckpoint:
        """Persist one completed extraction under the requested durable pause."""
        return _checkpoint(
            config,
            status=status,
            pending="cmp_next",
            stage="extraction",
            reason=reason,
        )

    _patch_m2(monkeypatch, fake)
    _patch_providers(monkeypatch, _DummyProvider)
    _patch_http(monkeypatch)
    monkeypatch.setenv("EXA_API_KEY", "test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")

    run_id = status.replace("_", "-")
    code = _call(
        [
            "run", "--run-id", run_id, "--data-root", str(tmp_path),
            "--deepseek-budget-usd", "1", "--execute-live",
        ]
    )
    _summary(capsys)
    assert code == 2
    run_dir = tmp_path / run_id
    rows = (run_dir / "companies_evaluated.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    checkpoint = load_checkpoint(run_dir / "checkpoint.json")
    assert checkpoint is not None
    assert checkpoint.status == status
    assert checkpoint.pending_company_id == "cmp_next"
    assert checkpoint.pending_stage == "extraction"
    assert checkpoint.pause_reason == reason


def test_failed_paid_run_does_not_evaluate_and_returns_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Failed paid state returns 1 and does not derive M3 artifacts."""
    def fake(config: M2BatchConfig, **_kwargs: Any) -> RunCheckpoint:
        """Persist a failed M2 checkpoint with no completed extraction."""
        return _checkpoint(config, status="failed", include_company=False, reason="authentication")

    _patch_m2(monkeypatch, fake)
    _patch_providers(monkeypatch, _DummyProvider)
    _patch_http(monkeypatch)
    monkeypatch.setenv("EXA_API_KEY", "test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    code = _call(
        [
            "run", "--run-id", "failed", "--data-root", str(tmp_path),
            "--deepseek-budget-usd", "1", "--execute-live",
        ]
    )
    _summary(capsys)
    assert code == 1
    assert not (tmp_path / "failed" / "companies_evaluated.jsonl").exists()


def test_missing_required_live_credentials_returns_one_without_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Live execution requires Exa and DeepSeek credentials before client construction."""
    _patch_providers(monkeypatch, _BombProvider)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    code = _call(
        [
            "run", "--run-id", "missing-creds", "--data-root", str(tmp_path),
            "--deepseek-budget-usd", "1", "--execute-live",
        ]
    )
    _summary(capsys)
    assert code == 1


def test_missing_optional_apify_credential_disables_apify_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Absent optional Apify token disables Apify without changing its independent ceiling."""
    captured: dict[str, Any] = {}

    def fake(config: M2BatchConfig, **_kwargs: Any) -> RunCheckpoint:
        """Capture the post-credential M2 config and complete."""
        captured["config"] = config
        return _checkpoint(config, status="completed")

    _patch_m2(monkeypatch, fake)
    _patch_providers(monkeypatch, _DummyProvider)
    _patch_http(monkeypatch)
    monkeypatch.setenv("EXA_API_KEY", "test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    code = _call(
        [
            "run", "--run-id", "no-apify", "--data-root", str(tmp_path),
            "--include-apify", "--apify-budget-usd", ".19",
            "--deepseek-budget-usd", "1", "--execute-live",
        ]
    )
    _summary(capsys)
    config = captured["config"]
    assert code == 0
    assert config.include_apify is False
    assert config.apify_budget_usd == pytest.approx(.19)
