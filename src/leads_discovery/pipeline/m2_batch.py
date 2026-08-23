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
from leads_discovery.pipeline.state import (
    append_company_snapshot,
    append_jsonl,
    append_usage_event,
    load_checkpoint,
    load_jsonl,
    load_latest_company_records,
    load_usage_events,
    write_checkpoint,
    write_json_atomic,
)
from leads_discovery.research import (
    DeepSeekExtractor,
    DeepSeekPriceSchedule,
    ExaEvidenceResearcher,
    apply_extraction,
    build_evidence_bundle,
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


def _persist_checkpoint(path: Path, checkpoint: RunCheckpoint) -> None:
    """Refresh the checkpoint timestamp and atomically persist all safe operation state."""
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
) -> None:
    """Durably mark one paid operation in flight before any provider call occurs."""
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
    """Atomically record a known operation outcome after usage and output are durable."""
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
    """Persist and return a durable pause/failure checkpoint without discarding completed work."""
    checkpoint.status = status
    checkpoint.pause_reason = reason
    checkpoint.pending_company_id = company_id
    checkpoint.pending_stage = stage
    _persist_checkpoint(path, checkpoint)
    return checkpoint


def _record_usage(paths: _RunPaths, tracker: CostTracker, event: UsageEvent) -> None:
    """Append/fsync one usage event and rebuild the atomic usage summary from replayed state."""
    append_usage_event(paths.usage_events, event)
    tracker.record(event)
    write_json_atomic(paths.usage, cast(dict[str, Any], tracker.summary()))


def _replay_usage(paths: _RunPaths) -> CostTracker:
    """Rebuild provider budgets from the append-only usage ledger on every invocation."""
    tracker = CostTracker(load_usage_events(paths.usage_events))
    write_json_atomic(paths.usage, cast(dict[str, Any], tracker.summary()))
    return tracker


def _provider_budget_allows(
    tracker: CostTracker,
    provider: str,
    ceiling: float | None,
    reservation: float = 0.0,
) -> bool:
    """Check one provider budget independently, failing closed when prior spend is unknown."""
    if ceiling is None:
        return True
    spend = tracker.provider_estimated_spend(provider)
    if spend is None:
        return False
    if reservation > 0:
        return spend + reservation <= ceiling + 1e-12
    return spend < ceiling - 1e-12


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


def _unknown_in_flight(checkpoint: RunCheckpoint) -> tuple[str, dict[str, Any]] | None:
    """Find the first provider operation whose previous paid outcome must not be repeated."""
    for operation_id, entry in sorted(_operations(checkpoint).items()):
        if entry.get("state") != "in_flight":
            continue
        provider = entry.get("provider")
        if provider in {"exa", "deepseek"}:
            return operation_id, entry
        if provider == "apify" and not isinstance(entry.get("run_id"), str):
            return operation_id, entry
    return None


def _resume_apify_if_needed(
    checkpoint: RunCheckpoint,
    paths: _RunPaths,
    requests: Sequence[DiscoveryRequest],
    provider: DiscoveryProvider,
    tracker: CostTracker,
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
        if not isinstance(resumed, DiscoveryBatch):
            return _pause(
                checkpoint,
                paths.checkpoint,
                status="failed",
                reason="apify_resume_returned_invalid_batch",
                stage="discovery",
            )
        for event in resumed.usage_events:
            _record_usage(paths, tracker, event)
        _append_discovery_batch(paths, resumed.records)
        _finish_operation(checkpoint, paths.checkpoint, operation_id)
    return None


def _discovery_phase(
    config: M2BatchConfig,
    paths: _RunPaths,
    checkpoint: RunCheckpoint,
    discovery: Mapping[str, DiscoveryProvider],
    tracker: CostTracker,
) -> RunCheckpoint | None:
    """Run or resume the bounded discovery plan while treating Apify as optional."""
    requests = build_discovery_requests(
        include_apify=config.include_apify and discovery.get("apify") is not None,
        max_candidates=config.max_candidates,
        apify_budget_usd=config.apify_budget_usd,
    )
    apify = discovery.get("apify")
    if apify is not None:
        paused = _resume_apify_if_needed(checkpoint, paths, requests, apify, tracker)
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
        if request.provider == "exa" and not _provider_budget_allows(
            tracker,
            "exa",
            config.exa_budget_usd,
        ):
            return _pause(
                checkpoint,
                paths.checkpoint,
                status="paused_budget",
                reason="exa_budget_exhausted_or_unknown",
                stage="discovery",
            )
        operation = "company_search" if request.provider == "exa" else "google_maps_search"
        _mark_in_flight(
            checkpoint,
            paths.checkpoint,
            operation_id=operation_id,
            provider=request.provider,
            operation=operation,
            request_id=request.request_id,
        )
        try:
            batch = provider.search(request)
        except DiscoveryProviderError as exc:
            _record_usage(paths, tracker, exc.usage_event)
            if request.provider == "apify":
                apify_entry = _operations(checkpoint)[operation_id]
                run_id = _entry_str(apify_entry, "run_id")
                if exc.retryable and run_id is not None:
                    apify_entry["state"] = "pending"
                    apify_entry["error_kind"] = exc.kind
                    checkpoint.pending_company_id = None
                    checkpoint.pending_stage = None
                    _persist_checkpoint(paths.checkpoint, checkpoint)
                    continue
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
                continue
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
                    reason="exa_budget_exhausted",
                    stage="discovery",
                )
            if exc.kind in {
                "authentication",
                "invalid_request",
                "invalid_response",
                "permanent",
            }:
                return _pause(
                    checkpoint,
                    paths.checkpoint,
                    status="failed",
                    reason=f"exa_{exc.kind}",
                    stage="discovery",
                )
            return _pause(
                checkpoint,
                paths.checkpoint,
                status="paused_retryable",
                reason=f"exa_{exc.kind}",
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

    pending_apify = [
        operation_id
        for operation_id, entry in sorted(_operations(checkpoint).items())
        if entry.get("provider") == "apify"
        and entry.get("state") == "pending"
        and isinstance(entry.get("run_id"), str)
    ]
    if pending_apify:
        return _pause(
            checkpoint,
            paths.checkpoint,
            status="paused_retryable",
            reason=f"apify_pending:{pending_apify[0]}",
            stage="discovery",
        )
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


def _handle_research_error(
    *,
    exc: DiscoveryProviderError,
    checkpoint: RunCheckpoint,
    paths: _RunPaths,
    tracker: CostTracker,
    operation_id: str,
    company_id: str,
) -> RunCheckpoint:
    """Persist a research failure once and protect already-successful calls from replay."""
    _record_usage(paths, tracker, exc.usage_event)
    entry = _operations(checkpoint)[operation_id]
    successful_calls = entry.get("successful_calls", 0)
    has_progress = (
        isinstance(successful_calls, int)
        and not isinstance(successful_calls, bool)
        and successful_calls > 0
    )
    if has_progress:
        entry["error_kind"] = exc.kind
        _persist_checkpoint(paths.checkpoint, checkpoint)
        return _pause(
            checkpoint,
            paths.checkpoint,
            status="paused_unknown",
            reason="partial_exa_research_not_replayable",
            company_id=company_id,
            stage="company_research",
        )
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
            reason="exa_budget_exhausted",
            company_id=company_id,
            stage="research",
        )
    if exc.kind in {"authentication", "invalid_request", "invalid_response", "permanent"}:
        return _pause(
            checkpoint,
            paths.checkpoint,
            status="failed",
            reason=f"exa_{exc.kind}",
            company_id=company_id,
            stage="research",
        )
    return _pause(
        checkpoint,
        paths.checkpoint,
        status="paused_retryable",
        reason=f"exa_{exc.kind}",
        company_id=company_id,
        stage="research",
    )


def _research_and_extract_phase(
    config: M2BatchConfig,
    paths: _RunPaths,
    checkpoint: RunCheckpoint,
    companies: Sequence[CompanyRecord],
    researcher: ExaEvidenceResearcher,
    extractor: DeepSeekExtractor,
    tracker: CostTracker,
) -> RunCheckpoint | None:
    """Research and extract selected companies in order under independent budgets."""
    selected = select_research_companies(companies, limit=config.max_extracted)
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
            return _pause(
                checkpoint,
                paths.checkpoint,
                status="paused_unknown",
                reason=f"unknown_in_flight:{research_op}",
                company_id=company.company_id,
                stage="company_research",
            )

        if company.stage_status.get("research") != "completed":
            if not _provider_budget_allows(tracker, "exa", config.exa_budget_usd):
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
                operation_id=research_op,
                provider="exa",
                operation="company_research",
                company_id=company.company_id,
            )
            progress_supported = _supports_research_progress(researcher)
            cumulative_items = [deepcopy(item) for item in company.evidence]

            def persist_progress(
                delta: EvidenceBundle,
                operation_key: str = research_op,
            ) -> None:
                """Fsync one Exa call's usage, raw rows, snapshot, and progress state."""
                nonlocal company, cumulative_items
                if delta.company_id != company.company_id:
                    raise ValueError("research progress bundle company_id mismatch")
                for event in delta.usage_events:
                    _record_usage(paths, tracker, event)
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
                entry = _operations(checkpoint)[operation_key]
                calls = entry.get("successful_calls", 0)
                if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
                    raise ValueError("research successful_calls checkpoint value is invalid")
                entry["successful_calls"] = calls + 1
                _persist_checkpoint(paths.checkpoint, checkpoint)

            try:
                if progress_supported:
                    bundle = researcher.research(company, on_progress=persist_progress)
                else:
                    bundle = researcher.research(company)
            except DiscoveryProviderError as exc:
                return _handle_research_error(
                    exc=exc,
                    checkpoint=checkpoint,
                    paths=paths,
                    tracker=tracker,
                    operation_id=research_op,
                    company_id=company.company_id,
                )
            if not progress_supported:
                for event in bundle.usage_events:
                    _record_usage(paths, tracker, event)
                _append_research_raw(paths, bundle.raw_records)
            company = _persist_research_snapshot(
                paths,
                company,
                bundle,
                completed=True,
            )
            _finish_operation(checkpoint, paths.checkpoint, research_op)
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
            return _pause(
                checkpoint,
                paths.checkpoint,
                status="paused_unknown",
                reason=f"unknown_in_flight:{extraction_op}",
                company_id=company.company_id,
                stage="structured_extraction",
            )
        reservation = _deepseek_reservation(extractor, company, bundle)
        if not _provider_budget_allows(
            tracker,
            "deepseek",
            config.deepseek_budget_usd,
            reservation,
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
            operation_id=extraction_op,
            provider="deepseek",
            operation="structured_extraction",
            company_id=company.company_id,
            reservation_usd=reservation,
        )
        try:
            result = extractor.extract(company, bundle)
        except DiscoveryProviderError as exc:
            _record_usage(paths, tracker, exc.usage_event)
            state = "pending" if exc.retryable or exc.kind == "budget_exhausted" else "failed"
            _finish_operation(
                checkpoint,
                paths.checkpoint,
                extraction_op,
                state=state,
                error_kind=exc.kind,
            )
            if exc.kind == "budget_exhausted":
                return _pause(
                    checkpoint,
                    paths.checkpoint,
                    status="paused_budget",
                    reason="deepseek_budget_exhausted",
                    company_id=company.company_id,
                    stage="extraction",
                )
            if exc.kind in {
                "authentication",
                "invalid_request",
                "invalid_response",
                "permanent",
            }:
                return _pause(
                    checkpoint,
                    paths.checkpoint,
                    status="failed",
                    reason=f"deepseek_{exc.kind}",
                    company_id=company.company_id,
                    stage="extraction",
                )
            return _pause(
                checkpoint,
                paths.checkpoint,
                status="paused_retryable",
                reason=f"deepseek_{exc.kind}",
                company_id=company.company_id,
                stage="extraction",
            )
        _record_usage(paths, tracker, result.usage_event)
        company = apply_extraction(company, bundle, result)
        append_company_snapshot(paths.companies_extracted, company)
        _finish_operation(checkpoint, paths.checkpoint, extraction_op)
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

    companies = _dedup_phase(paths, checkpoint)
    paused = _research_and_extract_phase(
        config,
        paths,
        checkpoint,
        companies,
        researcher,
        extractor,
        tracker,
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
