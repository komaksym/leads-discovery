"""Contract tests for M3 canonical JSONL/CSV output schemas and safety."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from m3_factories import accepted_facts, build_company, exact_threshold_facts, low_score_facts

from leads_discovery.models import CompanyRecord
from leads_discovery.pipeline.evaluation import EvaluationConfig, evaluate_run

RANKED_COLUMNS = [
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


def test_exact_jsonl_and_ranked_csv_schemas_and_stable_order(tmp_path: Path) -> None:
    """JSONL sorts by ID while the one ranked CSV preserves deterministic decision ordering."""
    a = build_company(facts=accepted_facts(), company_id="cmp_b", name="Alpha")
    b = build_company(facts=accepted_facts(), company_id="cmp_a", name="Beta")
    c = build_company(facts=exact_threshold_facts(), company_id="cmp_c", name="Gamma")
    u = build_company(facts=accepted_facts(), company_id="cmp_u", name="Uncertain")
    u.features["pvf_relevant"] = True
    u.feature_confidence["pvf_relevant"]["confidence"] = 0.7499
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
    assert [row["company_id"] for row in ranked if row["final_decision"] == "rejected"] == ["cmp_r"]
    assert [row["company_id"] for row in ranked if row["final_decision"] == "uncertain"] == ["cmp_u"]
    assert not (run_dir / "companies_rejected.csv").exists()
    assert not (run_dir / "companies_uncertain.csv").exists()


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
    """Absent category scores are empty cells; displayed numbers use fixed precision."""
    sparse = build_company(
        facts={"pvf_relevant": (True, 0.90), "employee_count": (151, 0.90)},
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
    assert len(by_id["cmp_full"]["final_score"].split(".")[1]) == 2


def test_reason_lists_and_calibration_template_shape_are_exact(tmp_path: Path) -> None:
    """CSV reasons stay semicolon-joined and the template adds only manual columns."""
    run_dir = _run(tmp_path, "template", [build_company(facts=low_score_facts())])
    header, ranked = _csv(run_dir / "companies_ranked.csv")
    template_header, template = _csv(run_dir / "calibration_template.csv")

    assert header == RANKED_COLUMNS
    assert "score_below_acceptance" in ranked[0]["review_reasons"].split(";")
    assert template_header == RANKED_COLUMNS + ["manual_label", "manual_notes"]
    assert template[0]["manual_label"] == template[0]["manual_notes"] == ""


def test_run_summary_is_derived_from_usage_events_and_has_relative_artifact_paths(
    tmp_path: Path,
) -> None:
    """Summary derives cost semantics from the ledger instead of a second persisted usage cache."""
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
        "total": {},
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
    assert report["usage"]["total"]["estimated_cost_usd"] == 0.021
    assert report["usage"]["total"]["exact_cost_usd"] is None
    assert not (run_dir / "usage.json").exists()
    assert set(report["artifacts"]) == {
        "companies_evaluated",
        "companies_ranked",
        "calibration_template",
        "run_summary",
    }
    assert all("/" not in value for value in report["artifacts"].values())


def test_empty_partial_run_emits_only_canonical_csv_headers(tmp_path: Path) -> None:
    """A zero-result paused run publishes the ranked and calibration-template views."""
    from m3_factories import write_run_inputs

    run_dir = write_run_inputs(
        tmp_path,
        "empty-output",
        [],
        checkpoint_status="paused_unknown",
        pause_reason="unknown_in_flight:research:x",
    )
    evaluate_run(EvaluationConfig(run_id="empty-output", data_root=tmp_path))
    for name in ("companies_ranked.csv", "calibration_template.csv"):
        raw = (run_dir / name).read_bytes()
        assert raw.endswith(b"\n")
        assert b"\r\n" not in raw
        header, rows = _csv(run_dir / name)
        expected = RANKED_COLUMNS + (["manual_label", "manual_notes"] if "template" in name else [])
        assert header == expected
        assert rows == []
