"""Narrow path-safe and resumable M2 discovery-to-extraction batch runner."""

from __future__ import annotations

import argparse
import inspect
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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
    RunCheckpoint,
    UsageEvent,
)
from leads_discovery.pipeline.costs import CostTracker
from leads_discovery.pipeline.paid_operations import (
    PaidAdmissionPolicy,
    PaidOperationLifecycle,
    classify_paid_error,
)
from leads_discovery.pipeline.state import (
    append_company_snapshot,
    append_jsonl,
    load_checkpoint,
    load_jsonl,
    load_latest_company_records,
    load_usage_events,
    read_json,
    write_checkpoint,
    write_json_atomic,
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


class _ResearchBudgetPause(Exception):
    """Signal a known local Exa budget stop after durable successful-call progress."""


@dataclass(frozen=True, slots=True)
class M2BatchConfig:
    """Configure one bounded M2 run without embedding credentials or pooled budgets."""

    run_id: str
    data_root: Path
    max_candidates: int = 100
    max_extracted: int = 20
    include_apify: bool = False
    apify_budget_usd: float = 0.25
    deepseek_budget_usd: float | None = None
    exa_budget_usd: float | None = None
    execute_live: bool = False


@dataclass(frozen=True, slots=True)
class _RunPaths:
    """Resolve the seven exact M2 artifacts beneath one validated run directory."""

    run_dir: Path
    companies_raw: Path
    companies_deduped: Path
    research_raw: Path
    companies_extracted: Path
    usage_events: Path
    usage: Path
    checkpoint: Path

    def artifacts(self) -> tuple[Path, ...]:
        """Return every writable M2 artifact path in deterministic order."""
        return (
            self.companies_raw,
            self.companies_deduped,
            self.research_raw,
            self.companies_extracted,
            self.usage_events,
            self.usage,
            self.checkpoint,
        )


def _now() -> str:
    """Return a UTC timestamp for checkpoint state transitions."""
    return datetime.now(UTC).isoformat()


def _validate_config(config: M2BatchConfig) -> _RunPaths:
    """Validate all controls before filesystem mutation or provider work."""
    if not isinstance(config.run_id, str) or not _RUN_ID.fullmatch(config.run_id):
        raise ValueError("run_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    if isinstance(config.max_candidates, bool) or not isinstance(config.max_candidates, int):
        raise ValueError("max_candidates must be an integer in 1..100")
    if not 1 <= config.max_candidates <= 100:
        raise ValueError("max_candidates must be an integer in 1..100")
    if isinstance(config.max_extracted, bool) or not isinstance(config.max_extracted, int):
        raise ValueError("max_extracted must be an integer in 1..20")
    if not 1 <= config.max_extracted <= 20:
        raise ValueError("max_extracted must be an integer in 1..20")
    if (
        isinstance(config.apify_budget_usd, bool)
        or not isinstance(config.apify_budget_usd, (int, float))
        or not math.isfinite(config.apify_budget_usd)
        or not 0 <= config.apify_budget_usd <= 1
    ):
        raise ValueError("apify_budget_usd must be in 0..1")
    for name, value in (
        ("deepseek_budget_usd", config.deepseek_budget_usd),
        ("exa_budget_usd", config.exa_budget_usd),
    ):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} must be a nonnegative number or null")
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
    return _RunPaths(
        run_dir=run_dir,
        companies_raw=run_dir / "companies_raw.jsonl",
        companies_deduped=run_dir / "companies_deduped.jsonl",
        research_raw=run_dir / "research_raw.jsonl",
        companies_extracted=run_dir / "companies_extracted.jsonl",
        usage_events=run_dir / "usage_events.jsonl",
        usage=run_dir / "usage.json",
        checkpoint=run_dir / "checkpoint.json",
    )


def _validate_artifact_paths(paths: _RunPaths) -> None:
    """Reject pre-existing artifact symlinks before any artifact read or write."""
    for path in paths.artifacts():
        if path.is_symlink():
            raise ValueError(f"artifact path must not be a symlink: {path.name}")


def _initial_checkpoint(run_id: str) -> RunCheckpoint:
    """Create a fresh running checkpoint with explicit operation and stage maps."""
    return RunCheckpoint(
        run_id=run_id,
        status="running",
        provider_state={"operations": {}, "stages": {}},
    )


def _operations(checkpoint: RunCheckpoint) -> dict[str, dict[str, Any]]:
    """Return and validate the mutable safe operation-state map in a checkpoint."""
    raw = checkpoint.provider_state.setdefault("operations", {})
    if not isinstance(raw, dict):
        raise ValueError("checkpoint operations must be an object")
    result: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise ValueError("checkpoint operation entries must be objects")
        state = value.get("state")
        if state not in _OPERATION_STATES:
            raise ValueError("checkpoint operation has an invalid state")
        result[key] = cast(dict[str, Any], value)
    checkpoint.provider_state["operations"] = result
    return result


def _stages(checkpoint: RunCheckpoint) -> dict[str, str]:
    """Return and validate the persisted free-stage completion map."""
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
    """Return one optional string checkpoint field without coercion."""
    value = entry.get(key)
    return value if isinstance(value, str) else None


def _research_successful_calls(
    entry: dict[str, Any] | None,
    *,
    max_queries: int,
) -> int:
    """Return a validated durable Exa completed-query cursor from operation state."""
    if entry is None:
        return 0
    value = entry.get("successful_calls", 0)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= max_queries
    ):
        raise ValueError("research successful_calls checkpoint value is invalid")
    return value


def _uses_resumable_exa_researcher(researcher: ExaEvidenceResearcher) -> bool:
    """Return whether the injected researcher uses the built-in resumable Exa loop."""
    return type(researcher).research is ExaEvidenceResearcher.research


def _persist_checkpoint(path: Path, checkpoint: RunCheckpoint) -> None:
    """Refresh the checkpoint timestamp and atomically persist all safe operation state."""
    checkpoint.updated_at = _now()
    write_checkpoint(path, checkpoint)


def _pause(
    checkpoint: RunCheckpoint,
    path: Path,
    *,
    status: str,
    reason: str,
    company_id: str | None = None,
    stage: str | None = None,
) -> RunCheckpoint:
    """Persist and return a durable pause/failure checkpoint without discarding completed work."""
    checkpoint.status = status
    checkpoint.pause_reason = reason
    checkpoint.pending_company_id = company_id
    checkpoint.pending_stage = stage
    _persist_checkpoint(path, checkpoint)
    return checkpoint


def _replay_usage(paths: _RunPaths) -> CostTracker:
    """Rebuild provider budgets by streaming the bounded append-only usage ledger."""
    tracker = CostTracker(load_usage_events(paths.usage_events))
    expected = cast(dict[str, Any], tracker.summary())
    try:
        current = read_json(paths.usage)
    except ValueError:
        current = None
    if current != expected:
        write_json_atomic(paths.usage, expected)
    return tracker


def _exa_search_reservation(max_results: int) -> float:
    """Reserve the current published worst-case Exa Search+highlights cost for a bounded call."""
    if (
        isinstance(max_results, bool)
        or not isinstance(max_results, int)
        or not 1 <= max_results <= 100
    ):
        raise ValueError("Exa reservation result cap must be in 1..100")
    search = 0.007 + max(0, max_results - 10) * 0.001
    highlights = max_results * 0.001
    return round(search + highlights, 6)


def _load_discovery_records(path: Path) -> list[DiscoveryRecord]:
    """Load all durably retained typed discovery rows for deduplication or resume."""
    return [DiscoveryRecord.from_dict(payload) for payload in load_jsonl(path)]


def _append_discovery_batch(paths: _RunPaths, records: Sequence[DiscoveryRecord]) -> None:
    """Persist every complete typed discovery row exactly once in provider order."""
    for record in records:
        append_jsonl(paths.companies_raw, record.to_dict())


def _append_research_raw(paths: _RunPaths, rows: Sequence[dict[str, Any]]) -> None:
    """Persist complete Exa research rows outside the bounded model evidence bundle."""
    for row in rows:
        append_jsonl(paths.research_raw, deepcopy(row))


def _resume_apify_if_needed(
    checkpoint: RunCheckpoint,
    paths: _RunPaths,
    requests: Sequence[DiscoveryRequest],
    provider: DiscoveryProvider,
    lifecycle: PaidOperationLifecycle,
) -> RunCheckpoint | None:
    """Resume persisted Apify Actor runs and never start automatic replacements."""
    request_by_id = {request.request_id: request for request in requests}
    for operation_id, entry in sorted(_operations(checkpoint).items()):
        if entry.get("provider") != "apify" or entry.get("state") not in {
            "in_flight",
            "pending",
        }:
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
        resume = getattr(provider, "resume", None)
        if not callable(resume):
            return _pause(
                checkpoint,
                paths.checkpoint,
                status="failed",
                reason="apify_provider_cannot_resume",
                stage="discovery",
            )
        try:
            resumed = resume(request, run_id)
        except DiscoveryProviderError as exc:
            lifecycle.record_usage(exc.usage_event)
            if exc.retryable:
                lifecycle.update_operation(
                    operation_id,
                    fields={"error_kind": exc.kind},
                    state="pending",
                )
                continue
            lifecycle.finish(operation_id, state="failed", error_kind=exc.kind)
            continue
        if not isinstance(resumed, DiscoveryBatch):
            return _pause(
                checkpoint,
                paths.checkpoint,
                status="failed",
                reason="apify_resume_returned_invalid_batch",
                stage="discovery",
            )
        for event in resumed.usage_events:
            lifecycle.record_usage(event)
        _append_discovery_batch(paths, resumed.records)
        lifecycle.finish(operation_id)
    return None


def _discovery_phase(
    config: M2BatchConfig,
    paths: _RunPaths,
    checkpoint: RunCheckpoint,
    discovery: Mapping[str, DiscoveryProvider],
    lifecycle: PaidOperationLifecycle,
) -> RunCheckpoint | None:
    """Run or resume the bounded discovery plan while treating Apify as optional."""
    requests = build_discovery_requests(
        include_apify=config.include_apify and discovery.get("apify") is not None,
        max_candidates=config.max_candidates,
        apify_budget_usd=config.apify_budget_usd,
    )
    apify = discovery.get("apify")
    if apify is not None:
        paused = _resume_apify_if_needed(checkpoint, paths, requests, apify, lifecycle)
        if paused is not None:
            return paused

    if lifecycle.freeze_if_unknown(
        replayable=lambda _operation_id, entry: entry.get("provider") == "apify"
        and isinstance(entry.get("run_id"), str),
    ) is not None:
        return checkpoint

    for request in requests:
        operation_id = f"discovery:{request.request_id}"
        request_entry = _operations(checkpoint).get(operation_id)
        if request_entry is not None and request_entry.get("state") in {"completed", "failed"}:
            continue
        if (
            request.provider == "apify"
            and request_entry is not None
            and request_entry.get("state") == "pending"
            and isinstance(request_entry.get("run_id"), str)
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
        reservation = (
            _exa_search_reservation(request.max_results_total)
            if request.provider == "exa"
            else request.max_cost_usd or 0.0
        )
        operation = "company_search" if request.provider == "exa" else "google_maps_search"
        ceiling = (
            config.exa_budget_usd
            if request.provider == "exa"
            else config.apify_budget_usd
        )
        admission = PaidAdmissionPolicy(
            provider=request.provider,
            ceiling=ceiling,
            budget_reason=f"{request.provider}_budget_exhausted",
            usage_unknown_reason=f"{request.provider}_usage_unknown",
            pause_on_budget=request.provider != "apify",
        )
        if not lifecycle.admit(
            operation_id,
            operation=operation,
            policy=admission,
            reservation_usd=reservation,
            request_id=request.request_id,
            pending_stage="discovery",
        ):
            if request.provider == "apify" and checkpoint.status == "running":
                continue
            return checkpoint
        try:
            batch = provider.search(request)
        except DiscoveryProviderError as exc:
            lifecycle.record_usage(exc.usage_event)
            if request.provider == "apify":
                apify_entry = _operations(checkpoint)[operation_id]
                run_id = _entry_str(apify_entry, "run_id")
                if exc.retryable and run_id is not None:
                    lifecycle.update_operation(
                        operation_id,
                        fields={"error_kind": exc.kind},
                        state="pending",
                        clear_pending=True,
                    )
                    continue
                if exc.retryable:
                    lifecycle.pause(
                        status="paused_unknown",
                        reason="apify_start_outcome_unknown",
                        stage="discovery",
                    )
                    return checkpoint
                lifecycle.finish(
                    operation_id,
                    state="failed",
                    error_kind=exc.kind,
                )
                continue
            return _handle_required_paid_error(
                exc=exc,
                checkpoint=checkpoint,
                paths=paths,
                lifecycle=lifecycle,
                operation_id=operation_id,
                company_id=None,
                stage="discovery",
            )
        for event in batch.usage_events:
            lifecycle.record_usage(event)
        _append_discovery_batch(paths, batch.records)
        if request.provider == "apify" and batch.usage_events:
            run_id = batch.usage_events[-1].metadata.get("run_id")
            if isinstance(run_id, str):
                lifecycle.update_operation(operation_id, fields={"run_id": run_id})
        lifecycle.finish(operation_id)

    pending_apify = [
        operation_id
        for operation_id, entry in sorted(_operations(checkpoint).items())
        if entry.get("provider") == "apify"
        and entry.get("state") == "pending"
        and isinstance(entry.get("run_id"), str)
    ]
    if pending_apify:
        lifecycle.pause(
            status="paused_retryable",
            reason=f"apify_pending:{pending_apify[0]}",
            stage="discovery",
        )
        return checkpoint
    _stages(checkpoint)["discovery"] = "completed"
    _persist_checkpoint(paths.checkpoint, checkpoint)
    return None


def _dedup_phase(paths: _RunPaths, checkpoint: RunCheckpoint) -> list[CompanyRecord]:
    """Run deterministic local dedup once and persist canonical snapshots for later resume."""
    stages = _stages(checkpoint)
    if stages.get("deduplication") == "completed" and paths.companies_deduped.exists():
        return sorted(
            load_latest_company_records(paths.companies_deduped).values(),
            key=lambda item: item.company_id,
        )
    result = deduplicate(_load_discovery_records(paths.companies_raw))
    if paths.companies_deduped.exists():
        paths.companies_deduped.unlink()
    for company in result.companies:
        append_company_snapshot(paths.companies_deduped, company)
    stages["deduplication"] = "completed"
    checkpoint.provider_state["unresolved_record_ids"] = [
        record.record_id for record in result.unresolved_records
    ]
    _persist_checkpoint(paths.checkpoint, checkpoint)
    return result.companies


def _latest_company(base: CompanyRecord, paths: _RunPaths) -> CompanyRecord:
    """Return the newest research/extraction snapshot, falling back to deduplicated data."""
    latest = load_latest_company_records(paths.companies_extracted).get(base.company_id)
    return base if latest is None else latest


def _persist_research_snapshot(
    paths: _RunPaths,
    company: CompanyRecord,
    bundle: EvidenceBundle,
    *,
    completed: bool,
) -> CompanyRecord:
    """Persist cumulative evidence with an explicit incomplete/completed research stage state."""
    updated = CompanyRecord.from_dict(company.to_dict())
    updated.evidence = [deepcopy(item) for item in bundle.items]
    updated.stage_status["research"] = "completed" if completed else "in_progress"
    append_company_snapshot(paths.companies_extracted, updated)
    return updated


def _supports_research_progress(researcher: ExaEvidenceResearcher) -> bool:
    """Return whether an injected researcher accepts the optional on_progress keyword."""
    try:
        parameters = inspect.signature(researcher.research).parameters
    except (TypeError, ValueError):
        return False
    return "on_progress" in parameters


def _deepseek_reservation(
    extractor: DeepSeekExtractor,
    company: CompanyRecord,
    bundle: EvidenceBundle,
) -> float:
    """Return a validated conservative reservation for real extractors and simple test fakes."""
    reservation_method = getattr(extractor, "reservation_cost_usd", None)
    if callable(reservation_method):
        value = reservation_method(company, bundle)
    else:
        prices = getattr(extractor, "prices", None)
        if not isinstance(prices, DeepSeekPriceSchedule):
            raise TypeError("extractor must expose reservation_cost_usd or a price schedule")
        prompt_chars = len(json.dumps(company.to_dict(), sort_keys=True)) + sum(
            len(item.excerpt or "") + len(item.url) + len(item.title or "")
            for item in bundle.items
        )
        value = (
            prompt_chars * prices.cache_miss_input_per_million
            + 2048 * prices.output_per_million
        ) / 1_000_000
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("DeepSeek reservation must be a finite nonnegative number")
    return float(value)


def _handle_required_paid_error(
    *,
    exc: DiscoveryProviderError,
    checkpoint: RunCheckpoint,
    paths: _RunPaths,
    lifecycle: PaidOperationLifecycle,
    operation_id: str,
    company_id: str | None,
    stage: str,
) -> RunCheckpoint:
    """Apply one lifecycle-owned error policy for required paid M2 providers."""
    lifecycle.record_usage(exc.usage_event)
    disposition = classify_paid_error(exc.kind, exc.retryable)
    if disposition == "unknown":
        lifecycle.pause(
            status="paused_unknown",
            reason=f"ambiguous_paid_outcome:{operation_id}",
            company_id=company_id,
            stage=stage,
        )
        return checkpoint

    if disposition in {"budget", "retryable"}:
        lifecycle.finish(operation_id, state="pending", error_kind=exc.kind)
    else:
        lifecycle.finish(operation_id, state="failed", error_kind=exc.kind)
    if disposition == "budget":
        lifecycle.pause(
            status="paused_budget",
            reason=f"{exc.provider}_budget_exhausted",
            company_id=company_id,
            stage=stage,
        )
        return checkpoint
    if disposition == "retryable":
        lifecycle.pause(
            status="paused_retryable",
            reason=f"{exc.provider}_{exc.kind}",
            company_id=company_id,
            stage=stage,
        )
        return checkpoint
    return _pause(
        checkpoint,
        paths.checkpoint,
        status="failed",
        reason=f"{exc.provider}_{exc.kind}",
        company_id=company_id,
        stage=stage,
    )


def _research_and_extract_phase(
    config: M2BatchConfig,
    paths: _RunPaths,
    checkpoint: RunCheckpoint,
    companies: Sequence[CompanyRecord],
    researcher: ExaEvidenceResearcher,
    extractor: DeepSeekExtractor,
    lifecycle: PaidOperationLifecycle,
) -> RunCheckpoint | None:
    """Research and extract selected companies in order under independent budgets."""
    selected = select_research_companies(companies, limit=config.max_extracted)
    exa_admission = PaidAdmissionPolicy(
        provider="exa",
        ceiling=config.exa_budget_usd,
        budget_reason="exa_budget_exhausted",
        usage_unknown_reason="exa_usage_unknown",
    )
    deepseek_admission = PaidAdmissionPolicy(
        provider="deepseek",
        ceiling=config.deepseek_budget_usd,
        budget_reason="deepseek_budget_exhausted",
        usage_unknown_reason="deepseek_usage_unknown",
    )
    completed_count = 0
    for selected_company in selected:
        company = _latest_company(selected_company, paths)
        if company.stage_status.get("extraction") == "completed":
            completed_count += 1
            if completed_count >= config.max_extracted:
                break
            continue

        research_op = f"research:{company.company_id}"
        research_entry = _operations(checkpoint).get(research_op)
        if research_entry is not None and research_entry.get("state") == "in_flight":
            lifecycle.pause(
                status="paused_unknown",
                reason=f"unknown_in_flight:{research_op}",
                company_id=company.company_id,
                stage="company_research",
            )
            return checkpoint

        if company.stage_status.get("research") != "completed":
            research_requests = build_research_requests(company)
            completed_queries = _research_successful_calls(
                research_entry,
                max_queries=len(research_requests),
            )
            resumable_researcher = _uses_resumable_exa_researcher(researcher)
            if completed_queries and not resumable_researcher:
                return _pause(
                    checkpoint,
                    paths.checkpoint,
                    status="failed",
                    reason="researcher_cannot_resume_partial_research",
                    company_id=company.company_id,
                    stage="research",
                )
            next_reservation = _exa_search_reservation(
                research_requests[completed_queries].max_results
            ) if completed_queries < len(research_requests) else 0.0
            if not lifecycle.admit(
                research_op,
                operation="company_research",
                policy=exa_admission,
                reservation_usd=next_reservation,
                company_id=company.company_id,
                fields={"successful_calls": completed_queries},
                pending_stage="research",
            ):
                return checkpoint
            progress_supported = _supports_research_progress(researcher)
            cumulative_items = [deepcopy(item) for item in company.evidence]
            research_result_caps = tuple(item.max_results for item in research_requests)

            def persist_progress(
                delta: EvidenceBundle,
                operation_key: str = research_op,
                query_count: int = len(research_requests),
                budget_checked: bool = resumable_researcher,
                result_caps: tuple[int, ...] = research_result_caps,
            ) -> None:
                """Persist one Exa result, reserve the next, and stop before over-budget work."""
                nonlocal company, cumulative_items
                if delta.company_id != company.company_id:
                    raise ValueError("research progress bundle company_id mismatch")
                for event in delta.usage_events:
                    lifecycle.record_usage(event)
                _append_research_raw(paths, delta.raw_records)
                cumulative_items.extend(deepcopy(delta.items))
                cumulative = build_evidence_bundle(
                    company=company,
                    items=cumulative_items,
                    raw_records=[],
                    usage_events=[],
                )
                company = _persist_research_snapshot(
                    paths,
                    company,
                    cumulative,
                    completed=False,
                )
                cumulative_items = [deepcopy(item) for item in cumulative.items]
                entry = lifecycle.operations()[operation_key]
                calls = _research_successful_calls(entry, max_queries=query_count) + 1
                if calls > query_count:
                    raise ValueError("research progress exceeded the bounded query count")
                has_next_query = calls < query_count
                next_cost = (
                    _exa_search_reservation(result_caps[calls])
                    if has_next_query
                    else 0.0
                )
                if budget_checked and has_next_query:
                    if not lifecycle.reserve_continuation(
                        operation_key,
                        policy=exa_admission,
                        reservation_usd=next_cost,
                        fields={"successful_calls": calls},
                        company_id=company.company_id,
                        stage="research",
                    ):
                        raise _ResearchBudgetPause
                else:
                    lifecycle.update_operation(
                        operation_key,
                        fields={
                            "successful_calls": calls,
                            "reservation_usd": next_cost,
                        },
                    )

            try:
                if progress_supported and resumable_researcher:
                    bundle = researcher._research_from(
                        company,
                        start_index=completed_queries,
                        on_progress=persist_progress,
                    )
                elif progress_supported:
                    bundle = researcher.research(company, on_progress=persist_progress)
                else:
                    bundle = researcher.research(company)
            except _ResearchBudgetPause:
                return checkpoint
            except DiscoveryProviderError as exc:
                return _handle_required_paid_error(
                    exc=exc,
                    checkpoint=checkpoint,
                    paths=paths,
                    lifecycle=lifecycle,
                    operation_id=research_op,
                    company_id=company.company_id,
                    stage="research",
                )
            if not progress_supported:
                for event in bundle.usage_events:
                    lifecycle.record_usage(event)
                _append_research_raw(paths, bundle.raw_records)
            else:
                bundle = build_evidence_bundle(
                    company=company,
                    items=cumulative_items,
                    raw_records=[],
                    usage_events=[],
                )
            company = _persist_research_snapshot(
                paths,
                company,
                bundle,
                completed=True,
            )
            lifecycle.finish(research_op)
        else:
            bundle = EvidenceBundle(
                company_id=company.company_id,
                items=company.evidence,
                raw_records=[],
                usage_events=[],
            )

        if not bundle.items:
            checkpoint.status = "completed"
            checkpoint.pause_reason = "empty_evidence"
            checkpoint.pending_company_id = company.company_id
            checkpoint.pending_stage = "extraction"
            _persist_checkpoint(paths.checkpoint, checkpoint)
            return checkpoint

        extraction_op = f"extraction:{company.company_id}"
        extraction_entry = _operations(checkpoint).get(extraction_op)
        if extraction_entry is not None and extraction_entry.get("state") == "in_flight":
            lifecycle.pause(
                status="paused_unknown",
                reason=f"unknown_in_flight:{extraction_op}",
                company_id=company.company_id,
                stage="structured_extraction",
            )
            return checkpoint
        reservation = _deepseek_reservation(extractor, company, bundle)
        if not lifecycle.admit(
            extraction_op,
            operation="structured_extraction",
            policy=deepseek_admission,
            reservation_usd=reservation,
            company_id=company.company_id,
            pending_stage="extraction",
        ):
            return checkpoint
        try:
            result = extractor.extract(company, bundle)
        except DiscoveryProviderError as exc:
            return _handle_required_paid_error(
                exc=exc,
                checkpoint=checkpoint,
                paths=paths,
                lifecycle=lifecycle,
                operation_id=extraction_op,
                company_id=company.company_id,
                stage="extraction",
            )
        lifecycle.record_usage(result.usage_event)
        company = apply_extraction(company, bundle, result)
        append_company_snapshot(paths.companies_extracted, company)
        lifecycle.finish(extraction_op)
        completed_count += 1
        if completed_count >= config.max_extracted:
            break
    return None


def run_m2_batch(
    config: M2BatchConfig,
    *,
    discovery: Mapping[str, DiscoveryProvider],
    researcher: ExaEvidenceResearcher,
    extractor: DeepSeekExtractor,
) -> RunCheckpoint:
    """Execute or resume one injected M2 batch without reading credentials or pooling budgets."""
    paths = _validate_config(config)
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
    if checkpoint.status == "completed":
        _replay_usage(paths)
        return checkpoint
    tracker = _replay_usage(paths)
    lifecycle = PaidOperationLifecycle(
        checkpoint=checkpoint,
        tracker=tracker,
        usage_path=paths.usage_events,
        persist_checkpoint=lambda: _persist_checkpoint(paths.checkpoint, checkpoint),
        publish_usage=lambda: write_json_atomic(
            paths.usage, cast(dict[str, Any], tracker.summary())
        ),
    )
    _persist_checkpoint(paths.checkpoint, checkpoint)

    if lifecycle.freeze_if_unknown(
        replayable=lambda _operation_id, entry: entry.get("provider") == "apify"
        and isinstance(entry.get("run_id"), str),
    ) is not None:
        return checkpoint

    if _stages(checkpoint).get("discovery") != "completed":
        paused = _discovery_phase(config, paths, checkpoint, discovery, lifecycle)
        if paused is not None:
            return paused

    companies = _dedup_phase(paths, checkpoint)
    paused = _research_and_extract_phase(
        config,
        paths,
        checkpoint,
        companies,
        researcher,
        extractor,
        lifecycle,
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


class _ApifyRunRecorder:
    """Persist an Actor run ID into the already in-flight Apify operation at composition time."""

    def __init__(self, checkpoint_path: Path) -> None:
        """Remember only the durable checkpoint path, never provider credentials."""
        self._checkpoint_path = checkpoint_path

    def __call__(self, run_id: str) -> None:
        """Persist the exact new run ID before the adapter's first poll begins."""
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


def _parser() -> argparse.ArgumentParser:
    """Build the narrow M2 command parser with explicit paid-execution controls."""
    parser = argparse.ArgumentParser(prog="python -m leads_discovery.pipeline.m2_batch")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--max-candidates", type=int, default=100)
    parser.add_argument("--max-extracted", type=int, default=20)
    parser.add_argument("--include-apify", action="store_true")
    parser.add_argument("--apify-budget-usd", type=float, default=0.25)
    parser.add_argument("--deepseek-budget-usd", type=float, required=True)
    parser.add_argument("--exa-budget-usd", type=float)
    parser.add_argument("--execute-live", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Read credentials only at the CLI boundary and run the narrow explicitly live M2 batch."""
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
        execute_live=args.execute_live,
    )
    paths = _validate_config(config)
    if not config.execute_live:
        return 0

    exa_key = os.environ.get("EXA_API_KEY", "")
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    apify_token = os.environ.get("APIFY_TOKEN", "")
    if not exa_key or not deepseek_key:
        return 2
    if config.include_apify and not apify_token:
        config = replace(config, include_apify=False)

    with httpx.Client() as client:
        discovery: dict[str, DiscoveryProvider] = {
            "exa": ExaDiscoveryProvider(api_key=exa_key, client=client),
        }
        if config.include_apify and apify_token:
            discovery["apify"] = ApifyDiscoveryProvider(
                api_token=apify_token,
                client=client,
                on_run_started=_ApifyRunRecorder(paths.checkpoint),
            )
        researcher = ExaEvidenceResearcher(api_key=exa_key, client=client)
        extractor = DeepSeekExtractor(
            api_key=deepseek_key,
            client=client,
            model=_DEEPSEEK_MODEL,
            prices=_DEFAULT_PRICES,
        )
        checkpoint = run_m2_batch(
            config,
            discovery=discovery,
            researcher=researcher,
            extractor=extractor,
        )
    return 0 if checkpoint.status in {"completed", "paused_budget", "paused_unknown"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
