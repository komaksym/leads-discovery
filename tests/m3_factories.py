"""Strict M3 contract fixtures built only from frozen public models."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, TypeAlias

from leads_discovery.models import CompanyRecord, EvidenceItem, FactValue, RunCheckpoint

FactInput: TypeAlias = tuple[FactValue, float]

_FIXED_TIME = "2026-08-23T12:00:00+00:00"


def accepted_facts() -> dict[str, FactInput]:
    """Return a fully covered fact set that cleanly passes every M3 acceptance gate."""
    return {
        "pvf_relevant": (True, 0.90),
        "rfq_or_quote_workflow_evidence": (True, 0.90),
        "inside_sales_or_estimating_presence": (True, 0.90),
        "project_or_tender_business": (True, 0.90),
        "bom_or_line_item_complexity": (True, 0.90),
        "manufacturer_count_or_breadth": (20, 0.90),
        "branch_count": (3, 0.90),
        "relevant_hiring": (True, 0.90),
        "industrial_or_process_customer_focus": (True, 0.90),
        "employee_count": (50, 0.90),
        "multi_location_signal": (True, 0.90),
        "regional_independent_signal": (True, 0.90),
        "revenue_if_reliably_available": (10_000_000.0, 0.90),
        "known_current_direct_competitor_customer": (False, 0.90),
        "known_quote_automation_or_order_automation_relationship": (False, 0.90),
        "direct_quotation_pain_evidence": (True, 0.90),
        "manual_workflow_evidence": (True, 0.90),
        "explicit_process_bottleneck_evidence": (True, 0.90),
    }


def exact_threshold_facts() -> dict[str, FactInput]:
    """Return fully covered facts whose unrounded default final score is exactly 70."""
    return {
        "pvf_relevant": (True, 0.90),
        "rfq_or_quote_workflow_evidence": (False, 0.90),
        "inside_sales_or_estimating_presence": (False, 0.90),
        "project_or_tender_business": (True, 0.90),
        "bom_or_line_item_complexity": (True, 0.90),
        "manufacturer_count_or_breadth": (0, 0.90),
        "branch_count": (1, 0.90),
        "relevant_hiring": (True, 0.90),
        "industrial_or_process_customer_focus": (True, 0.90),
        "employee_count": (151, 0.90),
        "multi_location_signal": (True, 0.90),
        "regional_independent_signal": (True, 0.90),
        "revenue_if_reliably_available": (1_000_000.0, 0.90),
        "known_current_direct_competitor_customer": (False, 0.90),
        "known_quote_automation_or_order_automation_relationship": (False, 0.90),
        "direct_quotation_pain_evidence": (True, 0.90),
        "manual_workflow_evidence": (True, 0.90),
        "explicit_process_bottleneck_evidence": (True, 0.90),
    }


def low_score_facts() -> dict[str, FactInput]:
    """Return fully covered facts with no hard rejection but a score below 70."""
    return {
        "pvf_relevant": (True, 0.90),
        "rfq_or_quote_workflow_evidence": (False, 0.90),
        "inside_sales_or_estimating_presence": (False, 0.90),
        "project_or_tender_business": (False, 0.90),
        "bom_or_line_item_complexity": (False, 0.90),
        "manufacturer_count_or_breadth": (0, 0.90),
        "branch_count": (3, 0.90),
        "relevant_hiring": (False, 0.90),
        "industrial_or_process_customer_focus": (False, 0.90),
        "employee_count": (10, 0.90),
        "multi_location_signal": (False, 0.90),
        "regional_independent_signal": (False, 0.90),
        "revenue_if_reliably_available": (500_000.0, 0.90),
        "known_current_direct_competitor_customer": (False, 0.90),
        "known_quote_automation_or_order_automation_relationship": (False, 0.90),
        "direct_quotation_pain_evidence": (False, 0.90),
        "manual_workflow_evidence": (False, 0.90),
        "explicit_process_bottleneck_evidence": (False, 0.90),
    }


def build_company(
    *,
    facts: Mapping[str, FactInput],
    company_id: str = "cmp_contract",
    name: str = "Contract Valve",
    domain: str = "contractvalve.com",
    country: str | None = "US",
    status: str = "active",
    discovery_country_code: str | None = "US",
    extraction_completed: bool = True,
) -> CompanyRecord:
    """Build one extracted company whose non-null facts cite unique retained evidence."""
    evidence: list[EvidenceItem] = []
    features: dict[str, FactValue] = {}
    confidence: dict[str, object] = {}
    for index, (key, (value, score)) in enumerate(sorted(facts.items()), start=1):
        evidence_id = f"ev_{index:024d}"
        ids = [] if value is None else [evidence_id]
        if value is not None:
            evidence.append(
                EvidenceItem(
                    evidence_id=evidence_id,
                    url=f"https://{domain}/evidence/{index}",
                    title=f"Evidence {index}",
                    excerpt=f"Public evidence for {key}.",
                    source_type="web",
                    provider="exa",
                    retrieved_at=_FIXED_TIME,
                )
            )
        features[key] = deepcopy(value)
        confidence[key] = {"confidence": score, "evidence_ids": ids}
    stages = {
        "research": "completed",
        "extraction": "completed" if extraction_completed else "pending",
    }
    discovery_records: list[dict[str, Any]] = []
    if discovery_country_code is not None:
        discovery_records.append(
            {
                "provider": "exa",
                "country_code": discovery_country_code,
                "source_url": f"https://{domain}/directory",
            }
        )
    return CompanyRecord(
        company_id=company_id,
        name=name,
        normalized_name=name.casefold().strip(),
        domain=domain,
        normalized_domain=domain.casefold(),
        country=country,
        status=status,
        discovery_sources=["exa"],
        discovery_queries=["industrial valve distributor"],
        discovery_records=discovery_records,
        evidence=evidence,
        features=features,
        feature_confidence=confidence,
        stage_status=stages,
        created_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    )


def write_jsonl(path: Path, payloads: Iterable[dict[str, Any]]) -> None:
    """Write strict UTF-8 JSONL without allowing non-finite JSON numbers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
        for payload in payloads
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write one strict deterministic JSON object for an M2 fixture artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        payload,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )
    path.write_text(text + "\n", encoding="utf-8", newline="\n")


def write_run_inputs(
    data_root: Path,
    run_id: str,
    companies: Iterable[CompanyRecord],
    *,
    checkpoint_status: str = "completed",
    pending_company_id: str | None = None,
    pending_stage: str | None = None,
    pause_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> Path:
    """Persist coherent M2 extraction/checkpoint/usage fixtures for one M3 run."""
    run_dir = data_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(
        run_dir / "companies_extracted.jsonl",
        (company.to_dict() for company in companies),
    )
    checkpoint = RunCheckpoint(
        run_id=run_id,
        status=checkpoint_status,
        pending_company_id=pending_company_id,
        pending_stage=pending_stage,
        pause_reason=pause_reason,
        provider_state={"operations": {}, "stages": {"m2_batch": checkpoint_status}},
        updated_at=_FIXED_TIME,
    )
    write_json(run_dir / "checkpoint.json", checkpoint.to_dict())
    if usage is None:
        usage = {
            "providers": {},
            "total": {
                "request_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_usd": None,
                "exact_cost_usd": None,
            },
        }
    write_json(run_dir / "usage.json", usage)
    (run_dir / "usage_events.jsonl").write_text("", encoding="utf-8", newline="\n")
    return run_dir


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read strict JSONL fixture output into dictionaries."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
