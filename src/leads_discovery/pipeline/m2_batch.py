"""Path-safe resumable M2 discovery, research, and extraction orchestration."""

from __future__ import annotations

import argparse
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import httpx

from leads_discovery.dedup import deduplicate
from leads_discovery.discovery import (
    ApifyDiscoveryProvider,
    DiscoveryProvider,
    DiscoveryProviderError,
    ExaDiscoveryProvider,
    build_discovery_requests,
)
from leads_discovery.models import (
    CompanyRecord,
    DiscoveryBatch,
    DiscoveryRecord,
    DiscoveryRequest,
    EvidenceBundle,
    ExtractionResult,
    RunCheckpoint,
    UsageEvent,
)
from leads_discovery.pipeline.costs import CostTracker
from leads_discovery.pipeline.state import (
    append_company_snapshot,
    append_jsonl,
    append_usage_event,
    load_checkpoint,
    load_jsonl,
    load_latest_company_records,
    load_usage_events,
    write_checkpoint,
)
from leads_discovery.research import (
    DeepSeekExtractor,
    DeepSeekPriceSchedule,
    ExaEvidenceResearcher,
    apply_extraction,
    build_evidence_bundle,
    build_research_requests,
    select_research_companies,
)

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_OPERATION_STATES = {"in_flight", "completed", "failed", "pending"}
_DEEPSEEK_MODEL = "deepseek-v4-flash"
_DEFAULT_PRICES = DeepSeekPriceSchedule(
    cache_hit_input_per_million=0.0028,
    cache_miss_input_per_million=0.14,
    output_per_million=0.28,
)


class MissingProviderCredentials(RuntimeError):
    """Signal that explicitly live execution lacks required provider credentials."""


class ResumableDiscoveryProvider(DiscoveryProvider, Protocol):
    """Discovery provider that can continue a previously started remote operation."""

    def resume(self, request: DiscoveryRequest, run_id: str) -> DiscoveryBatch:
        """Continue the exact persisted remote run."""


class EvidenceResearcher(Protocol):
    """Research boundary required by the M2 orchestrator."""

    def research(
        self,
        company: CompanyRecord,
        *,
        on_progress: Callable[[EvidenceBundle], None] | None = None,
    ) -> EvidenceBundle:
        """Research a company from the first bounded query."""

    def resume(
        self,
        company: CompanyRecord,
        *,
        start_index: int,
        on_progress: Callable[[EvidenceBundle], None],
    ) -> EvidenceBundle:
        """Continue a company from a durable successful-query cursor."""


class Extractor(Protocol):
    """Structured-extraction boundary required by the M2 orchestrator."""

    def reservation_cost_usd(self, company: CompanyRecord, bundle: EvidenceBundle) -> float:
        """Return a conservative pre-dispatch reservation."""

    def extract(self, company: CompanyRecord, bundle: EvidenceBundle) -> ExtractionResult:
        """Extract structured facts from bounded evidence."""


class _ResearchBudgetPause(Exception):
    """Stop between Exa calls when the next reserved request is no longer affordable."""


@dataclass(frozen=True, slots=True)
class M2BatchConfig:
    """Configure one bounded M2 run."""

    run_id: str
    data_root: Path
    max_candidates: int = 100
    max_extracted: int = 20
    include_apify: bool = False
    apify_budget_usd: float = 0.25
    deepseek_budget_usd: float | None = None
    exa_budget_usd: float | None = None
    exa_request_reservation_usd: float | None = None
    execute_live: bool = False


@dataclass(frozen=True, slots=True)
class M2RunPaths:
    """Resolve the four authoritative M2 artifacts for one run."""

    run_dir: Path
    companies_raw: Path
    companies_extracted: Path
    usage_events: Path
    checkpoint: Path

    def artifacts(self) -> tuple[Path, ...]:
        """Return every writable M2 artifact."""
        return (
            self.companies_raw,
            self.companies_extracted,
            self.usage_events,
            self.checkpoint,
        )


@dataclass(frozen=True, slots=True)
class LiveM2Result:
    """Return one environment-composed M2 result and effective optional-provider state."""

    checkpoint: RunCheckpoint
    apify_enabled: bool


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _finite_nonnegative(name: str, value: float | None) -> None:
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a finite nonnegative number or null")


def resolve_m2_paths(config: M2BatchConfig) -> M2RunPaths:
    """Validate run controls and resolve contained artifact paths before mutation."""
    if not isinstance(config.run_id, str) or not _RUN_ID.fullmatch(config.run_id):
        raise ValueError("run_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    if (
        isinstance(config.max_candidates, bool)
        or not isinstance(config.max_candidates, int)
        or not 1 <= config.max_candidates <= 100
    ):
        raise ValueError("max_candidates must be an integer in 1..100")
    if (
        isinstance(config.max_extracted, bool)
        or not isinstance(config.max_extracted, int)
        or not 1 <= config.max_extracted <= 20
    ):
        raise ValueError("max_extracted must be an integer in 1..20")
    if (
        isinstance(config.apify_budget_usd, bool)
        or not isinstance(config.apify_budget_usd, (int, float))
        or not math.isfinite(config.apify_budget_usd)
        or not 0 <= config.apify_budget_usd <= 1
    ):
        raise ValueError("apify_budget_usd must be in 0..1")
    _finite_nonnegative("deepseek_budget_usd", config.deepseek_budget_usd)
    _finite_nonnegative("exa_budget_usd", config.exa_budget_usd)
    _finite_nonnegative("exa_request_reservation_usd", config.exa_request_reservation_usd)
    if (
        config.exa_request_reservation_usd is not None
        and config.exa_request_reservation_usd <= 0
    ):
        raise ValueError("exa_request_reservation_usd must be positive when provided")
    if config.execute_live and (
        config.deepseek_budget_usd is None or config.deepseek_budget_usd <= 0
    ):
        raise ValueError("live extraction requires a positive explicit DeepSeek budget")

    root = config.data_root.expanduser().resolve()
    candidate = root / config.run_id
    if candidate.is_symlink():
        raise ValueError("run directory must not be a symlink")
    run_dir = candidate.resolve()
    if run_dir.parent != root:
        raise ValueError("run directory must remain directly beneath data_root")
    return M2RunPaths(
        run_dir=run_dir,
        companies_raw=run_dir / "companies_raw.jsonl",
        companies_extracted=run_dir / "companies_extracted.jsonl",
        usage_events=run_dir / "usage_events.jsonl",
        checkpoint=run_dir / "checkpoint.json",
    )


def _validate_artifact_paths(paths: M2RunPaths) -> None:
    for path in paths.artifacts():
        if path.is_symlink():
            raise ValueError(f"artifact path must not be a symlink: {path.name}")


def _initial_checkpoint(run_id: str) -> RunCheckpoint:
    return RunCheckpoint(
        run_id=run_id,
        status="running",
        provider_state={"operations": {}, "stages": {}},
    )


def _operations(checkpoint: RunCheckpoint) -> dict[str, dict[str, Any]]:
    raw = checkpoint.provider_state.setdefault("operations", {})
    if not isinstance(raw, dict):
        raise ValueError("checkpoint operations must be an object")
    result: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise ValueError("checkpoint operation entries must be objects")
        if value.get("state") not in _OPERATION_STATES:
            raise ValueError("checkpoint operation has an invalid state")
        result[key] = cast(dict[str, Any], value)
    checkpoint.provider_state["operations"] = result
    return result


def _stages(checkpoint: RunCheckpoint) -> dict[str, str]:
    raw = checkpoint.provider_state.setdefault("stages", {})
    if not isinstance(raw, dict):
        raise ValueError("checkpoint stages must be a string map")
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("checkpoint stages must be a string map")
        result[key] = value
    checkpoint.provider_state["stages"] = result
    return result


def _entry_str(entry: dict[str, Any], key: str) -> str | None:
    value = entry.get(key)
    return value if isinstance(value, str) else None


def _successful_calls(entry: dict[str, Any] | None, maximum: int) -> int:
    if entry is None:
        return 0
    value = entry.get("successful_calls", 0)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        raise ValueError("research successful_calls checkpoint value is invalid")
    return value


def _persist_checkpoint(path: Path, checkpoint: RunCheckpoint) -> None:
    checkpoint.updated_at = _now()
    write_checkpoint(path, checkpoint)


def _mark_in_flight(
    checkpoint: RunCheckpoint,
    path: Path,
    *,
    operation_id: str,
    provider: str,
    operation: str,
    request_id: str | None = None,
    company_id: str | None = None,
    reservation_usd: float | None = None,
    successful_calls: int | None = None,
) -> None:
    entry: dict[str, Any] = {
        "provider": provider,
        "operation": operation,
        "state": "in_flight",
    }
    if request_id is not None:
        entry["request_id"] = request_id
    if company_id is not None:
        entry["company_id"] = company_id
    if reservation_usd is not None:
        entry["reservation_usd"] = reservation_usd
    if successful_calls is not None:
        entry["successful_calls"] = successful_calls
    _operations(checkpoint)[operation_id] = entry
    checkpoint.status = "running"
    checkpoint.pending_company_id = company_id
    checkpoint.pending_stage = operation
    checkpoint.pause_reason = None
    _persist_checkpoint(path, checkpoint)


def _finish_operation(
    checkpoint: RunCheckpoint,
    path: Path,
    operation_id: str,
    *,
    state: str = "completed",
    error_kind: str | None = None,
) -> None:
    entry = _operations(checkpoint)[operation_id]
    entry["state"] = state
    entry.pop("reservation_usd", None)
    if error_kind is not None:
        entry["error_kind"] = error_kind
    checkpoint.pending_company_id = None
    checkpoint.pending_stage = None
    _persist_checkpoint(path, checkpoint)


def _pause(
    checkpoint: RunCheckpoint,
    path: Path,
    *,
    status: str,
    reason: str,
    company_id: str | None = None,
    stage: str | None = None,
) -> RunCheckpoint:
    checkpoint.status = status
    checkpoint.pause_reason = reason
    checkpoint.pending_company_id = company_id
    checkpoint.pending_stage = stage
    _persist_checkpoint(path, checkpoint)
    return checkpoint


def _record_usage(paths: M2RunPaths, tracker: CostTracker, event: UsageEvent) -> None:
    append_usage_event(paths.usage_events, event)
    tracker.record(event)


def _replay_usage(paths: M2RunPaths) -> CostTracker:
    return CostTracker(load_usage_events(paths.usage_events))


def _exa_reservation(config: M2BatchConfig) -> float:
    if config.exa_request_reservation_usd is not None:
        return config.exa_request_reservation_usd
    return config.exa_budget_usd or 0.0


def _budget_allows(
    tracker: CostTracker,
    provider: str,
    ceiling: float | None,
    reservation: float = 0.0,
) -> bool:
    if ceiling is None:
        return True
    spend = tracker.provider_estimated_spend(provider)
    return spend is not None and spend + reservation <= ceiling + 1e-12


def _load_discovery_records(path: Path) -> list[DiscoveryRecord]:
    return [DiscoveryRecord.from_dict(payload) for payload in load_jsonl(path)]


def _append_discovery_batch(paths: M2RunPaths, records: Sequence[DiscoveryRecord]) -> None:
    for record in records:
        append_jsonl(paths.companies_raw, record.to_dict())


def _unknown_in_flight(checkpoint: RunCheckpoint) -> tuple[str, dict[str, Any]] | None:
    for operation_id, entry in sorted(_operations(checkpoint).items()):
        if entry.get("state") != "in_flight":
            continue
        provider = entry.get("provider")
        if provider in {"exa", "deepseek"}:
            return operation_id, entry
        if provider == "apify" and not isinstance(entry.get("run_id"), str):
            return operation_id, entry
    return None


def _provider_failure_pause(
    *,
    provider: str,
    exc: DiscoveryProviderError,
    checkpoint: RunCheckpoint,
    paths: M2RunPaths,
    tracker: CostTracker,
    operation_id: str,
    company_id: str | None,
    stage: str,
) -> RunCheckpoint:
    _record_usage(paths, tracker, exc.usage_event)
    state = "pending" if exc.retryable or exc.kind == "budget_exhausted" else "failed"
    _finish_operation(
        checkpoint,
        paths.checkpoint,
        operation_id,
        state=state,
        error_kind=exc.kind,
    )
    if exc.kind == "budget_exhausted":
        return _pause(
            checkpoint,
            paths.checkpoint,
            status="paused_budget",
            reason=f"{provider}_budget_exhausted",
            company_id=company_id,
            stage=stage,
        )
    if exc.kind in {"authentication", "invalid_request", "invalid_response", "permanent"}:
        return _pause(
            checkpoint,
            paths.checkpoint,
            status="failed",
            reason=f"{provider}_{exc.kind}",
            company_id=company_id,
            stage=stage,
        )
    return _pause(
        checkpoint,
        paths.checkpoint,
        status="paused_retryable",
        reason=f"{provider}_{exc.kind}",
        company_id=company_id,
        stage=stage,
    )


def _resume_apify(
    checkpoint: RunCheckpoint,
    paths: M2RunPaths,
    requests: Sequence[DiscoveryRequest],
    provider: ResumableDiscoveryProvider,
    tracker: CostTracker,
) -> RunCheckpoint | None:
    request_by_id = {request.request_id: request for request in requests}
    for operation_id, entry in sorted(_operations(checkpoint).items()):
        if entry.get("provider") != "apify" or entry.get("state") not in {"in_flight", "pending"}:
            continue
        run_id = _entry_str(entry, "run_id")
        request_id = _entry_str(entry, "request_id")
        if run_id is None or request_id is None:
            return _pause(
                checkpoint,
                paths.checkpoint,
                status="paused_unknown",
                reason="apify_start_outcome_unknown",
                stage="discovery",
            )
        request = request_by_id.get(request_id)
        if request is None:
            return _pause(
                checkpoint,
                paths.checkpoint,
                status="failed",
                reason="persisted_apify_request_missing",
                stage="discovery",
            )
        try:
            batch = provider.resume(request, run_id)
        except DiscoveryProviderError as exc:
            _record_usage(paths, tracker, exc.usage_event)
            if exc.retryable:
                entry["state"] = "pending"
                entry["error_kind"] = exc.kind
                _persist_checkpoint(paths.checkpoint, checkpoint)
                continue
            _finish_operation(
                checkpoint,
                paths.checkpoint,
                operation_id,
                state="failed",
                error_kind=exc.kind,
            )
            continue
        for event in batch.usage_events:
            _record_usage(paths, tracker, event)
        _append_discovery_batch(paths, batch.records)
        _finish_operation(checkpoint, paths.checkpoint, operation_id)
    return None


def _run_discovery_request(
    config: M2BatchConfig,
    paths: M2RunPaths,
    checkpoint: RunCheckpoint,
    request: DiscoveryRequest,
    provider: DiscoveryProvider,
    tracker: CostTracker,
) -> RunCheckpoint | None:
    operation_id = f"discovery:{request.request_id}"
    operation = "company_search" if request.provider == "exa" else "google_maps_search"
    reservation = _exa_reservation(config) if request.provider == "exa" else 0.0
    if request.provider == "exa" and not _budget_allows(
        tracker, "exa", config.exa_budget_usd, reservation
    ):
        return _pause(
            checkpoint,
            paths.checkpoint,
            status="paused_budget",
            reason="exa_budget_exhausted_or_unknown",
            stage="discovery",
        )

    _mark_in_flight(
        checkpoint,
        paths.checkpoint,
        operation_id=operation_id,
        provider=request.provider,
        operation=operation,
        request_id=request.request_id,
        reservation_usd=reservation if reservation else None,
    )
    try:
        batch = provider.search(request)
    except DiscoveryProviderError as exc:
        if request.provider == "apify":
            _record_usage(paths, tracker, exc.usage_event)
            entry = _operations(checkpoint)[operation_id]
            run_id = _entry_str(entry, "run_id")
            if exc.retryable and run_id is not None:
                entry["state"] = "pending"
                entry["error_kind"] = exc.kind
                checkpoint.pending_company_id = None
                checkpoint.pending_stage = None
                _persist_checkpoint(paths.checkpoint, checkpoint)
                return None
            if exc.retryable:
                return _pause(
                    checkpoint,
                    paths.checkpoint,
                    status="paused_unknown",
                    reason="apify_start_outcome_unknown",
                    stage="discovery",
                )
            _finish_operation(
                checkpoint,
                paths.checkpoint,
                operation_id,
                state="failed",
                error_kind=exc.kind,
            )
            return None
        return _provider_failure_pause(
            provider="exa",
            exc=exc,
            checkpoint=checkpoint,
            paths=paths,
            tracker=tracker,
            operation_id=operation_id,
            company_id=None,
            stage="discovery",
        )

    for event in batch.usage_events:
        _record_usage(paths, tracker, event)
    _append_discovery_batch(paths, batch.records)
    if request.provider == "apify" and batch.usage_events:
        run_id = batch.usage_events[-1].metadata.get("run_id")
        if isinstance(run_id, str):
            _operations(checkpoint)[operation_id]["run_id"] = run_id
    _finish_operation(checkpoint, paths.checkpoint, operation_id)
    return None


def _discovery_phase(
    config: M2BatchConfig,
    paths: M2RunPaths,
    checkpoint: RunCheckpoint,
    discovery: Mapping[str, DiscoveryProvider],
    tracker: CostTracker,
) -> RunCheckpoint | None:
    requests = build_discovery_requests(
        include_apify=config.include_apify and discovery.get("apify") is not None,
        max_candidates=config.max_candidates,
        apify_budget_usd=config.apify_budget_usd,
    )
    apify = discovery.get("apify")
    if apify is not None:
        paused = _resume_apify(
            checkpoint,
            paths,
            requests,
            cast(ResumableDiscoveryProvider, apify),
            tracker,
        )
        if paused is not None:
            return paused

    unknown = _unknown_in_flight(checkpoint)
    if unknown is not None:
        operation_id, entry = unknown
        return _pause(
            checkpoint,
            paths.checkpoint,
            status="paused_unknown",
            reason=f"unknown_in_flight:{operation_id}",
            company_id=_entry_str(entry, "company_id"),
            stage=_entry_str(entry, "operation"),
        )

    for request in requests:
        operation_id = f"discovery:{request.request_id}"
        entry = _operations(checkpoint).get(operation_id)
        if entry is not None and entry.get("state") in {"completed", "failed"}:
            continue
        if (
            request.provider == "apify"
            and entry is not None
            and entry.get("state") == "pending"
            and isinstance(entry.get("run_id"), str)
        ):
            continue
        provider = discovery.get(request.provider)
        if provider is None:
            if request.provider == "apify":
                _operations(checkpoint)[operation_id] = {
                    "provider": "apify",
                    "operation": "google_maps_search",
                    "request_id": request.request_id,
                    "state": "failed",
                    "error_kind": "unavailable",
                }
                _persist_checkpoint(paths.checkpoint, checkpoint)
                continue
            return _pause(
                checkpoint,
                paths.checkpoint,
                status="failed",
                reason="exa_provider_unavailable",
                stage="discovery",
            )
        paused = _run_discovery_request(config, paths, checkpoint, request, provider, tracker)
        if paused is not None:
            return paused

    pending = [
        operation_id
        for operation_id, entry in sorted(_operations(checkpoint).items())
        if entry.get("provider") == "apify"
        and entry.get("state") == "pending"
        and isinstance(entry.get("run_id"), str)
    ]
    if pending:
        return _pause(
            checkpoint,
            paths.checkpoint,
            status="paused_retryable",
            reason=f"apify_pending:{pending[0]}",
            stage="discovery",
        )
    _stages(checkpoint)["discovery"] = "completed"
    _persist_checkpoint(paths.checkpoint, checkpoint)
    return None


def _deduplicate_raw(paths: M2RunPaths, checkpoint: RunCheckpoint) -> list[CompanyRecord]:
    result = deduplicate(_load_discovery_records(paths.companies_raw))
    checkpoint.provider_state["unresolved_record_ids"] = [
        record.record_id for record in result.unresolved_records
    ]
    _stages(checkpoint)["deduplication"] = "completed"
    _persist_checkpoint(paths.checkpoint, checkpoint)
    return result.companies


def _latest_company(base: CompanyRecord, paths: M2RunPaths) -> CompanyRecord:
    latest = load_latest_company_records(paths.companies_extracted).get(base.company_id)
    return base if latest is None else latest


def _persist_research_snapshot(
    paths: M2RunPaths,
    company: CompanyRecord,
    bundle: EvidenceBundle,
    *,
    completed: bool,
) -> CompanyRecord:
    updated = deepcopy(company)
    updated.evidence = deepcopy(bundle.items)
    updated.stage_status["research"] = "completed" if completed else "in_progress"
    append_company_snapshot(paths.companies_extracted, updated)
    return updated


def _persist_research_progress(
    *,
    config: M2BatchConfig,
    paths: M2RunPaths,
    checkpoint: RunCheckpoint,
    tracker: CostTracker,
    company: CompanyRecord,
    operation_id: str,
    query_count: int,
    cumulative_items: list[Any],
    delta: EvidenceBundle,
) -> tuple[CompanyRecord, list[Any]]:
    if delta.company_id != company.company_id:
        raise ValueError("research progress bundle company_id mismatch")
    for event in delta.usage_events:
        _record_usage(paths, tracker, event)
    cumulative_items.extend(deepcopy(delta.items))
    cumulative = build_evidence_bundle(
        company=company,
        items=cumulative_items,
        raw_records=[],
        usage_events=[],
    )
    company = _persist_research_snapshot(paths, company, cumulative, completed=False)
    cumulative_items = deepcopy(cumulative.items)

    entry = _operations(checkpoint)[operation_id]
    calls = _successful_calls(entry, query_count) + 1
    if calls > query_count:
        raise ValueError("research progress exceeded the bounded query count")
    entry["successful_calls"] = calls
    _persist_checkpoint(paths.checkpoint, checkpoint)
    if calls < query_count and not _budget_allows(
        tracker,
        "exa",
        config.exa_budget_usd,
        _exa_reservation(config),
    ):
        raise _ResearchBudgetPause
    return company, cumulative_items


def _research_company(
    config: M2BatchConfig,
    paths: M2RunPaths,
    checkpoint: RunCheckpoint,
    company: CompanyRecord,
    researcher: EvidenceResearcher,
    tracker: CostTracker,
) -> tuple[CompanyRecord, EvidenceBundle] | RunCheckpoint:
    operation_id = f"research:{company.company_id}"
    entry = _operations(checkpoint).get(operation_id)
    if entry is not None and entry.get("state") == "in_flight":
        return _pause(
            checkpoint,
            paths.checkpoint,
            status="paused_unknown",
            reason=f"unknown_in_flight:{operation_id}",
            company_id=company.company_id,
            stage="company_research",
        )

    requests = build_research_requests(company)
    completed_queries = _successful_calls(entry, len(requests))
    reservation = _exa_reservation(config)
    if not _budget_allows(tracker, "exa", config.exa_budget_usd, reservation):
        return _pause(
            checkpoint,
            paths.checkpoint,
            status="paused_budget",
            reason="exa_budget_exhausted_or_unknown",
            company_id=company.company_id,
            stage="research",
        )
    _mark_in_flight(
        checkpoint,
        paths.checkpoint,
        operation_id=operation_id,
        provider="exa",
        operation="company_research",
        company_id=company.company_id,
        reservation_usd=reservation if reservation else None,
        successful_calls=completed_queries,
    )
    cumulative_items: list[Any] = deepcopy(company.evidence)

    def progress(delta: EvidenceBundle) -> None:
        nonlocal company, cumulative_items
        company, cumulative_items = _persist_research_progress(
            config=config,
            paths=paths,
            checkpoint=checkpoint,
            tracker=tracker,
            company=company,
            operation_id=operation_id,
            query_count=len(requests),
            cumulative_items=cumulative_items,
            delta=delta,
        )

    try:
        if completed_queries:
            researcher.resume(company, start_index=completed_queries, on_progress=progress)
        else:
            researcher.research(company, on_progress=progress)
    except _ResearchBudgetPause:
        operation = _operations(checkpoint)[operation_id]
        operation["state"] = "pending"
        operation.pop("error_kind", None)
        operation.pop("reservation_usd", None)
        _persist_checkpoint(paths.checkpoint, checkpoint)
        return _pause(
            checkpoint,
            paths.checkpoint,
            status="paused_budget",
            reason="exa_budget_exhausted_or_unknown",
            company_id=company.company_id,
            stage="research",
        )
    except DiscoveryProviderError as exc:
        return _provider_failure_pause(
            provider="exa",
            exc=exc,
            checkpoint=checkpoint,
            paths=paths,
            tracker=tracker,
            operation_id=operation_id,
            company_id=company.company_id,
            stage="research",
        )

    bundle = build_evidence_bundle(
        company=company,
        items=cumulative_items,
        raw_records=[],
        usage_events=[],
    )
    company = _persist_research_snapshot(paths, company, bundle, completed=True)
    _finish_operation(checkpoint, paths.checkpoint, operation_id)
    return company, bundle


def _extract_company(
    config: M2BatchConfig,
    paths: M2RunPaths,
    checkpoint: RunCheckpoint,
    company: CompanyRecord,
    bundle: EvidenceBundle,
    extractor: Extractor,
    tracker: CostTracker,
) -> CompanyRecord | RunCheckpoint:
    operation_id = f"extraction:{company.company_id}"
    entry = _operations(checkpoint).get(operation_id)
    if entry is not None and entry.get("state") == "in_flight":
        return _pause(
            checkpoint,
            paths.checkpoint,
            status="paused_unknown",
            reason=f"unknown_in_flight:{operation_id}",
            company_id=company.company_id,
            stage="structured_extraction",
        )
    reservation = extractor.reservation_cost_usd(company, bundle)
    if (
        isinstance(reservation, bool)
        or not isinstance(reservation, (int, float))
        or not math.isfinite(reservation)
        or reservation < 0
    ):
        raise ValueError("DeepSeek reservation must be a finite nonnegative number")
    if not _budget_allows(
        tracker, "deepseek", config.deepseek_budget_usd, float(reservation)
    ):
        return _pause(
            checkpoint,
            paths.checkpoint,
            status="paused_budget",
            reason="deepseek_budget_exhausted",
            company_id=company.company_id,
            stage="extraction",
        )
    _mark_in_flight(
        checkpoint,
        paths.checkpoint,
        operation_id=operation_id,
        provider="deepseek",
        operation="structured_extraction",
        company_id=company.company_id,
        reservation_usd=float(reservation),
    )
    try:
        result = extractor.extract(company, bundle)
    except DiscoveryProviderError as exc:
        return _provider_failure_pause(
            provider="deepseek",
            exc=exc,
            checkpoint=checkpoint,
            paths=paths,
            tracker=tracker,
            operation_id=operation_id,
            company_id=company.company_id,
            stage="extraction",
        )
    _record_usage(paths, tracker, result.usage_event)
    updated = apply_extraction(company, bundle, result)
    append_company_snapshot(paths.companies_extracted, updated)
    _finish_operation(checkpoint, paths.checkpoint, operation_id)
    return updated


def _research_and_extract_phase(
    config: M2BatchConfig,
    paths: M2RunPaths,
    checkpoint: RunCheckpoint,
    companies: Sequence[CompanyRecord],
    researcher: EvidenceResearcher,
    extractor: Extractor,
    tracker: CostTracker,
) -> RunCheckpoint | None:
    completed = 0
    for selected in select_research_companies(companies, limit=config.max_extracted):
        company = _latest_company(selected, paths)
        if company.stage_status.get("extraction") == "completed":
            completed += 1
            if completed >= config.max_extracted:
                break
            continue

        if company.stage_status.get("research") == "completed":
            bundle = EvidenceBundle(
                company_id=company.company_id,
                items=company.evidence,
                raw_records=[],
                usage_events=[],
            )
        else:
            research = _research_company(
                config, paths, checkpoint, company, researcher, tracker
            )
            if isinstance(research, RunCheckpoint):
                return research
            company, bundle = research

        if not bundle.items:
            checkpoint.status = "completed"
            checkpoint.pause_reason = "empty_evidence"
            checkpoint.pending_company_id = company.company_id
            checkpoint.pending_stage = "extraction"
            _persist_checkpoint(paths.checkpoint, checkpoint)
            return checkpoint

        extraction = _extract_company(
            config, paths, checkpoint, company, bundle, extractor, tracker
        )
        if isinstance(extraction, RunCheckpoint):
            return extraction
        completed += 1
        if completed >= config.max_extracted:
            break
    return None


def run_m2_batch(
    config: M2BatchConfig,
    *,
    discovery: Mapping[str, DiscoveryProvider],
    researcher: EvidenceResearcher,
    extractor: Extractor,
) -> RunCheckpoint:
    """Execute or resume one injected M2 batch."""
    paths = resolve_m2_paths(config)
    if not config.execute_live:
        return RunCheckpoint(
            run_id=config.run_id,
            status="dry_run",
            pause_reason="live_execution_not_authorized",
        )

    paths.run_dir.mkdir(parents=True, exist_ok=True)
    _validate_artifact_paths(paths)
    checkpoint = load_checkpoint(paths.checkpoint) or _initial_checkpoint(config.run_id)
    if checkpoint.run_id != config.run_id:
        raise ValueError("checkpoint run_id does not match requested run")
    _operations(checkpoint)
    _stages(checkpoint)
    if checkpoint.status == "failed":
        return checkpoint

    tracker = _replay_usage(paths)
    _persist_checkpoint(paths.checkpoint, checkpoint)
    unknown = _unknown_in_flight(checkpoint)
    if unknown is not None and unknown[1].get("provider") != "apify":
        operation_id, entry = unknown
        return _pause(
            checkpoint,
            paths.checkpoint,
            status="paused_unknown",
            reason=f"unknown_in_flight:{operation_id}",
            company_id=_entry_str(entry, "company_id"),
            stage=_entry_str(entry, "operation"),
        )

    if _stages(checkpoint).get("discovery") != "completed":
        paused = _discovery_phase(config, paths, checkpoint, discovery, tracker)
        if paused is not None:
            return paused

    companies = _deduplicate_raw(paths, checkpoint)
    paused = _research_and_extract_phase(
        config, paths, checkpoint, companies, researcher, extractor, tracker
    )
    if paused is not None:
        return paused

    checkpoint.status = "completed"
    checkpoint.pending_company_id = None
    checkpoint.pending_stage = None
    checkpoint.pause_reason = None
    _stages(checkpoint)["m2_batch"] = "completed"
    _persist_checkpoint(paths.checkpoint, checkpoint)
    return checkpoint


class ApifyRunRecorder:
    """Persist an Actor run ID into the single in-flight Apify operation."""

    def __init__(self, checkpoint_path: Path) -> None:
        self._checkpoint_path = checkpoint_path

    def __call__(self, run_id: str) -> None:
        checkpoint = load_checkpoint(self._checkpoint_path)
        if checkpoint is None:
            raise RuntimeError("Apify run started before an in-flight checkpoint was persisted")
        candidates = [
            entry
            for entry in _operations(checkpoint).values()
            if entry.get("provider") == "apify" and entry.get("state") == "in_flight"
        ]
        if len(candidates) != 1:
            raise RuntimeError("expected exactly one in-flight Apify operation")
        candidates[0]["run_id"] = run_id
        _persist_checkpoint(self._checkpoint_path, checkpoint)


def run_live_m2(config: M2BatchConfig) -> LiveM2Result:
    """Compose credentials and providers only for an explicitly live M2 execution."""
    if not config.execute_live:
        raise ValueError("run_live_m2 requires execute_live")
    paths = resolve_m2_paths(config)
    exa_key = os.environ.get("EXA_API_KEY", "")
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    apify_token = os.environ.get("APIFY_TOKEN", "")
    if not exa_key or not deepseek_key:
        raise MissingProviderCredentials

    active = replace(
        config,
        include_apify=config.include_apify and bool(apify_token),
    )
    with httpx.Client() as client:
        discovery: dict[str, DiscoveryProvider] = {
            "exa": ExaDiscoveryProvider(api_key=exa_key, client=client)
        }
        if active.include_apify:
            discovery["apify"] = ApifyDiscoveryProvider(
                api_token=apify_token,
                client=client,
                on_run_started=ApifyRunRecorder(paths.checkpoint),
            )
        checkpoint = run_m2_batch(
            active,
            discovery=discovery,
            researcher=ExaEvidenceResearcher(api_key=exa_key, client=client),
            extractor=DeepSeekExtractor(
                api_key=deepseek_key,
                client=client,
                model=_DEEPSEEK_MODEL,
                prices=_DEFAULT_PRICES,
            ),
        )
    return LiveM2Result(checkpoint=checkpoint, apify_enabled=active.include_apify)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m leads_discovery.pipeline.m2_batch")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--max-candidates", type=int, default=100)
    parser.add_argument("--max-extracted", type=int, default=20)
    parser.add_argument("--include-apify", action="store_true")
    parser.add_argument("--apify-budget-usd", type=float, default=0.25)
    parser.add_argument("--deepseek-budget-usd", type=float, required=True)
    parser.add_argument("--exa-budget-usd", type=float)
    parser.add_argument("--exa-request-reservation-usd", type=float)
    parser.add_argument("--execute-live", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = M2BatchConfig(
        run_id=args.run_id,
        data_root=args.data_root,
        max_candidates=args.max_candidates,
        max_extracted=args.max_extracted,
        include_apify=args.include_apify,
        apify_budget_usd=args.apify_budget_usd,
        deepseek_budget_usd=args.deepseek_budget_usd,
        exa_budget_usd=args.exa_budget_usd,
        exa_request_reservation_usd=args.exa_request_reservation_usd,
        execute_live=args.execute_live,
    )
    resolve_m2_paths(config)
    if not config.execute_live:
        return 0
    try:
        result = run_live_m2(config)
    except MissingProviderCredentials:
        return 2
    return 0 if result.checkpoint.status in {"completed", "paused_budget", "paused_unknown"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
