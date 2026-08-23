"""Contract tests for M2 models, path-safe orchestration, budgets, and resume semantics."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from leads_discovery.discovery.apify import ApifyDiscoveryProvider
from leads_discovery.models import (
    CompanyRecord,
    DeduplicationResult,
    DiscoveryBatch,
    DiscoveryRecord,
    DiscoveryRequest,
    EvidenceBundle,
    EvidenceItem,
    ExtractedFact,
    ExtractionResult,
    ResearchRequest,
    UsageEvent,
)
from leads_discovery.pipeline.m2_batch import M2BatchConfig, run_m2_batch
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


def _record(request: DiscoveryRequest, index: int) -> DiscoveryRecord:
    """Build one valid deterministic raw discovery row for a supplied request."""
    domain = f"company-{index}.example.com"
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
        raw_metadata={"index": index, "nested": {"keep": [1, 2]}},
        retrieved_at="2026-08-23T10:00:00+00:00",
    )


class FakeDiscovery:
    """Deterministic discovery fake that can assert pre-call persistence."""

    def __init__(self, run_dir: Path | None = None) -> None:
        self.calls = 0
        self.run_dir = run_dir

    def search(self, request: DiscoveryRequest) -> DiscoveryBatch:
        """Return one unique company and one zero-cost authenticated usage event."""
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
    """Deterministic research fake with observable artifact-order checks."""

    def __init__(self, run_dir: Path | None = None) -> None:
        self.calls = 0
        self.run_dir = run_dir

    def research(self, company: CompanyRecord) -> EvidenceBundle:
        """Return one retained evidence item and one full raw research row."""
        if self.run_dir is not None:
            assert (self.run_dir / "companies_raw.jsonl").exists()
            assert (self.run_dir / "companies_deduped.jsonl").exists()
            usage = (self.run_dir / "usage_events.jsonl").read_text(encoding="utf-8")
            assert "company_search" in usage
            checkpoint = (self.run_dir / "checkpoint.json").read_text(encoding="utf-8")
            assert "in_flight" in checkpoint
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


class FakeExtractor:
    """Deterministic extractor fake exposing the public model and price configuration."""

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

    def extract(self, company: CompanyRecord, bundle: EvidenceBundle) -> ExtractionResult:
        """Return the complete fact map or simulate process death after intent persistence."""
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
    """Build a minimal explicit-live M2 configuration with optional overrides."""
    values: dict[str, Any] = {
        "run_id": run_id,
        "data_root": tmp_path,
        "max_candidates": 1,
        "max_extracted": 1,
        "deepseek_budget_usd": 1.0,
        "execute_live": True,
    }
    values.update(overrides)
    return M2BatchConfig(**values)


def test_new_models_round_trip_nested_json_and_defensively_copy_callers() -> None:
    """Every M2 model round-trips JSON-safe nested data without aliasing caller mutables."""
    nested = {"nested": {"values": [1, 2]}}
    request = DiscoveryRequest("exa:us:test:v1", "exa", "test", "US", ("query",), 1, 1)
    record = _record(request, 1)
    record.raw_metadata = nested
    usage = UsageEvent(provider="exa", operation="company_search", metadata=deepcopy(nested))
    company = CompanyRecord(company_id="cmp_1", name="Acme", discovery_records=[record.to_dict()])
    evidence = EvidenceItem(evidence_id="ev_1", url="https://acme.com/e", provider="exa")
    models = [
        record,
        DiscoveryBatch(request, [record], [usage]),
        DeduplicationResult([company], [record]),
        evidence,
        ResearchRequest("research-1", "cmp_1", "company-profile", "query", 5),
        EvidenceBundle("cmp_1", [evidence], [deepcopy(nested)], [usage]),
        ExtractedFact(["valves"], 0.8, ["ev_1"]),
        ExtractionResult(
            "cmp_1",
            "deepseek-v4-flash",
            {"pvf_product_breadth": ExtractedFact(["valves"], 0.8, ["ev_1"])},
            UsageEvent(provider="deepseek", operation="structured_extraction"),
        ),
    ]
    for model in models:
        payload = model.to_dict()
        assert type(model).from_dict(payload).to_dict() == payload
        json.dumps(payload)

    nested["nested"]["values"].append(3)
    assert record.raw_metadata == {"nested": {"values": [1, 2]}}


def test_company_record_and_m2_config_defaults_remain_compatible(tmp_path: Path) -> None:
    """M2 preserves M1 empty scoring defaults and keeps provider budgets independent."""
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
    assert config.execute_live is False


@pytest.mark.parametrize("run_id", ["", ".", "..", "../escape", "a/b", "/abs", "_bad", "a" * 65])
def test_unsafe_run_ids_fail_before_provider_work(tmp_path: Path, run_id: str) -> None:
    """Only the frozen safe run-ID grammar may resolve beneath the configured data root."""
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
    """INV-13 requires an integer 1..20 extraction cap before any paid provider intent."""
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
    """Run isolation requires every M2 artifact write to remain beneath the configured data root."""
    run_dir = tmp_path / "symlink-escape"
    run_dir.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.jsonl"
    outside.write_text("sentinel\n", encoding="utf-8")
    link = run_dir / "companies_raw.jsonl"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("filesystem does not support symlinks")

    try:
        run_m2_batch(
            _config(tmp_path, "symlink-escape"),
            discovery={"exa": FakeDiscovery()},
            researcher=FakeResearcher(),
            extractor=FakeExtractor(),
        )
    except (OSError, RuntimeError, ValueError):
        pass

    assert outside.read_text(encoding="utf-8") == "sentinel\n"


def test_explicit_live_flag_is_required_before_provider_calls(tmp_path: Path) -> None:
    """Without explicit live authorization the runner returns dry-run state and makes no calls."""
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
    """Injected fakes complete discovery-to-extraction with intent/usage/raw ordering and fsync."""
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
    assert (run_dir / "companies_raw.jsonl").read_text(encoding="utf-8").strip()
    assert (run_dir / "companies_deduped.jsonl").read_text(encoding="utf-8").strip()
    assert (run_dir / "research_raw.jsonl").read_text(encoding="utf-8").strip()
    assert (run_dir / "companies_extracted.jsonl").read_text(encoding="utf-8").strip()


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
    """Persisted authenticated usage is replayed so restart cannot reset DeepSeek spend."""
    extractor = FakeExtractor(
        estimated_cost_usd=0.6,
        prices=DeepSeekPriceSchedule(10.0, 10.0, 10.0),
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
    """Optional Apify may exhaust independently while Exa research/extraction continues."""
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


def test_unknown_in_flight_exa_is_not_automatically_repeated(tmp_path: Path) -> None:
    """Process death during required Exa yields paused_unknown and no automatic repeat."""
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
    """Process death during DeepSeek yields paused_unknown and no second model call."""
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
