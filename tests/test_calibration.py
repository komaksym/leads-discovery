"""Frozen-contract tests for report-only M3 A/B/C calibration."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest
from m3_factories import FactInput, accepted_facts, build_company, low_score_facts, write_run_inputs

from leads_discovery.calibration import CalibrationSummary, calibrate_run
from leads_discovery.pipeline.evaluation import EvaluationConfig, evaluate_run
from leads_discovery.scoring import DEFAULT_POLICY


def _prepare(tmp_path: Path, run_id: str, companies: list[Any]) -> Path:
    """Publish evaluated records that calibration may join."""
    run_dir = write_run_inputs(tmp_path, run_id, companies)
    evaluate_run(EvaluationConfig(run_id=run_id, data_root=tmp_path))
    return run_dir


def _labels(path: Path, rows: list[list[str]], header: list[str] | None = None) -> Path:
    """Write labels with the standard CSV writer unless testing malformed raw bytes."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header or ["company_id", "manual_label", "manual_notes"])
        writer.writerows(rows)
    return path


def _calibrate(tmp_path: Path, run_id: str, labels: Path) -> CalibrationSummary:
    """Run the frozen local calibration API."""
    return calibrate_run(
        EvaluationConfig(run_id=run_id, data_root=tmp_path),
        labels_path=labels,
    )


def _decision_facts(decision: str) -> dict[str, FactInput]:
    """Build a fact set for accepted or uncertain matrix fixtures."""
    if decision == "accepted":
        return accepted_facts()
    facts = accepted_facts()
    facts["pvf_relevant"] = (True, .70)
    return facts


def _find_matrix(value: object) -> dict[str, Any]:
    """Find the unique nested object shaped as the complete A/B/C decision matrix."""
    if isinstance(value, dict):
        if set(value) == {"A", "B", "C"} and all(isinstance(v, dict) for v in value.values()):
            return value
        for child in value.values():
            try:
                return _find_matrix(child)
            except LookupError:
                pass
    raise LookupError("matrix not found")


def _find_id_list(value: object, ids: set[str]) -> list[str]:
    """Find one nested list containing exactly the expected disagreement IDs."""
    if isinstance(value, list) and set(value) == ids:
        return value
    if isinstance(value, dict):
        for child in value.values():
            try:
                return _find_id_list(child, ids)
            except LookupError:
                pass
    if isinstance(value, list):
        for child in value:
            try:
                return _find_id_list(child, ids)
            except LookupError:
                pass
    raise LookupError(f"IDs not found: {ids}")


def _symlink_or_skip(link: Path, target: Path) -> None:
    """Create a symlink or skip where unavailable."""
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")


def test_complete_matrix_and_disagreement_classes(tmp_path: Path) -> None:
    """All nine A/B/C x accepted/rejected/uncertain cells and disagreement sets are reported."""
    companies = []
    rows = []
    for label in "ABC":
        for decision in ("accepted", "rejected", "uncertain"):
            company_id = f"cmp_{label}_{decision}"
            if decision == "rejected":
                company = build_company(
                    facts=accepted_facts(),
                    company_id=company_id,
                    status="inactive",
                )
            else:
                company = build_company(
                    facts=_decision_facts(decision),
                    company_id=company_id,
                )
            companies.append(company)
            rows.append([company_id, label, f"note {company_id}"])
    run_dir = _prepare(tmp_path, "matrix", companies)
    labels = _labels(tmp_path / "labels.csv", rows)

    summary = _calibrate(tmp_path, "matrix", labels)
    report = json.loads((run_dir / "calibration_report.json").read_text(encoding="utf-8"))
    matrix = _find_matrix(report)

    assert summary.evaluated_count == summary.labeled_count == 9
    assert summary.unlabeled_count == 0
    for label in "ABC":
        assert matrix[label] == {"accepted": 1, "rejected": 1, "uncertain": 1}
    critical = {"cmp_A_rejected", "cmp_C_accepted"}
    review = {
        "cmp_A_uncertain",
        "cmp_B_accepted",
        "cmp_B_rejected",
        "cmp_C_uncertain",
    }
    assert _find_id_list(report, critical)
    assert _find_id_list(report, review)
    assert summary.critical_disagreement_count == 2
    assert report["policy_version"] == "m3-v1"


def test_partial_labels_are_trimmed_uppercased_and_notes_sanitized(tmp_path: Path) -> None:
    """Partial labeling is valid; labels normalize and formula-like notes are neutralized."""
    companies = [
        build_company(facts=accepted_facts(), company_id="cmp_a", name="Canonical A"),
        build_company(facts=low_score_facts(), company_id="cmp_b", name="Canonical B"),
    ]
    run_dir = _prepare(tmp_path, "partial", companies)
    labels = _labels(
        tmp_path / "partial.csv",
        [["  cmp_a  ", " a ", "   =1+1"]],
        header=["company_id", "manual_label", "manual_notes"],
    )
    summary = _calibrate(tmp_path, "partial", labels)

    assert summary.labeled_count == 1
    assert summary.unlabeled_count == 1
    with (run_dir / "companies_calibrated.csv").open(encoding="utf-8", newline="") as handle:
        rows = {row["company_id"]: row for row in csv.DictReader(handle)}
    assert rows["cmp_a"]["manual_label"] == "A"
    assert rows["cmp_a"]["manual_notes"] == "'=1+1"
    assert rows["cmp_b"]["manual_label"] == ""
    assert rows["cmp_b"]["manual_notes"] == ""


def test_context_columns_from_template_are_ignored_and_regenerated(tmp_path: Path) -> None:
    """Read-only context supplied by labels cannot overwrite evaluated canonical context."""
    run_dir = _prepare(
        tmp_path,
        "context",
        [build_company(facts=accepted_facts(), company_id="cmp_a", name="Real Name")],
    )
    labels = _labels(
        tmp_path / "context.csv",
        [["cmp_a", "A", "ok", "FAKE NAME", "0.00"]],
        header=["company_id", "manual_label", "manual_notes", "name", "final_score"],
    )
    _calibrate(tmp_path, "context", labels)

    with (run_dir / "companies_calibrated.csv").open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["name"] == "Real Name"
    assert row["final_score"] != "0.00"


@pytest.mark.parametrize(
    "raw",
    [
        "company_id,manual_label,manual_label\ncmp_contract,A,A\n",
        "company_id,manual_label\ncmp_contract,A\ncmp_contract,B\n",
        "company_id,manual_label\nunknown,A\n",
        "company_id,manual_label\ncmp_contract,\n",
        "company_id,manual_label\n=cmd,A\n",
        "company_id,manual_label\ncmp_contract,D\n",
        'company_id,manual_label\ncmp_contract,"A\n',
    ],
)
def test_bad_label_inputs_fail_before_output_mutation(tmp_path: Path, raw: str) -> None:
    """Duplicate/unknown/blank/formula/malformed label data is rejected atomically."""
    run_dir = _prepare(
        tmp_path,
        "bad-labels",
        [build_company(facts=accepted_facts())],
    )
    labels = tmp_path / "bad.csv"
    labels.write_text(raw, encoding="utf-8")
    with pytest.raises((ValueError, csv.Error)):
        _calibrate(tmp_path, "bad-labels", labels)
    assert not (run_dir / "calibration_report.json").exists()
    assert not (run_dir / "companies_calibrated.csv").exists()


def test_label_and_output_symlinks_are_rejected(tmp_path: Path) -> None:
    """Calibration follows neither input label symlinks nor output symlinks."""
    run_dir = _prepare(
        tmp_path,
        "links",
        [build_company(facts=accepted_facts())],
    )
    real = _labels(tmp_path / "real.csv", [["cmp_contract", "A", "ok"]])
    link = tmp_path / "labels-link.csv"
    _symlink_or_skip(link, real)
    with pytest.raises(ValueError):
        _calibrate(tmp_path, "links", link)

    outside = tmp_path / "outside.json"
    outside.write_text("outside\n", encoding="utf-8")
    _symlink_or_skip(run_dir / "calibration_report.json", outside)
    with pytest.raises(ValueError):
        _calibrate(tmp_path, "links", real)
    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert not (run_dir / "companies_calibrated.csv").exists()


def test_duplicate_ids_and_conflicting_policy_versions_in_evaluated_input_fail(
    tmp_path: Path,
) -> None:
    """Calibration rejects ambiguous evaluated identity and policy context."""
    run_dir = _prepare(
        tmp_path,
        "corrupt-evaluated",
        [
            build_company(facts=accepted_facts(), company_id="cmp_a"),
            build_company(facts=accepted_facts(), company_id="cmp_b"),
        ],
    )
    path = run_dir / "companies_evaluated.jsonl"
    rows = path.read_text(encoding="utf-8").splitlines()
    labels = _labels(tmp_path / "valid.csv", [["cmp_a", "A", "ok"]])

    path.write_text(rows[0] + "\n" + rows[0] + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        _calibrate(tmp_path, "corrupt-evaluated", labels)

    first = json.loads(rows[0])
    second = json.loads(rows[1])
    second["evaluation_policy_version"] = "other"
    path.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        _calibrate(tmp_path, "corrupt-evaluated", labels)


def test_calibration_is_report_only_and_failed_recompute_preserves_outputs(tmp_path: Path) -> None:
    """Calibration never mutates policy/evaluated/checkpoint/usage and preserves prior reports."""
    run_dir = _prepare(
        tmp_path,
        "report-only",
        [build_company(facts=accepted_facts())],
    )
    labels = _labels(tmp_path / "good.csv", [["cmp_contract", "A", "ok"]])
    protected = [
        "companies_evaluated.jsonl",
        "companies_ranked.csv",
        "checkpoint.json",
        "usage.json",
        "usage_events.jsonl",
    ]
    before = {name: (run_dir / name).read_bytes() for name in protected}
    policy_before = repr(DEFAULT_POLICY)
    _calibrate(tmp_path, "report-only", labels)
    report_before = (run_dir / "calibration_report.json").read_bytes()
    joined_before = (run_dir / "companies_calibrated.csv").read_bytes()
    assert before == {name: (run_dir / name).read_bytes() for name in protected}
    assert repr(DEFAULT_POLICY) == policy_before

    bad = tmp_path / "later-bad.csv"
    bad.write_text("company_id,manual_label\nunknown,A\n", encoding="utf-8")
    with pytest.raises(ValueError):
        _calibrate(tmp_path, "report-only", bad)
    assert (run_dir / "calibration_report.json").read_bytes() == report_before
    assert (run_dir / "companies_calibrated.csv").read_bytes() == joined_before
