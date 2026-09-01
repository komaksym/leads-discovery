"""Independent live-readiness and paid-execution safety contracts."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from leads_discovery.discovery.apify import ApifyDiscoveryProvider
from leads_discovery.discovery.base import DiscoveryProviderError, provider_error, request_json
from leads_discovery.discovery.queries import build_discovery_requests
from leads_discovery.models import (
    CompanyRecord,
    EvidenceBundle,
    EvidenceItem,
    ExtractedFact,
    ExtractionResult,
    RunCheckpoint,
    UsageEvent,
)
from leads_discovery.pipeline.m2_batch import (
    M2BatchConfig,
    run_m2_batch,
)
from leads_discovery.pipeline.state import (
    load_jsonl,
    write_checkpoint,
    write_jsonl_atomic,
    write_text_atomic,
)
from leads_discovery.research.extract import (
    FACT_KEYS,
    DeepSeekExtractor,
    DeepSeekPriceSchedule,
    apply_extraction,
)
from leads_discovery.scoring import evaluate_company

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


def _workflow_texts() -> dict[str, str]:
    """Load checked-in workflow YAML as inert text."""
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(WORKFLOW_DIR.glob("*.y*ml"))
    }


def _paid_workflow() -> str:
    """Return the one workflow whose trigger is manual and whose job runs the fixed canary."""
    matches: list[str] = []
    for text in _workflow_texts().values():
        trigger = text.split("jobs:", 1)[0].casefold()
        if "workflow_dispatch:" in trigger and "production_canary" in text.casefold():
            matches.append(text)
    assert len(matches) == 1, "exactly one dedicated paid live/canary workflow is required"
    return matches[0]


def _deepseek_company() -> CompanyRecord:
    """Build the smallest company accepted by the DeepSeek adapter boundary."""
    return CompanyRecord(
        company_id="cmp_live_readiness",
        name="Readiness Valve",
        domain="readiness.example",
        normalized_domain="readiness.example",
    )


def _deepseek_bundle(
    excerpt: str = "Readiness Valve distributes industrial valves.",
) -> EvidenceBundle:
    """Build one bounded retained-evidence bundle for DeepSeek and evidence tests."""
    return EvidenceBundle(
        company_id="cmp_live_readiness",
        items=[
            EvidenceItem(
                evidence_id="ev_live_readiness",
                url="https://readiness.example/about",
                excerpt=excerpt,
                provider="exa",
            )
        ],
        raw_records=[],
        usage_events=[],
    )


def _deepseek_response(content: str) -> dict[str, Any]:
    """Build one syntactically valid provider envelope around supplied model content."""
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 1,
        },
    }


def _valid_deepseek_content() -> str:
    """Return a complete schema-valid model result containing only unknown facts."""
    facts: dict[str, dict[str, object]] = {
        key: {"value": None, "confidence": 0, "evidence_ids": []}
        for key in FACT_KEYS
    }
    return json.dumps({"facts": facts})


def _evaluate_pvf_false(excerpt: str) -> CompanyRecord:
    """Apply one cited negative model fact through the production extraction boundary."""
    facts = {key: ExtractedFact(None, 0.0, []) for key in FACT_KEYS}
    facts["pvf_relevant"] = ExtractedFact(False, 0.95, ["ev_live_readiness"])
    bundle = _deepseek_bundle(excerpt)
    extracted = apply_extraction(
        _deepseek_company(),
        bundle,
        ExtractionResult(
            company_id="cmp_live_readiness",
            model="deepseek-v4-flash",
            facts=facts,
            usage_event=UsageEvent(
                provider="deepseek",
                operation="structured_extraction",
            ),
        ),
    )
    return evaluate_company(extracted)


def test_contract_1_unknown_paid_work_is_global_replay_barrier(tmp_path: Path) -> None:
    """Ambiguous paid work must globally freeze later paid dispatch."""
    run_dir = tmp_path / "live-readiness-barrier"
    run_dir.mkdir()
    write_checkpoint(
        run_dir / "checkpoint.json",
        RunCheckpoint(
            run_id="live-readiness-barrier",
            status="running",
            provider_state={
                "operations": {
                    "research:lost": {
                        "provider": "exa",
                        "operation": "company_research",
                        "state": "in_flight",
                    }
                },
                "stages": {},
            },
        ),
    )
    checkpoint = run_m2_batch(
        M2BatchConfig(
            run_id="live-readiness-barrier",
            data_root=tmp_path,
            deepseek_budget_usd=1.0,
            execute_live=True,
        ),
        discovery={},
        researcher=None,  # type: ignore[arg-type]
        extractor=None,  # type: ignore[arg-type]
    )
    assert checkpoint.status == "paused_unknown"
    assert checkpoint.pause_reason == "unknown_in_flight:research:lost"


def test_contract_2_unknown_cost_is_not_silently_zero() -> None:
    """Unknown spend stays unknown/reserved across replay."""
    # Cost replay itself is exercised behaviorally by the budget-gate tests in
    # the M2 suite; keep this contract focused on the user-visible unknown value.
    from leads_discovery.pipeline.costs import CostTracker

    tracker = CostTracker(
        [UsageEvent(provider="exa", operation="search", estimated_cost_usd=None)]
    )
    assert tracker.provider_estimated_spend("exa") is None


def test_contract_4_apify_exposes_run_id_before_polling_and_resumes_it() -> None:
    """Apify must persist a run identity before waiting and resume that exact run."""
    start_calls: list[str] = []
    observed: list[str] = []

    def start_handler(request: httpx.Request) -> httpx.Response:
        """Return one running Actor creation and reject any later poll."""
        start_calls.append(request.method)
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
    with httpx.Client(transport=httpx.MockTransport(start_handler)) as client:
        provider = ApifyDiscoveryProvider(
            api_token="test-token",
            client=client,
            on_run_started=persist_then_crash,
        )
        with pytest.raises(KeyboardInterrupt):
            provider.search(request)

    assert observed == ["run-123"]
    assert start_calls == ["POST"]

    resume_calls: list[str] = []

    def resume_handler(req: httpx.Request) -> httpx.Response:
        """Serve only the persisted run and its existing dataset."""
        resume_calls.append(req.method)
        assert req.method == "GET", "restart must not create a replacement Actor run"
        if "actor-runs" in req.url.path:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "run-123",
                        "status": "SUCCEEDED",
                        "defaultDatasetId": "dataset-123",
                    }
                },
            )
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(resume_handler)) as client:
        provider = ApifyDiscoveryProvider(api_token="test-token", client=client)
        provider.resume(request, "run-123")

    assert resume_calls and set(resume_calls) == {"GET"}


@pytest.mark.parametrize("bad_content", ["", "{not-json", '{"facts": {}}'])
def test_contract_6_deepseek_invalid_json_retries_then_succeeds(bad_content: str) -> None:
    """Empty, malformed, and schema-invalid JSON must each get a bounded retry."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return one retryable invalid result followed by a valid result."""
        nonlocal calls
        calls += 1
        content = bad_content if calls == 1 else _valid_deepseek_content()
        return httpx.Response(200, json=_deepseek_response(content))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        extractor = DeepSeekExtractor(
            api_key="test-key",
            client=client,
            model="deepseek-v4-flash",
            prices=DeepSeekPriceSchedule(0, 0, 0),
        )
        extractor.extract(_deepseek_company(), _deepseek_bundle())

    assert calls == 2


def test_contract_6_deepseek_retry_exhaustion_is_bounded() -> None:
    """Repeated malformed JSON must terminate deterministically without an infinite loop."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return malformed JSON while guarding against an unbounded retry loop."""
        nonlocal calls
        calls += 1
        if calls > 10:
            raise AssertionError("DeepSeek malformed-response retry is unbounded")
        return httpx.Response(200, json=_deepseek_response("{not-json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        extractor = DeepSeekExtractor(
            api_key="test-key",
            client=client,
            model="deepseek-v4-flash",
            prices=DeepSeekPriceSchedule(0, 0, 0),
        )
        with pytest.raises(DiscoveryProviderError):
            extractor.extract(_deepseek_company(), _deepseek_bundle())

    assert 1 < calls <= 10


def test_contract_7_unsupported_negative_cannot_hard_reject() -> None:
    """A cited false claim unsupported by its text must be treated as unknown."""
    result = _evaluate_pvf_false("We distribute industrial pipe, valves, and fittings.")
    assert result.final_decision == "uncertain"
    assert "confirmed_not_pvf_relevant" not in result.rejection_reasons


def test_contract_7_explicit_negative_evidence_can_reject() -> None:
    """Explicit exclusionary evidence may support a normal negative hard fact."""
    result = _evaluate_pvf_false(
        "We sell electrical supplies only and do not distribute pipe, valves, or fittings."
    )
    assert result.final_decision == "rejected"
    assert result.rejection_reasons == ["confirmed_not_pvf_relevant"]


def test_contract_7_unrelated_negation_cannot_support_hard_negative() -> None:
    """Negation elsewhere in a citation cannot validate the opposite PVF claim."""
    result = _evaluate_pvf_false(
        "We do not sell electrical equipment. We distribute industrial pipe, valves, and fittings."
    )
    assert result.final_decision == "uncertain"
    assert "confirmed_not_pvf_relevant" not in result.rejection_reasons


def test_contract_9_nested_raw_fields_have_deterministic_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized nested provider field cannot cross the persisted record ceiling."""
    monkeypatch.setenv("LEADS_MAX_RECORD_BYTES", "128")
    monkeypatch.setenv("LEADS_MAX_FILE_BYTES", "4096")
    monkeypatch.setenv("LEADS_MAX_RUN_BYTES", "4096")
    payload = {"raw_metadata": {"nested": {"blob": "x" * 1024}}}
    with pytest.raises(ValueError, match="record exceeds"):
        write_jsonl_atomic(tmp_path / "raw.jsonl", [payload])


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


def test_contract_11_per_run_storage_has_hard_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured aggregate run-byte ceiling blocks the next unsafe write."""
    monkeypatch.setenv("LEADS_MAX_FILE_BYTES", "128")
    monkeypatch.setenv("LEADS_MAX_RUN_BYTES", "40")
    write_text_atomic(tmp_path / "one.txt", "a" * 30)
    with pytest.raises(ValueError, match="run would exceed"):
        write_text_atomic(tmp_path / "two.txt", "b" * 20)
    assert not (tmp_path / "two.txt").exists()


def test_contract_12_data_root_symlink_is_rejected(tmp_path: Path) -> None:
    """The configured writable root itself cannot be a symlink."""
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        run_m2_batch(
            M2BatchConfig(run_id="symlink-test", data_root=linked_root),
            discovery={},
            researcher=None,  # type: ignore[arg-type]
            extractor=None,  # type: ignore[arg-type]
        )


def test_contract_12_child_output_symlink_is_rejected(tmp_path: Path) -> None:
    """A child artifact symlink cannot redirect generated files."""
    root = tmp_path / "data"
    run_dir = root / "child-test"
    run_dir.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("sentinel", encoding="utf-8")
    (run_dir / "companies_raw.jsonl").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        run_m2_batch(
            M2BatchConfig(
                run_id="child-test",
                data_root=root,
                deepseek_budget_usd=1.0,
                execute_live=True,
            ),
            discovery={},
            researcher=None,  # type: ignore[arg-type]
            extractor=None,  # type: ignore[arg-type]
        )
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_contract_12_parent_component_redirect_is_rejected(tmp_path: Path) -> None:
    """Replacing the validated run directory cannot redirect an actual artifact write."""
    root = tmp_path / "data"
    root.mkdir()
    run_dir = root / "parent-test"
    run_dir.mkdir()
    run_dir.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    run_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        run_m2_batch(
            M2BatchConfig(
                run_id="parent-test",
                data_root=root,
                deepseek_budget_usd=1.0,
                execute_live=True,
            ),
            discovery={},
            researcher=None,  # type: ignore[arg-type]
            extractor=None,  # type: ignore[arg-type]
        )

    assert list(outside.iterdir()) == []


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
    """Workflow and fixed wrapper together must enforce tiny non-input canary ceilings."""
    workflow = _paid_workflow().casefold()
    assert "production_canary" in workflow
    assert "leads_max_run_bytes" in workflow
    assert "inputs:" not in workflow


def test_contract_15_failure_logs_do_not_retain_secrets_or_raw_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Representative provider failures cannot expose secret/raw values."""
    secret = "secret-api-key-value"
    raw = "private-raw-provider-payload"
    response = httpx.Response(
        500,
        content=raw.encode(),
        request=httpx.Request(
            "GET",
            "https://provider.invalid/test",
            headers={"Authorization": f"Bearer {secret}"},
        ),
    )
    with pytest.raises(DiscoveryProviderError) as caught:
        request_json(
            response,
            provider="exa",
            request_id="request-1",
            operation="company_search",
            request_count=1,
        )

    logging.getLogger("live-readiness-contract").error("%s", caught.value)
    assert secret not in str(caught.value)
    assert raw not in str(caught.value)
    assert secret not in caplog.text
    assert raw not in caplog.text


def test_contract_15_sanitized_error_constructor_is_private_by_default() -> None:
    """Structured provider errors retain identifiers, not credentials or payloads."""
    error = provider_error(
        provider="exa",
        request_id="request-1",
        operation="company_search",
        request_count=1,
        kind="transient",
        retryable=True,
        metadata={"request_id": "request-1"},
    )
    assert "authorization" not in str(error).casefold()
    assert "payload" not in str(error).casefold()


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
