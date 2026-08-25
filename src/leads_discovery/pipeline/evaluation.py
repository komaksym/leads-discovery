"""Local M3 evaluation and deterministic derived-artifact publication."""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from leads_discovery.models import CompanyRecord, RunCheckpoint
from leads_discovery.pipeline.costs import CostTracker
from leads_discovery.pipeline.state import (
    load_checkpoint,
    load_jsonl,
    load_usage_events,
    write_json_atomic,
    write_jsonl_atomic,
    write_text_atomic,
)
from leads_discovery.scoring import DEFAULT_POLICY, ScoringPolicy, evaluate_companies

_RUN_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DECISION_ORDER: Final[dict[str, int]] = {
    "accepted": 0,
    "uncertain": 1,
    "rejected": 2,
}
_FORMULA_PREFIXES: Final[frozenset[str]] = frozenset("=+-@")
CSV_COLUMNS: Final[tuple[str, ...]] = (
    "company_id",
    "name",
    "domain",
    "country",
    "policy_version",
    "workload_score",
    "workload_coverage",
    "economic_fit_score",
    "economic_fit_coverage",
    "low_incumbent_exposure_score",
    "low_incumbent_exposure_coverage",
    "direct_pain_score",
    "direct_pain_coverage",
    "overall_coverage",
    "final_score",
    "final_decision",
    "review_reasons",
    "rejection_reasons",
)
_M3_ARTIFACT_NAMES: Final[tuple[str, ...]] = (
    "companies_evaluated.jsonl",
    "companies_ranked.csv",
    "calibration_template.csv",
    "calibration_report.json",
    "companies_calibrated.csv",
    "run_summary.json",
)


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    """Configure one bounded local M3 evaluation publication."""

    run_id: str
    data_root: Path
    max_evaluated: int = 20


@dataclass(frozen=True, slots=True)
class EvaluationSummary:
    """Summarize one complete local M3 evaluation publication."""

    run_id: str
    policy_version: str
    evaluated_count: int
    accepted_count: int
    rejected_count: int
    uncertain_count: int
    artifact_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class EvaluationPaths:
    """Resolved M2 inputs and M3 outputs for one contained run directory."""

    run_dir: Path
    extracted: Path
    checkpoint: Path
    usage_events: Path
    evaluated: Path
    ranked: Path
    calibration_template: Path
    run_summary: Path

    def output_paths(self) -> tuple[Path, ...]:
        return (
            self.evaluated,
            self.ranked,
            self.calibration_template,
            self.run_summary,
        )


def resolve_evaluation_paths(config: EvaluationConfig) -> EvaluationPaths:
    """Validate evaluation controls and every read/write symlink boundary."""
    if not isinstance(config.run_id, str) or not _RUN_ID.fullmatch(config.run_id):
        raise ValueError("run_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    if (
        isinstance(config.max_evaluated, bool)
        or not isinstance(config.max_evaluated, int)
        or not 1 <= config.max_evaluated <= 20
    ):
        raise ValueError("max_evaluated must be an integer in 1..20")
    root = config.data_root.expanduser().resolve()
    candidate = root / config.run_id
    if candidate.is_symlink():
        raise ValueError("run directory must not be a symlink")
    run_dir = candidate.resolve()
    if run_dir.parent != root:
        raise ValueError("run directory must remain directly beneath data_root")
    if not run_dir.is_dir():
        raise ValueError("run directory does not exist")
    for name in _M3_ARTIFACT_NAMES:
        if (run_dir / name).is_symlink():
            raise ValueError(f"artifact path must not be a symlink: {name}")
    for name in ("companies_extracted.jsonl", "checkpoint.json", "usage_events.jsonl"):
        if (run_dir / name).is_symlink():
            raise ValueError(f"input artifact must not be a symlink: {name}")
    return EvaluationPaths(
        run_dir=run_dir,
        extracted=run_dir / "companies_extracted.jsonl",
        checkpoint=run_dir / "checkpoint.json",
        usage_events=run_dir / "usage_events.jsonl",
        evaluated=run_dir / "companies_evaluated.jsonl",
        ranked=run_dir / "companies_ranked.csv",
        calibration_template=run_dir / "calibration_template.csv",
        run_summary=run_dir / "run_summary.json",
    )


def _validate_company_shape(company: CompanyRecord) -> None:
    if not isinstance(company.company_id, str) or not company.company_id:
        raise ValueError("company_id must be a nonempty string")
    if not isinstance(company.name, str) or not company.name:
        raise ValueError("company name must be a nonempty string")
    if company.country is not None and not isinstance(company.country, str):
        raise ValueError("company country must be a string or null")
    if not isinstance(company.status, str):
        raise ValueError("company status must be a string")
    if not isinstance(company.features, dict):
        raise ValueError("company features must be an object")
    if not isinstance(company.feature_confidence, dict):
        raise ValueError("company feature_confidence must be an object")
    if not isinstance(company.stage_status, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in company.stage_status.items()
    ):
        raise ValueError("company stage_status must be a string map")
    if not isinstance(company.discovery_records, list) or any(
        not isinstance(record, dict) for record in company.discovery_records
    ):
        raise ValueError("company discovery_records must contain objects")
    for name, value in company.coverage.items():
        if (
            not isinstance(name, str)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("persisted coverage must be a finite numeric map")
    for name, value in company.score_components.items():
        if (
            not isinstance(name, str)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("persisted score_components must be a finite numeric map")
    if company.final_score is not None and (
        isinstance(company.final_score, bool)
        or not isinstance(company.final_score, (int, float))
        or not math.isfinite(company.final_score)
    ):
        raise ValueError("persisted final_score must be finite or null")


def _load_latest_extracted(path: Path) -> tuple[CompanyRecord, ...]:
    if not path.exists():
        return ()
    latest: dict[str, CompanyRecord] = {}
    for payload in load_jsonl(path):
        company = CompanyRecord.from_dict(payload)
        _validate_company_shape(company)
        latest[company.company_id] = company
    return tuple(latest[key] for key in sorted(latest))


def safe_csv_text(value: str) -> str:
    """Neutralize spreadsheet formulas while preserving JSON text."""
    stripped = value.lstrip()
    return "'" + value if stripped and stripped[0] in _FORMULA_PREFIXES else value


def _score_cell(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def _coverage_cell(value: float) -> str:
    return f"{value:.4f}"


def csv_row(company: CompanyRecord) -> dict[str, str]:
    """Render one evaluated company into the ranked CSV schema."""
    if company.evaluation_policy_version is None:
        raise ValueError("evaluated company is missing policy version")
    if company.final_decision not in _DECISION_ORDER:
        raise ValueError("evaluated company has an invalid final decision")
    coverage = company.coverage
    required = {
        "workload",
        "economic_fit",
        "low_incumbent_exposure",
        "direct_pain",
        "overall",
    }
    if set(coverage) != required:
        raise ValueError("evaluated company has an invalid coverage schema")
    return {
        "company_id": safe_csv_text(company.company_id),
        "name": safe_csv_text(company.name),
        "domain": safe_csv_text(company.normalized_domain or company.domain or ""),
        "country": safe_csv_text(company.country or ""),
        "policy_version": company.evaluation_policy_version,
        "workload_score": _score_cell(company.score_components.get("workload")),
        "workload_coverage": _coverage_cell(coverage["workload"]),
        "economic_fit_score": _score_cell(company.score_components.get("economic_fit")),
        "economic_fit_coverage": _coverage_cell(coverage["economic_fit"]),
        "low_incumbent_exposure_score": _score_cell(
            company.score_components.get("low_incumbent_exposure")
        ),
        "low_incumbent_exposure_coverage": _coverage_cell(
            coverage["low_incumbent_exposure"]
        ),
        "direct_pain_score": _score_cell(company.score_components.get("direct_pain")),
        "direct_pain_coverage": _coverage_cell(coverage["direct_pain"]),
        "overall_coverage": _coverage_cell(coverage["overall"]),
        "final_score": _score_cell(company.final_score),
        "final_decision": company.final_decision,
        "review_reasons": ";".join(company.review_reasons),
        "rejection_reasons": ";".join(company.rejection_reasons),
    }


def _normalized_sort_name(company: CompanyRecord) -> str:
    return " ".join((company.normalized_name or company.name).split()).casefold()


def rank_records(companies: tuple[CompanyRecord, ...]) -> tuple[CompanyRecord, ...]:
    """Sort by decision, score, coverage, name, and stable company ID."""

    def key(company: CompanyRecord) -> tuple[int, bool, float, float, str, str]:
        if company.final_decision not in _DECISION_ORDER:
            raise ValueError("evaluated company has an invalid final decision")
        score = company.final_score
        return (
            _DECISION_ORDER[company.final_decision],
            score is None,
            0.0 if score is None else -score,
            -company.coverage["overall"],
            _normalized_sort_name(company),
            company.company_id,
        )

    return tuple(sorted(companies, key=key))


def _render_csv(
    companies: tuple[CompanyRecord, ...],
    *,
    include_manual_columns: bool = False,
) -> str:
    manual_columns = ("manual_label", "manual_notes")
    columns = CSV_COLUMNS + (manual_columns if include_manual_columns else ())
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for company in companies:
        row = csv_row(company)
        if include_manual_columns:
            row["manual_label"] = ""
            row["manual_notes"] = ""
        writer.writerow(row)
    return stream.getvalue()


def _artifact_names(paths: EvaluationPaths) -> dict[str, str]:
    return {
        "companies_evaluated": paths.evaluated.name,
        "companies_ranked": paths.ranked.name,
        "calibration_template": paths.calibration_template.name,
        "run_summary": paths.run_summary.name,
    }


def _summary_payload(
    config: EvaluationConfig,
    policy: ScoringPolicy,
    checkpoint: RunCheckpoint,
    companies: tuple[CompanyRecord, ...],
    paths: EvaluationPaths,
) -> dict[str, Any]:
    decisions = [company.final_decision for company in companies]
    usage = CostTracker(load_usage_events(paths.usage_events)).summary()
    return {
        "run_id": config.run_id,
        "policy_version": policy.version,
        "m2_checkpoint": {
            "status": checkpoint.status,
            "pending_company_id": checkpoint.pending_company_id,
            "pending_stage": checkpoint.pending_stage,
            "pause_reason": checkpoint.pause_reason,
        },
        "evaluated_count": len(companies),
        "accepted_count": decisions.count("accepted"),
        "rejected_count": decisions.count("rejected"),
        "uncertain_count": decisions.count("uncertain"),
        "artifacts": _artifact_names(paths),
        "usage": usage,
    }


def load_evaluated_records(path: Path) -> tuple[CompanyRecord, ...]:
    """Strictly load unique complete M3 records for calibration."""
    if path.is_symlink():
        raise ValueError("companies_evaluated.jsonl must not be a symlink")
    if not path.exists():
        raise ValueError("companies_evaluated.jsonl does not exist")
    records: list[CompanyRecord] = []
    ids: set[str] = set()
    versions: set[str] = set()
    for payload in load_jsonl(path):
        company = CompanyRecord.from_dict(payload)
        _validate_company_shape(company)
        if company.company_id in ids:
            raise ValueError("companies_evaluated.jsonl contains duplicate company IDs")
        ids.add(company.company_id)
        if company.stage_status.get("decision") != "completed":
            raise ValueError("evaluated company decision stage is not completed")
        if company.evaluation_policy_version is None:
            raise ValueError("evaluated company is missing policy version")
        versions.add(company.evaluation_policy_version)
        records.append(company)
    if len(versions) > 1:
        raise ValueError("evaluated companies contain conflicting policy versions")
    return tuple(sorted(records, key=lambda company: company.company_id))


def evaluate_run(
    config: EvaluationConfig,
    *,
    policy: ScoringPolicy = DEFAULT_POLICY,
) -> EvaluationSummary:
    """Evaluate completed M2 snapshots and atomically publish local M3 views."""
    paths = resolve_evaluation_paths(config)
    checkpoint = load_checkpoint(paths.checkpoint)
    if checkpoint is None:
        raise ValueError("checkpoint.json does not exist")
    if checkpoint.run_id != config.run_id:
        raise ValueError("checkpoint run_id does not match requested run")

    latest = _load_latest_extracted(paths.extracted)
    evaluated = evaluate_companies(latest, limit=config.max_evaluated, policy=policy)
    if not evaluated and checkpoint.status == "completed":
        raise ValueError("completed M2 checkpoint has no extraction-complete companies")

    by_id = tuple(sorted(evaluated, key=lambda company: company.company_id))
    ranked = rank_records(by_id)
    summary = _summary_payload(config, policy, checkpoint, by_id, paths)

    write_jsonl_atomic(paths.evaluated, [company.to_dict() for company in by_id])
    write_text_atomic(paths.ranked, _render_csv(ranked))
    write_text_atomic(paths.calibration_template, _render_csv(ranked, include_manual_columns=True))
    write_json_atomic(paths.run_summary, summary)

    decisions = [company.final_decision for company in by_id]
    return EvaluationSummary(
        run_id=config.run_id,
        policy_version=policy.version,
        evaluated_count=len(by_id),
        accepted_count=decisions.count("accepted"),
        rejected_count=decisions.count("rejected"),
        uncertain_count=decisions.count("uncertain"),
        artifact_paths=paths.output_paths(),
    )
