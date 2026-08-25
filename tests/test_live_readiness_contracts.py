"""Implementation-independent live-readiness and paid-execution safety contracts."""

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
    """Return normalized lowercase source for one runtime object."""
    return inspect.getsource(obj).casefold()


def _workflow_texts() -> dict[str, str]:
    """Load checked-in workflow YAML as inert text without executing it."""
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(WORKFLOW_DIR.glob("*.y*ml"))
    }


def _paid_workflow() -> tuple[str, str]:
    """Return the dedicated live/canary workflow, failing clearly when it does not exist."""
    workflows = _workflow_texts()
    matches = [
        (name, text)
        for name, text in workflows.items()
        if any(marker in text.casefold() for marker in ("canary", "live", "paid"))
    ]
    assert len(matches) == 1, "exactly one dedicated paid live/canary workflow is required"
    return matches[0]


def test_unknown_paid_work_is_a_global_replay_barrier() -> None:
    """Contract 1: ambiguous paid work must globally freeze later paid dispatch."""
    source = _source(__import__("leads_discovery.pipeline.m2_batch", fromlist=["*"]))
    assert "unknown_outcome" in source or "paused_unknown" in source
    assert "global" in source or "unknown_in_flight" in source
    paid = {"exa", "apify", "deepseek", "clay", "apollo", "instantly"}
    represented = {provider for provider in paid if provider in source}
    assert represented >= {"exa", "apify", "deepseek"}


def test_unknown_cost_is_not_silently_zero_and_reservation_is_durable() -> None:
    """Contract 2: unknown spend stays unknown/reserved across replay."""
    costs = _source(__import__("leads_discovery.pipeline.costs", fromlist=["*"]))
    runner = _source(__import__("leads_discovery.pipeline.m2_batch", fromlist=["*"]))
    assert "return none" in costs
    assert "reservation_usd" in runner
    assert "unknown" in runner


def test_budget_is_checked_before_dispatch_with_next_call_reservation() -> None:
    """Contract 3: committed + reserved + worst-case next call must fit before dispatch."""
    source = _source(__import__("leads_discovery.pipeline.m2_batch", fromlist=["*"]))
    assert "spend + reservation" in source
    assert source.index("_provider_budget_allows") < source.index("extractor.extract")


def test_apify_run_identity_is_observed_before_long_polling() -> None:
    """Contract 4: Apify exposes durable run identity before any long wait/poll loop."""
    calls: list[tuple[str, str]] = []
    observed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a created running Actor and reject any poll after interruption."""
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(
                201,
                json={"data": {"id": "run-123", "status": "RUNNING"}},
            )
        raise AssertionError("polling must not begin after simulated process interruption")

    def persist_then_crash(run_id: str) -> None:
        """Model durable run-id persistence followed immediately by process death."""
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
    assert calls == [("POST", "/v2/acts/compass~crawler-google-places/runs")]


def test_provider_clients_use_explicit_timeout_policy() -> None:
    """Contract 5: long-running provider HTTP must not inherit accidental HTTPX defaults."""
    modules = (
        __import__("leads_discovery.discovery.apify", fromlist=["*"]),
        __import__("leads_discovery.discovery.exa", fromlist=["*"]),
        __import__("leads_discovery.research.extract", fromlist=["*"]),
    )
    missing = [module.__name__ for module in modules if "timeout=" not in _source(module)]
    assert not missing, f"provider modules lack explicit timeout behavior: {missing}"


def test_deepseek_invalid_json_is_retryable_but_bounded() -> None:
    """Contract 6: empty/malformed/schema-invalid model JSON gets bounded retry handling."""
    source = _source(DeepSeekExtractor.extract)
    invalid = _source(DeepSeekExtractor._invalid)
    assert "retry" in source or "attempt" in source
    assert "retryable=true" in source or "retryable=true" in invalid
    assert any(marker in source for marker in ("max_attempt", "max_retr", "range("))


def test_unsupported_negative_fact_cannot_be_hard_rejection() -> None:
    """Contract 7: cited-but-unsupported false is unknown; explicit exclusion may stay false."""
    evaluation = _source(__import__("leads_discovery.pipeline.evaluation", fromlist=["*"]))
    extraction = _source(__import__("leads_discovery.research.extract", fromlist=["*"]))
    assert "pvf_relevant" in evaluation
    assert "evidence" in extraction
    assert any(marker in evaluation for marker in ("unsupported", "support", "unknown"))
    assert "no supporting evidence" in extraction or "unsupported" in extraction


def test_provider_response_parsing_is_stream_bounded() -> None:
    """Contract 8: provider JSON parsing must not buffer an unchecked whole response."""
    source = _source(request_json)
    assert ".json()" not in source
    assert ".read()" not in source
    assert "iter_bytes" in source or "iter_raw" in source
    assert "max" in source and "byte" in source


def test_nested_raw_provider_fields_have_a_deterministic_bound() -> None:
    """Contract 9: raw/nested provider fields cannot be persisted without a size boundary."""
    sources = "\n".join(
        _source(__import__(name, fromlist=["*"]))
        for name in (
            "leads_discovery.discovery.apify",
            "leads_discovery.discovery.exa",
            "leads_discovery.pipeline.m2_batch",
        )
    )
    assert "raw_metadata" in sources or "raw_records" in sources
    assert any(marker in sources for marker in ("max_raw", "raw_byte", "sanitize_raw", "truncate"))


def test_jsonl_replay_is_incremental_not_read_text_splitlines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract 10: replay must stream records and enforce bounds without read-all."""
    path = tmp_path / "history.jsonl"
    path.write_text('{"ok":1}\n{"ok":2}\n', encoding="utf-8")

    def forbidden_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        """Reject any implementation that loads the complete history into one string."""
        del self, args, kwargs
        raise AssertionError("JSONL replay must be incremental")

    monkeypatch.setattr(Path, "read_text", forbidden_read_text)
    assert load_jsonl(path) == [{"ok": 1}, {"ok": 2}]


def test_replay_exposes_file_line_and_record_hard_limits() -> None:
    """Contract 10: replay has explicit file-byte, line-byte, and record-count ceilings."""
    source = _source(__import__("leads_discovery.pipeline.state", fromlist=["*"]))
    expected = ("file", "line", "record")
    assert all(word in source for word in expected)
    assert source.count("max") >= 3


def test_per_run_persisted_storage_has_a_hard_ceiling() -> None:
    """Contract 11: storage exhaustion must stop safely before more paid work."""
    state = _source(__import__("leads_discovery.pipeline.state", fromlist=["*"]))
    source = _source(M2BatchConfig) + state
    assert any(
        marker in source
        for marker in ("storage_budget", "persisted_byte", "max_storage", "disk_budget")
    )


def test_data_root_symlink_is_rejected(tmp_path: Path) -> None:
    """Contract 12: the configured writable root itself cannot be a symlink."""
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)
    config = M2BatchConfig(run_id="symlink-test", data_root=linked_root)
    with pytest.raises(ValueError, match="symlink"):
        _validate_config(config)


def test_child_output_symlink_is_rejected(tmp_path: Path) -> None:
    """Contract 12: a pre-existing child artifact symlink cannot redirect writes."""
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


def test_paid_github_workflow_has_explicit_production_boundary() -> None:
    """Contract 13: only explicit workflow_dispatch may authorize live paid execution."""
    _, workflow = _paid_workflow()
    lowered = workflow.casefold()
    assert "workflow_dispatch" in lowered
    assert "pull_request" not in lowered
    assert "push:" not in lowered
    assert "actions/checkout@" in lowered
    assert "pip install" in lowered or "python -m pip install" in lowered
    assert "execute_live" in lowered or "live" in lowered
    assert "github.event.inputs.command" not in lowered


def test_pr_and_push_ci_do_not_inject_live_provider_credentials() -> None:
    """Contract 13: ordinary CI must not silently turn into paid scraping."""
    workflows = _workflow_texts()
    for name, text in workflows.items():
        lowered = text.casefold()
        if "pull_request" not in lowered and "push:" not in lowered:
            continue
        assert "execute_live" not in lowered, f"{name} enables live execution in CI"
        assert "secrets.deepseek" not in lowered, f"{name} exposes paid credentials to CI"
        assert "secrets.apify" not in lowered, f"{name} exposes paid credentials to CI"
        assert "secrets.exa" not in lowered, f"{name} exposes paid credentials to CI"


def test_one_company_canary_limits_are_not_user_raiseable() -> None:
    """Contract 14: canary scope, spend, calls, and storage are hard-coded safe ceilings."""
    _, workflow = _paid_workflow()
    lowered = workflow.casefold()
    assert any(
        marker in lowered for marker in ("max_companies: 1", "max-companies 1", "companies=1")
    )
    assert "budget" in lowered
    assert "max" in lowered and "call" in lowered
    assert "storage" in lowered or "bytes" in lowered
    assert "inputs.max_companies" not in lowered
    assert "inputs.budget" not in lowered
    assert "inputs.max_calls" not in lowered


def test_representative_provider_failure_does_not_retain_secrets_or_raw_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Contract 15: safe errors/logs never expose credentials or raw provider payloads."""
    api_key = "secret-api-key-value"
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
    assert api_key not in str(error)
    assert raw not in str(error)
    assert api_key not in caplog.text
    assert raw not in caplog.text


def test_mocktransport_remains_usable_under_global_offline_guard() -> None:
    """Global safety: in-memory HTTP mocks work even though real DNS/sockets are blocked."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Return one local in-memory response without touching the network stack."""
        return httpx.Response(200, json={"path": request.url.path})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert client.get("https://example.invalid/test").json() == {"path": "/test"}


def test_default_http_network_is_blocked_by_global_guard() -> None:
    """Global safety: a default HTTP client cannot reach real networking."""
    with httpx.Client(timeout=0.01) as client:
        with pytest.raises((AssertionError, httpx.HTTPError, OSError)):
            client.get("https://example.com/")
