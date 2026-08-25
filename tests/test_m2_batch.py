"""Contract tests for M2 path safety, budgets, artifacts, and resume semantics."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import pytest

from leads_discovery.discovery.apify import ApifyDiscoveryProvider
from leads_discovery.discovery.queries import build_discovery_requests
from leads_discovery.models import (
    CompanyRecord,
    DiscoveryBatch,
    DiscoveryRecord,
    DiscoveryRequest,
    EvidenceBundle,
    EvidenceItem,
    ExtractedFact,
    ExtractionResult,
    UsageEvent,
)
from leads_discovery.pipeline.m2_batch import M2BatchConfig, run_m2_batch
from leads_discovery.research.evidence import ExaEvidenceResearcher
from leads_discovery.research.extract import DeepSeekPriceSchedule

FACT_KEYS = (
    "pvf_relevant",
    "pvf_product_breadth",
    "industrial_or_process_customer_focus",
    "branch_count",
    "inside_sales_or_estimating_presence",
    "rfq_or_quote_workflow_evidence",
    "project_or_tender_business",
    "bom_or_line_item_complexity",
    "manufacturer_count_or_breadth",
    "relevant_hiring",
    "employee_count",
    "revenue_if_reliably_available",
    "regional_independent_signal",
    "multi_location_signal",
    "known_current_direct_competitor_customer",
    "known_competitor_evaluation_history",
    "known_quote_automation_or_order_automation_relationship",
    "direct_quotation_pain_evidence",
    "manual_workflow_evidence",
    "explicit_process_bottleneck_evidence",
)
ARTIFACTS = {
    "companies_raw.jsonl",
    "companies_deduped.jsonl",
    "research_raw.jsonl",
    "companies_extracted.jsonl",
    "usage_events.jsonl",
    "usage.json",
    "checkpoint.json",
}


def _record(
    request: DiscoveryRequest,
    index: int,
    *,
    raw_metadata: dict[str, Any] | None = None,
) -> DiscoveryRecord:
    """Build one valid deterministic raw discovery row."""
    domain = f"company-{index}.com"
    return DiscoveryRecord(
        record_id=f"raw_{index:024x}",
        provider=request.provider,
        request_id=request.request_id,
        target_country_code=request.target_country_code,
        query=request.queries[0] if request.queries else None,
        provider_result_id=f"provider-{index}",
        name=f"Company {index}",
        source_url=f"https://{domain}/source",
        website_url=f"https://{domain}",
        city="Houston",
        region="TX",
        postal_code="77001",
        country_code="US",
        title=f"Company {index}",
        snippet="PVF distributor",
        raw_metadata=(
            {"index": index, "nested": {"keep": [1, 2]}}
            if raw_metadata is None
            else raw_metadata
        ),
        retrieved_at="2026-08-23T10:00:00+00:00",
    )


class FakeDiscovery:
    """Deterministic discovery fake with optional persistence assertions."""

    def __init__(self, run_dir: Path | None = None) -> None:
        self.calls = 0
        self.run_dir = run_dir

    def search(self, request: DiscoveryRequest) -> DiscoveryBatch:
        """Return one unique company and zero-cost usage."""
        if self.run_dir is not None:
            checkpoint = (self.run_dir / "checkpoint.json").read_text(encoding="utf-8")
            assert "in_flight" in checkpoint
        self.calls += 1
        return DiscoveryBatch(
            request=request,
            records=[_record(request, self.calls)],
            usage_events=[
                UsageEvent(
                    provider=request.provider,
                    operation="company_search",
                    request_count=1,
                    estimated_cost_usd=0.0,
                )
            ],
        )


class FakeResearcher:
    """Minimal structural researcher fake with observable persistence ordering."""

    def __init__(self, run_dir: Path | None = None) -> None:
        self.calls = 0
        self.run_dir = run_dir

    def _bundle(self, company: CompanyRecord) -> EvidenceBundle:
        self.calls += 1
        evidence_id = f"ev_{self.calls:024x}"
        return EvidenceBundle(
            company_id=company.company_id,
            items=[
                EvidenceItem(
                    evidence_id=evidence_id,
                    url=f"https://{company.domain}/evidence",
                    title="Evidence",
                    excerpt="Industrial pipe valves and fittings.",
                    source_type="web",
                    provider="exa",
                    retrieved_at="2026-08-23T11:00:00+00:00",
                )
            ],
            raw_records=[{"id": f"research-{self.calls}", "full": {"preserve": True}}],
            usage_events=[
                UsageEvent(
                    provider="exa",
                    operation="company_research",
                    request_count=3,
                    estimated_cost_usd=0.0,
                )
            ],
        )

    def research(
        self,
        company: CompanyRecord,
        *,
        on_progress: Callable[[EvidenceBundle], None] | None = None,
    ) -> EvidenceBundle:
        """Return one retained evidence delta."""
        if self.run_dir is not None:
            assert (self.run_dir / "companies_raw.jsonl").exists()
            assert (self.run_dir / "companies_deduped.jsonl").exists()
            usage = (self.run_dir / "usage_events.jsonl").read_text(encoding="utf-8")
            assert "company_search" in usage
            checkpoint = (self.run_dir / "checkpoint.json").read_text(encoding="utf-8")
            assert "in_flight" in checkpoint
        bundle = self._bundle(company)
        if on_progress is not None:
            on_progress(bundle)
        return bundle

    def resume(
        self,
        company: CompanyRecord,
        *,
        start_index: int,
        on_progress: Callable[[EvidenceBundle], None],
    ) -> EvidenceBundle:
        """Continue the fake from any valid persisted cursor without concrete-class coupling."""
        assert start_index > 0
        return self.research(company, on_progress=on_progress)


class FakeExtractor:
    """Minimal structural extractor fake with coherent reservation and usage."""

    def __init__(
        self,
        *,
        run_dir: Path | None = None,
        estimated_cost_usd: float = 0.0,
        prices: DeepSeekPriceSchedule | None = None,
        crash: bool = False,
    ) -> None:
        self.calls = 0
        self.run_dir = run_dir
        self.estimated_cost_usd = estimated_cost_usd
        self.prices = prices or DeepSeekPriceSchedule(0.0, 0.0, 0.0)
        self.model = "deepseek-v4-flash"
        self.crash = crash

    def reservation_cost_usd(self, company: CompanyRecord, bundle: EvidenceBundle) -> float:
        """Mirror the production cache-miss/input plus bounded-output reservation shape."""
        prompt_characters = 2_000 + len(company.name) + sum(
            len(item.url) + len(item.title or "") + len(item.excerpt or "")
            for item in bundle.items
        )
        return (
            prompt_characters * self.prices.cache_miss_input_per_million
            + 2_048 * self.prices.output_per_million
        ) / 1_000_000

    def extract(self, company: CompanyRecord, bundle: EvidenceBundle) -> ExtractionResult:
        """Return a complete fact map or simulate process death after durable intent."""
        if self.run_dir is not None:
            assert (self.run_dir / "research_raw.jsonl").exists()
            usage = (self.run_dir / "usage_events.jsonl").read_text(encoding="utf-8")
            assert "company_research" in usage
            checkpoint = (self.run_dir / "checkpoint.json").read_text(encoding="utf-8")
            assert "in_flight" in checkpoint
        self.calls += 1
        if self.crash:
            raise KeyboardInterrupt
        evidence_id = bundle.items[0].evidence_id
        facts = {key: ExtractedFact(None, 0, []) for key in FACT_KEYS}
        facts["pvf_relevant"] = ExtractedFact(True, 0.9, [evidence_id])
        return ExtractionResult(
            company_id=company.company_id,
            model=self.model,
            facts=facts,
            usage_event=UsageEvent(
                provider="deepseek",
                operation="structured_extraction",
                request_count=1,
                input_tokens=50,
                output_tokens=20,
                estimated_cost_usd=self.estimated_cost_usd,
            ),
        )


def _config(tmp_path: Path, run_id: str, **overrides: Any) -> M2BatchConfig:
    """Build a minimal explicit-live M2 configuration with bounded paid calls."""
    values: dict[str, Any] = {
        "run_id": run_id,
        "data_root": tmp_path,
        "max_candidates": 1,
        "max_extracted": 1,
        "deepseek_budget_usd": 1.0,
        "exa_budget_usd": 1.0,
        "exa_request_reservation_usd": 0.10,
        "execute_live": True,
    }
    values.update(overrides)
    return M2BatchConfig(**values)


def test_persisted_models_defensively_copy_nested_values() -> None:
    """Persistence-facing records detach nested caller-owned values."""
    nested: dict[str, Any] = {"nested": {"values": [1, 2]}}
    request = DiscoveryRequest("exa:us:test:v1", "exa", "test", "US", ("query",), 1, 1)
    record = _record(request, 1, raw_metadata=nested)
    payload = record.to_dict()
    assert DiscoveryRecord.from_dict(payload).to_dict() == payload
    json.dumps(payload)
    nested["nested"]["values"].append(3)
    assert record.raw_metadata == {"nested": {"values": [1, 2]}}


def test_company_record_and_m2_config_defaults_remain_compatible(tmp_path: Path) -> None:
    """M2 preserves M1 scoring defaults and keeps provider budgets independent."""
    company = CompanyRecord(company_id="cmp_defaults", name="Defaults Co")
    assert company.status == "active"
    assert company.coverage == {}
    assert company.score_components == {}
    assert company.final_score is None
    assert company.final_decision is None
    assert company.rejection_reasons == []

    config = M2BatchConfig(run_id="run-1", data_root=tmp_path)
    assert config.max_candidates == 100
    assert config.max_extracted == 20
    assert config.include_apify is False
    assert config.apify_budget_usd == 0.25
    assert config.deepseek_budget_usd is None
    assert config.exa_budget_usd is None
    assert config.exa_request_reservation_usd is None
    assert config.execute_live is False


@pytest.mark.parametrize("run_id", ["", ".", "..", "../escape", "a/b", "/abs", "_bad", "a" * 65])
def test_unsafe_run_ids_fail_before_provider_work(tmp_path: Path, run_id: str) -> None:
    """Only the safe run-ID grammar may resolve beneath the data root."""
    discovery = FakeDiscovery()
    with pytest.raises((TypeError, ValueError)):
        run_m2_batch(
            _config(tmp_path, run_id),
            discovery={"exa": discovery},
            researcher=FakeResearcher(),
            extractor=FakeExtractor(),
        )
    assert discovery.calls == 0
    assert not (tmp_path.parent / "escape").exists()


def test_fractional_extraction_cap_fails_before_provider_work(tmp_path: Path) -> None:
    """The extraction cap is a strict integer in 1..20 before paid intent."""
    discovery = FakeDiscovery()
    with pytest.raises((TypeError, ValueError)):
        run_m2_batch(
            _config(tmp_path, "fractional-cap", max_extracted=1.5),
            discovery={"exa": discovery},
            researcher=FakeResearcher(),
            extractor=FakeExtractor(),
        )
    assert discovery.calls == 0


def test_preexisting_artifact_symlink_cannot_redirect_writes_outside_data_root(
    tmp_path: Path,
) -> None:
    """Every M2 artifact write remains beneath the configured data root."""
    run_dir = tmp_path / "symlink-escape"
    run_dir.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.jsonl"
    outside.write_text("sentinel\n", encoding="utf-8")
    (run_dir / "companies_raw.jsonl").symlink_to(outside)

    with suppress(OSError, RuntimeError, ValueError):
        run_m2_batch(
            _config(tmp_path, "symlink-escape"),
            discovery={"exa": FakeDiscovery()},
            researcher=FakeResearcher(),
            extractor=FakeExtractor(),
        )
    assert outside.read_text(encoding="utf-8") == "sentinel\n"


def test_explicit_live_flag_is_required_before_provider_calls(tmp_path: Path) -> None:
    """Without live authorization the runner returns dry state and makes no calls."""
    discovery = FakeDiscovery()
    checkpoint = run_m2_batch(
        _config(tmp_path, "dry", execute_live=False),
        discovery={"exa": discovery},
        researcher=FakeResearcher(),
        extractor=FakeExtractor(),
    )
    assert checkpoint.status == "dry_run"
    assert checkpoint.pause_reason == "live_execution_not_authorized"
    assert discovery.calls == 0


def test_fake_provider_end_to_end_writes_seven_artifacts_in_durable_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected boundaries preserve intent, usage, raw artifacts, and fsync ordering."""
    import os

    fsync_calls = 0
    real_fsync = os.fsync

    def count_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", count_fsync)
    run_dir = tmp_path / "e2e"
    checkpoint = run_m2_batch(
        _config(tmp_path, "e2e"),
        discovery={"exa": FakeDiscovery(run_dir)},
        researcher=FakeResearcher(run_dir),
        extractor=FakeExtractor(run_dir=run_dir),
    )

    assert checkpoint.status == "completed"
    assert {path.name for path in run_dir.iterdir()} == ARTIFACTS
    assert fsync_calls > 0
    usage = [
        json.loads(line)
        for line in (run_dir / "usage_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["operation"] for event in usage] == [
        "company_search",
        "company_research",
        "structured_extraction",
    ]
    summary = json.loads((run_dir / "usage.json").read_text(encoding="utf-8"))
    assert set(summary["providers"]) == {"deepseek", "exa"}
    for name in (
        "companies_raw.jsonl",
        "companies_deduped.jsonl",
        "research_raw.jsonl",
        "companies_extracted.jsonl",
    ):
        assert (run_dir / name).read_text(encoding="utf-8").strip()


def test_successful_research_response_is_durable_before_next_paid_search(tmp_path: Path) -> None:
    """A paid Exa research response is durable before the following paid search begins."""
    calls = 0
    first_row = {
        "id": "research-1",
        "url": "https://research-one.com/about",
        "title": "Company 1 research",
        "highlights": ["Industrial valves and fittings"],
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={"results": [first_row], "costDollars": {"total": 0.01}},
            )
        raise KeyboardInterrupt

    run_dir = tmp_path / "research-crash"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        researcher = ExaEvidenceResearcher(api_key="test-key", client=client)
        with pytest.raises(KeyboardInterrupt):
            run_m2_batch(
                _config(tmp_path, "research-crash"),
                discovery={"exa": FakeDiscovery()},
                researcher=researcher,
                extractor=FakeExtractor(),
            )

    assert calls == 2
    raw_rows = [
        json.loads(line)
        for line in (run_dir / "research_raw.jsonl").read_text().splitlines()
    ]
    assert raw_rows == [first_row]
    usage_rows = [
        json.loads(line)
        for line in (run_dir / "usage_events.jsonl").read_text().splitlines()
    ]
    research_usage = [row for row in usage_rows if row["operation"] == "company_research"]
    assert len(research_usage) == 1
    assert research_usage[0]["request_count"] == 1
    assert research_usage[0]["estimated_cost_usd"] == pytest.approx(0.01)


def test_three_call_research_progress_is_counted_once_without_aggregate_double_count(
    tmp_path: Path,
) -> None:
    """Three successful Exa responses persist three deltas without aggregate double-counting."""
    costs = [0.01, 0.02, 0.03]
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        index = calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": f"research-{index}",
                        "url": f"https://research-{index}.com/page",
                        "title": f"Research {index}",
                        "highlights": [f"Evidence {index}"],
                    }
                ],
                "costDollars": {"total": costs[index]},
            },
        )

    run_dir = tmp_path / "research-complete"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        checkpoint = run_m2_batch(
            _config(tmp_path, "research-complete"),
            discovery={"exa": FakeDiscovery()},
            researcher=ExaEvidenceResearcher(api_key="test-key", client=client),
            extractor=FakeExtractor(),
        )

    assert checkpoint.status == "completed"
    assert calls == 3
    raw_rows = [
        json.loads(line)
        for line in (run_dir / "research_raw.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["id"] for row in raw_rows] == ["research-0", "research-1", "research-2"]
    usage_rows = [
        json.loads(line)
        for line in (run_dir / "usage_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    research_usage = [row for row in usage_rows if row["operation"] == "company_research"]
    assert len(research_usage) == 3
    assert [row["request_count"] for row in research_usage] == [1, 1, 1]
    assert [row["estimated_cost_usd"] for row in research_usage] == pytest.approx(costs)
    assert sum(row["request_count"] for row in research_usage) == 3
    assert sum(row["estimated_cost_usd"] for row in research_usage) == pytest.approx(sum(costs))


def test_conservative_deepseek_reservation_persists_paused_budget_without_call(
    tmp_path: Path,
) -> None:
    """Worst-case pre-call reservation wins over the extraction target."""
    extractor = FakeExtractor(
        prices=DeepSeekPriceSchedule(1_000_000.0, 1_000_000.0, 1_000_000.0)
    )
    checkpoint = run_m2_batch(
        _config(tmp_path, "budget", deepseek_budget_usd=0.01),
        discovery={"exa": FakeDiscovery()},
        researcher=FakeResearcher(),
        extractor=extractor,
    )
    assert checkpoint.status == "paused_budget"
    assert extractor.calls == 0
    persisted = json.loads((tmp_path / "budget" / "checkpoint.json").read_text())
    assert persisted["status"] == "paused_budget"


def test_usage_replay_prevents_budget_reset_and_second_model_call(tmp_path: Path) -> None:
    """Persisted usage plus the next reservation blocks operation two after restart."""
    extractor = FakeExtractor(
        estimated_cost_usd=0.6,
        prices=DeepSeekPriceSchedule(100.0, 100.0, 100.0),
    )
    config = _config(
        tmp_path,
        "replay",
        max_candidates=2,
        max_extracted=2,
        deepseek_budget_usd=0.7,
    )
    first = run_m2_batch(
        config,
        discovery={"exa": FakeDiscovery()},
        researcher=FakeResearcher(),
        extractor=extractor,
    )
    first_calls = extractor.calls
    second = run_m2_batch(
        config,
        discovery={"exa": FakeDiscovery()},
        researcher=FakeResearcher(),
        extractor=extractor,
    )
    assert first.status == "paused_budget"
    assert second.status == "paused_budget"
    assert first_calls == 1
    assert extractor.calls == first_calls


def test_optional_apify_budget_exhaustion_does_not_stop_required_exa(tmp_path: Path) -> None:
    """Optional Apify may exhaust independently while required Exa continues."""
    apify_calls = 0

    def apify_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal apify_calls
        apify_calls += 1
        return httpx.Response(402, text="credit exhausted")

    with httpx.Client(transport=httpx.MockTransport(apify_handler)) as client:
        checkpoint = run_m2_batch(
            _config(
                tmp_path,
                "optional-apify",
                max_candidates=4,
                include_apify=True,
                apify_budget_usd=0.25,
            ),
            discovery={
                "exa": FakeDiscovery(),
                "apify": ApifyDiscoveryProvider(api_token="token", client=client),
            },
            researcher=FakeResearcher(),
            extractor=FakeExtractor(),
        )

    assert apify_calls == 1
    assert checkpoint.status == "completed"
    assert (tmp_path / "optional-apify" / "companies_extracted.jsonl").read_text().strip()


def test_persisted_apify_run_id_is_resumed_without_replacement_search(tmp_path: Path) -> None:
    """Durable Apify state resumes the exact existing run."""
    config = _config(
        tmp_path,
        "resume-apify",
        max_candidates=4,
        include_apify=True,
        apify_budget_usd=0.25,
    )
    requests = build_discovery_requests(
        include_apify=True,
        max_candidates=4,
        apify_budget_usd=0.25,
    )
    operations: dict[str, dict[str, Any]] = {}
    for request in requests:
        operation_id = f"discovery:{request.request_id}"
        operations[operation_id] = {
            "provider": request.provider,
            "operation": "company_search" if request.provider == "exa" else "google_maps_search",
            "request_id": request.request_id,
            "state": "completed",
        }
    apify_request = next(request for request in requests if request.provider == "apify")
    operations[f"discovery:{apify_request.request_id}"]["state"] = "in_flight"
    operations[f"discovery:{apify_request.request_id}"]["run_id"] = "persisted-run-123"

    run_dir = tmp_path / "resume-apify"
    run_dir.mkdir()
    (run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "run_id": "resume-apify",
                "status": "running",
                "provider_state": {"operations": operations, "stages": {}},
            }
        ),
        encoding="utf-8",
    )

    class ResumeOnlyApify:
        """Permit only resuming the persisted remote run."""

        def __init__(self) -> None:
            self.resume_calls: list[tuple[str, str]] = []

        def search(self, _request: DiscoveryRequest) -> DiscoveryBatch:
            raise AssertionError("persisted Apify run must not start a replacement search")

        def resume(self, request: DiscoveryRequest, run_id: str) -> DiscoveryBatch:
            self.resume_calls.append((request.request_id, run_id))
            return DiscoveryBatch(
                request=request,
                records=[_record(request, 1)],
                usage_events=[
                    UsageEvent(
                        provider="apify",
                        operation="google_maps_search",
                        request_count=1,
                        estimated_cost_usd=0.0,
                        metadata={"run_id": run_id},
                    )
                ],
            )

    apify = ResumeOnlyApify()
    checkpoint = run_m2_batch(
        config,
        discovery={"apify": apify},
        researcher=FakeResearcher(),
        extractor=FakeExtractor(),
    )
    assert checkpoint.status == "completed"
    assert apify.resume_calls == [(apify_request.request_id, "persisted-run-123")]


def test_unknown_in_flight_exa_is_not_automatically_repeated(tmp_path: Path) -> None:
    """Process death during required Exa leaves an unreplayable unknown operation."""
    calls = 0

    class CrashDiscovery:
        def search(self, _request: DiscoveryRequest) -> DiscoveryBatch:
            nonlocal calls
            calls += 1
            raise KeyboardInterrupt

    config = _config(tmp_path, "unknown-exa")
    with pytest.raises(KeyboardInterrupt):
        run_m2_batch(
            config,
            discovery={"exa": CrashDiscovery()},
            researcher=FakeResearcher(),
            extractor=FakeExtractor(),
        )
    assert calls == 1

    class BombDiscovery:
        def search(self, _request: DiscoveryRequest) -> DiscoveryBatch:
            raise AssertionError("unknown Exa operation must not repeat")

    resumed = run_m2_batch(
        config,
        discovery={"exa": BombDiscovery()},
        researcher=FakeResearcher(),
        extractor=FakeExtractor(),
    )
    assert resumed.status == "paused_unknown"
    assert calls == 1


def test_unknown_in_flight_deepseek_is_not_automatically_repeated(tmp_path: Path) -> None:
    """Process death during DeepSeek leaves an unreplayable unknown operation."""
    crashing = FakeExtractor(crash=True)
    config = _config(tmp_path, "unknown-deepseek")
    with pytest.raises(KeyboardInterrupt):
        run_m2_batch(
            config,
            discovery={"exa": FakeDiscovery()},
            researcher=FakeResearcher(),
            extractor=crashing,
        )
    assert crashing.calls == 1

    class BombExtractor(FakeExtractor):
        def extract(self, company: CompanyRecord, bundle: EvidenceBundle) -> ExtractionResult:
            raise AssertionError("unknown DeepSeek operation must not repeat")

    resumed = run_m2_batch(
        config,
        discovery={"exa": FakeDiscovery()},
        researcher=FakeResearcher(),
        extractor=BombExtractor(),
    )
    assert resumed.status == "paused_unknown"
    assert crashing.calls == 1


def test_exa_budget_is_rechecked_between_paid_research_queries(tmp_path: Path) -> None:
    """Exa stops before the next research call when reservation would cross its ceiling."""
    calls = 0
    first_row = {
        "id": "budget-research-1",
        "url": "https://budget-research.com/about",
        "title": "Budget research",
        "highlights": ["Industrial valves and fittings"],
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={"results": [first_row], "costDollars": {"total": 0.01}},
            )
        raise AssertionError("Exa budget must be rechecked before the next paid research call")

    run_dir = tmp_path / "exa-research-budget"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        checkpoint = run_m2_batch(
            _config(
                tmp_path,
                "exa-research-budget",
                exa_budget_usd=0.01,
                exa_request_reservation_usd=0.01,
            ),
            discovery={"exa": FakeDiscovery()},
            researcher=ExaEvidenceResearcher(api_key="test-key", client=client),
            extractor=FakeExtractor(),
        )

    assert calls == 1
    assert checkpoint.status == "paused_budget"
    usage_rows = [
        json.loads(line)
        for line in (run_dir / "usage_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    research_usage = [row for row in usage_rows if row["operation"] == "company_research"]
    assert len(research_usage) == 1
    assert research_usage[0]["estimated_cost_usd"] == pytest.approx(0.01)
