"""Frozen-contract tests for M3 JSONL/CSV derived output schemas and safety."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from m3_factories import accepted_facts, build_company, exact_threshold_facts, low_score_facts

from leads_discovery.models import CompanyRecord
from leads_discovery.pipeline.evaluation import EvaluationConfig, evaluate_run

RANKED_COLUMNS = [
    "company_id", "name", "domain", "country", "policy_version",
    "workload_score", "workload_coverage",
    "economic_fit_score", "economic_fit_coverage",
    "low_incumbent_exposure_score", "low_incumbent_exposure_coverage",
    "direct_pain_score", "direct_pain_coverage",
    "overall_coverage", "final_score", "final_decision",
    "review_reasons", "rejection_reasons",
]


def _run(tmp_path: Path, run_id: str, companies: list[CompanyRecord]) -> Path:
    """Persist M2 fixtures and publish M3 output views."""
    from m3_factories import write_run_inputs

    run_dir = write_run_inputs(tmp_path, run_id, companies)
    evaluate_run(EvaluationConfig(run_id=run_id, data_root=tmp_path))
    return run_dir


def _csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read one RFC-compatible CSV into header and rows."""
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames is not None
        return list(reader.fieldnames), list(reader)


def test_exact_jsonl_and_csv_schemas_and_stable_order(tmp_path: Path) -> None:
    """JSONL sorts by ID while CSV uses decision, score, coverage, name, then ID."""
    a = build_company(facts=accepted_facts(), company_id="cmp_b", name="Alpha")
    b = build_company(facts=accepted_facts(), company_id="cmp_a", name="Beta")
    c = build_company(facts=exact_threshold_facts(), company_id="cmp_c", name="Gamma")
    u = build_company(facts=accepted_facts(), company_id="cmp_u", name="Uncertain")
    u.features["pvf_relevant"] = True
    u.feature_confidence["pvf_relevant"]["confidence"] = .7499
    r = build_company(facts=accepted_facts(), company_id="cmp_r", name="Rejected", status="dead")
    run_dir = _run(tmp_path, "schemas", [u, c, r, b, a])

    json_rows = [
        json.loads(line)
        for line in (run_dir / "companies_evaluated.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["company_id"] for row in json_rows] == ["cmp_a", "cmp_b", "cmp_c", "cmp_r", "cmp_u"]
    for row in json_rows:
        assert CompanyRecord.from_dict(row).to_dict() == row
        assert row["evaluation_policy_version"] == "m3-v1"

    header, ranked = _csv(run_dir / "companies_ranked.csv")
    assert header == RANKED_COLUMNS
    assert [row["company_id"] for row in ranked] == ["cmp_b", "cmp_a", "cmp_c", "cmp_u", "cmp_r"]

    rejected_header, rejected = _csv(run_dir / "companies_rejected.csv")
    uncertain_header, uncertain = _csv(run_dir / "companies_uncertain.csv")
    assert rejected_header == uncertain_header == RANKED_COLUMNS
    assert [row["company_id"] for row in rejected] == ["cmp_r"]
    assert [row["company_id"] for row in uncertain] == ["cmp_u"]


def test_csv_quotes_unicode_newlines_and_formula_text_while_json_preserves_original(
    tmp_path: Path,
) -> None:
    """External text is RFC-quoted and spreadsheet-neutralized without changing canonical JSON."""
    name = '  =SUM(1,2)\n"Válvulas", 東京'
    company = build_company(facts=accepted_facts(), name=name, company_id="cmp_formula")
    run_dir = _run(tmp_path, "text", [company])

    raw_csv = (run_dir / "companies_ranked.csv").read_bytes()
    assert b"\r\n" not in raw_csv
    header, rows = _csv(run_dir / "companies_ranked.csv")
    assert header == RANKED_COLUMNS
    assert rows[0]["name"] == "'" + name

    payload = json.loads((run_dir / "companies_evaluated.jsonl").read_text(encoding="utf-8"))
    assert payload["name"] == name


def test_missing_scores_are_blank_and_numeric_display_rounding_is_exact(tmp_path: Path) -> None:
    """Absent category scores are empty cells; displayed scores/coverage use fixed precision."""
    sparse = build_company(
        facts={
            "pvf_relevant": (True, .90),
            "employee_count": (151, .90),
        },
        company_id="cmp_sparse",
    )
    full = build_company(facts=accepted_facts(), company_id="cmp_full")
    run_dir = _run(tmp_path, "display", [sparse, full])
    _, rows = _csv(run_dir / "companies_ranked.csv")
    by_id = {row["company_id"]: row for row in rows}

    assert by_id["cmp_sparse"]["workload_score"] == ""
    assert by_id["cmp_sparse"]["direct_pain_score"] == ""
    assert by_id["cmp_sparse"]["economic_fit_score"] == "70.00"
    assert by_id["cmp_sparse"]["economic_fit_coverage"] == "0.3500"
    assert by_id["cmp_full"]["overall_coverage"] == "1.0000"
    assert by_id["cmp_full"]["final_score"].count(".") == 1
    assert len(by_id["cmp_full"]["final_score"].split(".")[1]) == 2


def test_reason_lists_are_semicolon_joined_and_template_adds_only_two_columns(
    tmp_path: Path,
) -> None:
    """CSV reason serialization and calibration-template shape are exact."""
    low = build_company(facts=low_score_facts(), company_id="cmp_low")
    run_dir = _run(tmp_path, "template", [low])
    header, ranked = _csv(run_dir / "companies_ranked.csv")
    template_header, template = _csv(run_dir / "calibration_template.csv")

    assert header == RANKED_COLUMNS
    assert ";" not in ranked[0]["rejection_reasons"]
    assert "score_below_acceptance" in ranked[0]["review_reasons"].split(";")
    assert template_header == RANKED_COLUMNS + ["manual_label", "manual_notes"]
    assert template[0]["manual_label"] == template[0]["manual_notes"] == ""
    assert [template[0][key] for key in RANKED_COLUMNS] == [
        ranked[0][key] for key in RANKED_COLUMNS
    ]


def test_run_summary_keeps_provider_cost_semantics_and_relative_artifact_paths(
    tmp_path: Path,
) -> None:
    """Summary carries exact/estimated/null usage without fabricating an aggregate exact cost."""
    usage = {
        "providers": {
            "exa": {
                "request_count": 2,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_usd": 0.02,
                "exact_cost_usd": None,
            },
            "deepseek": {
                "request_count": 1,
                "input_tokens": 10,
                "output_tokens": 2,
                "estimated_cost_usd": 0.001,
                "exact_cost_usd": 0.0009,
            },
        },
        "total": {
            "request_count": 3,
            "input_tokens": 10,
            "output_tokens": 2,
            "estimated_cost_usd": 0.021,
            "exact_cost_usd": None,
        },
    }
    from m3_factories import write_run_inputs

    run_dir = write_run_inputs(
        tmp_path,
        "summary-output",
        [build_company(facts=accepted_facts())],
        usage=usage,
    )
    evaluate_run(EvaluationConfig(run_id="summary-output", data_root=tmp_path))
    report = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))

    assert report["policy_version"] == "m3-v1"
    assert report["evaluated_count"] == 1
    assert report["accepted_count"] == 1
    text = json.dumps(report, sort_keys=True)
    assert '"exact_cost_usd": null' in text
    assert '"estimated_cost_usd": 0.021' in text

    def strings(value: object) -> list[str]:
        """Collect nested strings for path-safety assertions."""
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [item for child in value for item in strings(child)]
        if isinstance(value, dict):
            return [item for child in value.values() for item in strings(child)]
        return []

    artifact_values = [value for value in strings(report) if "." in value and "/" in value]
    assert all(not Path(value).is_absolute() for value in artifact_values)
    assert all(".tmp" not in value for value in artifact_values)


def test_empty_partial_run_still_emits_all_csv_headers_with_lf(tmp_path: Path) -> None:
    """A zero-result paused run publishes inspectable header-only CSV views."""
    from m3_factories import write_run_inputs

    run_dir = write_run_inputs(
        tmp_path,
        "empty-output",
        [],
        checkpoint_status="paused_unknown",
        pause_reason="unknown_in_flight:research:x",
    )
    evaluate_run(EvaluationConfig(run_id="empty-output", data_root=tmp_path))
    for name in (
        "companies_ranked.csv",
        "companies_rejected.csv",
        "companies_uncertain.csv",
        "calibration_template.csv",
    ):
        raw = (run_dir / name).read_bytes()
        assert raw.endswith(b"\n")
        assert b"\r\n" not in raw
        header, rows = _csv(run_dir / name)
        expected = RANKED_COLUMNS + (["manual_label", "manual_notes"] if "template" in name else [])
        assert header == expected
        assert rows == []
