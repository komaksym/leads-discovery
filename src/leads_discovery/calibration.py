"""Report-only local calibration against manual A/B/C lead labels."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Final, Literal

from leads_discovery.models import CompanyRecord
from leads_discovery.pipeline.evaluation import (
    _CSV_COLUMNS,
    EvaluationConfig,
    _csv_row,
    _load_evaluated_records,
    _rank_records,
    _resolve_paths,
    _safe_csv_text,
)
from leads_discovery.pipeline.state import read_json, write_json_atomic, write_text_atomic

ManualLabel = Literal["A", "B", "C"]
_LABELS: Final[tuple[ManualLabel, ...]] = ("A", "B", "C")
_DECISIONS: Final[tuple[str, ...]] = ("accepted", "rejected", "uncertain")
_FORMULA_PREFIXES: Final[frozenset[str]] = frozenset("=+-@")


@dataclass(frozen=True, slots=True)
class CalibrationSummary:
    """Summarize one local manual-label comparison."""

    run_id: str
    policy_version: str
    evaluated_count: int
    labeled_count: int
    unlabeled_count: int
    critical_disagreement_count: int
    report_path: Path
    joined_csv_path: Path


@dataclass(frozen=True, slots=True)
class _ManualEntry:
    """Hold one fully validated manual label row."""

    label: ManualLabel
    notes: str


def _load_labels(path: Path, known_ids: set[str]) -> dict[str, _ManualEntry]:
    """Validate the entire manual CSV before any calibration output is mutated."""
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ValueError("labels file must not be a symlink")
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise ValueError("labels file does not exist")
    try:
        with resolved.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle, strict=True))
    except (OSError, csv.Error, UnicodeError) as exc:
        raise ValueError("labels file is not a valid UTF-8 CSV") from exc
    if not rows:
        raise ValueError("labels CSV must contain a header")
    headers = rows[0]
    if len(headers) != len(set(headers)):
        raise ValueError("labels CSV contains duplicate header names")
    if "company_id" not in headers or "manual_label" not in headers:
        raise ValueError("labels CSV requires company_id and manual_label columns")
    id_index = headers.index("company_id")
    label_index = headers.index("manual_label")
    notes_index = headers.index("manual_notes") if "manual_notes" in headers else None

    entries: dict[str, _ManualEntry] = {}
    for row_number, row in enumerate(rows[1:], start=2):
        if len(row) != len(headers):
            raise ValueError(
                f"labels CSV row {row_number} has the wrong number of columns"
            )
        company_id = row[id_index].strip()
        raw_label = row[label_index].strip()
        notes = "" if notes_index is None else row[notes_index].strip()
        if not company_id:
            raise ValueError(f"labels CSV row {row_number} has a blank company_id")
        if company_id[0] in _FORMULA_PREFIXES:
            raise ValueError("formula-like company IDs are not allowed")
        if company_id in entries:
            raise ValueError(
                f"labels CSV contains duplicate company_id {company_id!r}"
            )
        if company_id not in known_ids:
            raise ValueError(f"labels CSV contains unknown company_id {company_id!r}")
        if not raw_label:
            raise ValueError(
                f"labels CSV row {row_number} has a blank manual_label"
            )
        label = raw_label.upper()
        if label not in _LABELS:
            raise ValueError("manual_label must be exactly A, B, or C")
        entries[company_id] = _ManualEntry(label, notes)
    return entries


def _policy_version(records: tuple[CompanyRecord, ...], run_summary: Path) -> str:
    """Resolve one policy version from evaluated records and the run summary."""
    persisted = read_json(run_summary)
    summary_version: str | None = None
    if persisted is not None:
        value = persisted.get("policy_version")
        if value is not None and (
            not isinstance(value, str) or not value.strip()
        ):
            raise ValueError("run summary policy_version is invalid")
        summary_version = value
    record_version = records[0].evaluation_policy_version if records else None
    if (
        record_version is not None
        and summary_version is not None
        and record_version != summary_version
    ):
        raise ValueError("evaluated artifacts contain conflicting policy versions")
    version = record_version or summary_version
    if version is None:
        raise ValueError("calibration cannot resolve an evaluation policy version")
    return version


def _metric_summary(
    values: list[float],
    *,
    digits: int,
) -> dict[str, float | int] | None:
    """Summarize one finite persisted score or coverage series for a manual label."""
    if not values:
        return None
    return {
        "count": len(values),
        "min": round(min(values), digits),
        "max": round(max(values), digits),
        "mean": round(fmean(values), digits),
    }


def _label_summaries(
    records: tuple[CompanyRecord, ...],
    labels: dict[str, _ManualEntry],
) -> dict[str, dict[str, object]]:
    """Compute score and coverage summaries for every represented manual label."""
    grouped: dict[ManualLabel, list[CompanyRecord]] = {
        label: [] for label in _LABELS
    }
    for company in records:
        entry = labels.get(company.company_id)
        if entry is not None:
            grouped[entry.label].append(company)
    result: dict[str, dict[str, object]] = {}
    for label in _LABELS:
        companies = grouped[label]
        if not companies:
            continue
        scores: list[float] = [
            company.final_score
            for company in companies
            if company.final_score is not None
        ]
        coverage = [company.coverage["overall"] for company in companies]
        result[label] = {
            "labeled_count": len(companies),
            "final_score": _metric_summary(scores, digits=2),
            "overall_coverage": _metric_summary(coverage, digits=4),
        }
    return result


def _matrix(
    records: tuple[CompanyRecord, ...],
    labels: dict[str, _ManualEntry],
) -> dict[str, dict[str, int]]:
    """Build the complete A/B/C by accepted/rejected/uncertain count matrix."""
    matrix: dict[str, dict[str, int]] = {
        label: {decision: 0 for decision in _DECISIONS}
        for label in _LABELS
    }
    for company in records:
        entry = labels.get(company.company_id)
        if entry is None:
            continue
        decision = company.final_decision
        if decision not in _DECISIONS:
            raise ValueError("evaluated company has an invalid final decision")
        matrix[entry.label][decision] += 1
    return matrix


def _disagreements(
    records: tuple[CompanyRecord, ...],
    labels: dict[str, _ManualEntry],
) -> tuple[list[str], list[str]]:
    """Return deterministic critical and review disagreement company-ID lists."""
    critical: list[str] = []
    review: list[str] = []
    for company in records:
        entry = labels.get(company.company_id)
        if entry is None:
            continue
        pair = (entry.label, company.final_decision)
        if pair in {("A", "rejected"), ("C", "accepted")}:
            critical.append(company.company_id)
        elif pair in {
            ("A", "uncertain"),
            ("B", "accepted"),
            ("B", "rejected"),
            ("C", "uncertain"),
        }:
            review.append(company.company_id)
    return sorted(critical), sorted(review)


def _render_joined_csv(
    records: tuple[CompanyRecord, ...],
    labels: dict[str, _ManualEntry],
) -> str:
    """Regenerate ranked context and join only validated manual label/notes values."""
    columns = _CSV_COLUMNS + ("manual_label", "manual_notes")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for company in _rank_records(records):
        row = _csv_row(company)
        entry = labels.get(company.company_id)
        row["manual_label"] = "" if entry is None else entry.label
        row["manual_notes"] = (
            "" if entry is None else _safe_csv_text(entry.notes)
        )
        writer.writerow(row)
    return stream.getvalue()


def calibrate_run(
    config: EvaluationConfig,
    *,
    labels_path: Path,
) -> CalibrationSummary:
    """Compare labels with decisions without mutating policy, state, or M2 artifacts."""
    paths = _resolve_paths(config)
    records = _load_evaluated_records(paths.evaluated)
    known_ids = {company.company_id for company in records}
    labels = _load_labels(labels_path, known_ids)
    version = _policy_version(records, paths.run_summary)
    critical, review = _disagreements(records, labels)
    report = {
        "run_id": config.run_id,
        "policy_version": version,
        "evaluated_count": len(records),
        "labeled_count": len(labels),
        "unlabeled_count": len(records) - len(labels),
        "decision_matrix": _matrix(records, labels),
        "manual_label_summaries": _label_summaries(records, labels),
        "critical_disagreement_ids": critical,
        "review_disagreement_ids": review,
    }
    joined = _render_joined_csv(records, labels)
    report_path = paths.run_dir / "calibration_report.json"
    joined_path = paths.run_dir / "companies_calibrated.csv"
    write_json_atomic(report_path, report)
    write_text_atomic(joined_path, joined)
    return CalibrationSummary(
        run_id=config.run_id,
        policy_version=version,
        evaluated_count=len(records),
        labeled_count=len(labels),
        unlabeled_count=len(records) - len(labels),
        critical_disagreement_count=len(critical),
        report_path=Path(report_path),
        joined_csv_path=Path(joined_path),
    )
