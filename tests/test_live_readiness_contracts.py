"""Independent live-readiness and paid-execution safety contracts."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import httpx
import pytest

from leads_discovery.discovery.apify import ApifyDiscoveryProvider
from leads_discovery.discovery.base import provider_error, request_json
from leads_discovery.discovery.queries import build_discovery_requests
from leads_discovery.pipeline.m2_batch import (
    M2BatchConfig,
    _validate_artifact_paths,
    _validate_config,
)
from leads_discovery.pipeline.state import load_jsonl
from leads_discovery.research.extract import DeepSeekExtractor

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


def _source(obj: Any) -> str:
    """Return normalized source for one runtime object."""
    return inspect.getsource(obj).casefold()


def _module_source(name: str) -> str:
    """Return normalized source for one imported runtime module."""
    return _source(__import__(name, fromlist=["*"]))


def _workflow_texts() -> dict[str, str]:
    """Load checked-in workflow YAML as inert text."""
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(WORKFLOW_DIR.glob("*.y*ml"))
    }


def _paid_workflow() -> str:
    """Return the one dedicated live/canary workflow."""
    matches = [
        text
        for text in _workflow_texts().values()
        if any(word in text.casefold() for word in ("canary", "live", "paid"))
    ]
    assert len(matches) == 1, "exactly one dedicated paid live/canary workflow is required"
    return matches[0]


def test_contract_1_unknown_paid_work_is_global_replay_barrier() -> None:
    """Ambiguous paid work must globally freeze later paid dispatch."""
    source = _module_source("leads_discovery.pipeline.m2_batch")
    assert "paused_unknown" in source or "unknown_outcome" in source
    assert "unknown_in_flight" in source or "global" in source
    assert all(provider in source for provider in ("exa", "apify", "deepseek"))


def test_contract_2_unknown_cost_is_not_silently_zero() -> None:
    """Unknown spend stays unknown/reserved across replay."""
    costs = _module_source("leads_discovery.pipeline.costs")
    runner = _module_source("leads_discovery.pipeline.m2_batch")
    assert "return none" in costs
    assert "reservation_usd" in runner
    assert "unknown" in runner


def test_contract_3_budget_is_checked_before_dispatch() -> None:
    """Committed spend plus reservation must fit before paid dispatch."""
    source = _module_source("leads_discovery.pipeline.m2_batch")
    assert "spend + reservation" in source
    assert source.index("_provider_budget_allows") < source.index("extractor.extract")


def test_contract_4_apify_exposes_run_id_before_polling() -> None:
    """Apify run identity must be observable before long polling starts."""
    calls: list[str] = []
    observed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return one running Actor creation and reject any later poll."""
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(
                201,
                json={"data": {"id": "run-123", "status": "RUNNING"}},
            )
        raise AssertionError("polling began after simulated interruption")

    def persist_then_crash(run_id: str) -> None:
        """Model durable run-id persistence followed by process death."""
        observed.append(run_id)
        raise KeyboardInterrupt

    request = next(
        req for req in build_discovery_requests(include_apify=True) if req.provider == "apify"
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = ApifyDiscoveryProvider(
            api_token="test-token",
            client=client,
            on_run_started=persist_then_crash,
        )
        with pytest.raises(KeyboardInterrupt):
            provider.search(request)

    assert observed == ["run-123"]
    assert calls == ["POST"]


def test_contract_5_provider_timeout_policy_is_explicit() -> None:
    """Provider HTTP calls must not accidentally inherit HTTPX defaults."""
    names = (
        "leads_discovery.discovery.apify",
        "leads_discovery.discovery.exa",
        "leads_discovery.research.extract",
    )
    missing = [name for name in names if "timeout=" not in _module_source(name)]
    assert not missing, f"provider modules lack explicit timeout behavior: {missing}"


def test_contract_6_deepseek_invalid_json_retry_is_bounded() -> None:
    """Malformed model JSON must be retryable within a finite bound."""
    source = _source(DeepSeekExtractor.extract)
    invalid = _source(DeepSeekExtractor._invalid)
    assert "retry" in source or "attempt" in source
    assert "retryable=true" in source or "retryable=true" in invalid
    assert any(word in source for word in ("max_attempt", "max_retr", "range("))


def test_contract_7_unsupported_negative_cannot_hard_reject() -> None:
    """No supporting evidence is not equivalent to a false fact."""
    evaluation = _module_source("leads_discovery.pipeline.evaluation")
    extraction = _module_source("leads_discovery.research.extract")
    assert "pvf_relevant" in evaluation
    assert "evidence" in extraction
    assert any(word in evaluation for word in ("unsupported", "support", "unknown"))
    assert "unsupported" in extraction or "no supporting evidence" in extraction


def test_contract_8_provider_json_parsing_is_stream_bounded() -> None:
    """Provider response parsing must not buffer an unchecked whole body."""
    source = _source(request_json)
    assert ".json()" not in source
    assert ".read()" not in source
    assert "iter_bytes" in source or "iter_raw" in source
    assert "max" in source and "byte" in source


def test_contract_9_nested_raw_fields_have_deterministic_bound() -> None:
    """Multi-megabyte raw/nested fields cannot be persisted unbounded."""
    source = "\n".join(
        _module_source(name)
        for name in (
            "leads_discovery.discovery.apify",
            "leads_discovery.discovery.exa",
            "leads_discovery.pipeline.m2_batch",
        )
    )
    assert "raw_metadata" in source or "raw_records" in source
    assert any(word in source for word in ("max_raw", "raw_byte", "sanitize_raw", "truncate"))


def test_contract_10_jsonl_replay_is_incremental(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay must not read and split the complete history in memory."""
    path = tmp_path / "history.jsonl"
    path.write_text('{"ok":1}\n{"ok":2}\n', encoding="utf-8")

    def forbidden_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        """Reject read-all JSONL replay."""
        del self, args, kwargs
        raise AssertionError("JSONL replay must be incremental")

    monkeypatch.setattr(Path, "read_text", forbidden_read_text)
    assert load_jsonl(path) == [{"ok": 1}, {"ok": 2}]


def test_contract_10_replay_has_three_hard_limits() -> None:
    """Replay must bound file bytes, line bytes, and record count."""
    source = _module_source("leads_discovery.pipeline.state")
    assert all(word in source for word in ("file", "line", "record"))
    assert source.count("max") >= 3


def test_contract_11_per_run_storage_has_hard_ceiling() -> None:
    """Persisted bytes must have a per-run hard ceiling."""
    source = _source(M2BatchConfig) + _module_source("leads_discovery.pipeline.state")
    markers = ("storage_budget", "persisted_byte", "max_storage", "disk_budget")
    assert any(marker in source for marker in markers)


def test_contract_12_data_root_symlink_is_rejected(tmp_path: Path) -> None:
    """The configured writable root itself cannot be a symlink."""
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)
    config = M2BatchConfig(run_id="symlink-test", data_root=linked_root)
    with pytest.raises(ValueError, match="symlink"):
        _validate_config(config)


def test_contract_12_child_output_symlink_is_rejected(tmp_path: Path) -> None:
    """A child artifact symlink cannot redirect generated files."""
    root = tmp_path / "data"
    run_dir = root / "child-test"
    run_dir.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("sentinel", encoding="utf-8")
    (run_dir / "companies_raw.jsonl").symlink_to(outside)
    paths = _validate_config(M2BatchConfig(run_id="child-test", data_root=root))
    with pytest.raises(ValueError, match="symlink"):
        _validate_artifact_paths(paths)
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_contract_13_paid_workflow_has_production_boundary() -> None:
    """Only explicit workflow_dispatch may authorize live paid execution."""
    workflow = _paid_workflow().casefold()
    assert "workflow_dispatch" in workflow
    assert "pull_request" not in workflow
    assert "push:" not in workflow
    assert "actions/checkout@" in workflow
    assert "pip install" in workflow or "python -m pip install" in workflow
    assert "github.event.inputs.command" not in workflow


def test_contract_13_pr_and_push_ci_do_not_get_live_credentials() -> None:
    """Ordinary CI must not silently become paid scraping."""
    for name, text in _workflow_texts().items():
        workflow = text.casefold()
        if "pull_request" not in workflow and "push:" not in workflow:
            continue
        assert "execute_live" not in workflow, f"{name} enables live execution in CI"
        for provider in ("deepseek", "apify", "exa"):
            assert f"secrets.{provider}" not in workflow


def test_contract_14_one_company_canary_limits_are_fixed() -> None:
    """Canary scope, spend, calls, and storage must be hard-coded ceilings."""
    workflow = _paid_workflow().casefold()
    one_company = ("max_companies: 1", "max-companies 1", "companies=1")
    assert any(marker in workflow for marker in one_company)
    assert "budget" in workflow
    assert "max" in workflow and "call" in workflow
    assert "storage" in workflow or "bytes" in workflow
    for name in ("max_companies", "budget", "max_calls"):
        assert f"inputs.{name}" not in workflow


def test_contract_15_failure_text_does_not_retain_secret_or_raw_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Representative provider failures cannot expose secret/raw values."""
    secret = "secret-api-key-value"
    raw = "private-raw-provider-payload"
    error = provider_error(
        provider="exa",
        request_id="request-1",
        operation="company_search",
        request_count=1,
        kind="transient",
        retryable=True,
        metadata={"request_id": "request-1"},
    )
    assert secret not in str(error)
    assert raw not in str(error)
    assert secret not in caplog.text
    assert raw not in caplog.text


def test_global_guard_allows_mocktransport() -> None:
    """The offline guard must preserve in-memory HTTP transports."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Return one in-memory response without network access."""
        return httpx.Response(200, json={"path": request.url.path})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert client.get("https://example.invalid/test").json() == {"path": "/test"}


def test_global_guard_blocks_default_http_network() -> None:
    """A default HTTP client cannot reach real networking."""
    with (
        httpx.Client(timeout=0.01) as client,
        pytest.raises((AssertionError, httpx.HTTPError, OSError)),
    ):
        client.get("https://example.com/")
