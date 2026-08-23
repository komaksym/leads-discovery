"""Adversarial M3 probes added after production and contract-test integration."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

import leads_discovery.pipeline.state as state_module
from leads_discovery.pipeline.evaluation import EvaluationConfig, evaluate_run
from leads_discovery.scoring import ScoringPolicy, evaluate_company
from m3_factories import (
    accepted_facts,
    build_company,
    exact_threshold_facts,
    write_run_inputs,
)


def test_exact_coverage_gates_are_inclusive_and_epsilon_above_blocks() -> None:
    """Raw coverage equal to each frozen threshold passes; epsilon above it blocks acceptance."""
    workload_economic = {
        "pvf_relevant": (True, 0.90),
        "rfq_or_quote_workflow_evidence": (True, 0.90),
        "inside_sales_or_estimating_presence": (True, 0.90),
        "project_or_tender_business": (True, 0.90),
        "relevant_hiring": (True, 0.90),
        "employee_count": (50, 0.90),
        "regional_independent_signal": (True, 0.90),
        "known_current_direct_competitor_customer": (False, 0.90),
        "known_quote_automation_or_order_automation_relationship": (False, 0.90),
        "direct_quotation_pain_evidence": (True, 0.90),
        "manual_workflow_evidence": (True, 0.90),
        "explicit_process_bottleneck_evidence": (True, 0.90),
    }
    exact = evaluate_company(build_company(facts=workload_economic))
    assert exact.coverage["workload"] == 0.60
    assert exact.coverage["economic_fit"] == 0.50
    assert exact.final_decision == "accepted"

    workload_blocked = evaluate_company(
        build_company(facts=workload_economic),
        policy=ScoringPolicy(minimum_workload_coverage=0.6001),
    )
    economic_blocked = evaluate_company(
        build_company(facts=workload_economic),
        policy=ScoringPolicy(minimum_economic_coverage=0.5001),
    )
    assert workload_blocked.final_decision == "uncertain"
    assert "low_workload_coverage" in workload_blocked.review_reasons
    assert economic_blocked.final_decision == "uncertain"
    assert "low_economic_coverage" in economic_blocked.review_reasons

    overall_facts = {
        "pvf_relevant": (True, 0.90),
        "rfq_or_quote_workflow_evidence": (True, 0.90),
        "inside_sales_or_estimating_presence": (True, 0.90),
        "project_or_tender_business": (True, 0.90),
        "bom_or_line_item_complexity": (True, 0.90),
        "relevant_hiring": (True, 0.90),
        "employee_count": (50, 0.90),
        "multi_location_signal": (True, 0.90),
        "revenue_if_reliably_available": (10_000_000.0, 0.90),
        "known_current_direct_competitor_customer": (False, 0.90),
        "known_quote_automation_or_order_automation_relationship": (False, 0.90),
    }
    overall_exact = evaluate_company(build_company(facts=overall_facts))
    assert overall_exact.coverage["overall"] == 0.70
    assert overall_exact.final_decision == "accepted"
    overall_blocked = evaluate_company(
        build_company(facts=overall_facts),
        policy=ScoringPolicy(minimum_overall_coverage=0.7001),
    )
    assert overall_blocked.final_decision == "uncertain"
    assert "low_overall_coverage" in overall_blocked.review_reasons


def test_exact_score_70_is_inclusive_and_epsilon_above_blocks() -> None:
    """A raw score of exactly 70 passes the default gate and fails a 70.0001 experiment."""
    company = build_company(facts=exact_threshold_facts())
    exact = evaluate_company(company)
    blocked = evaluate_company(
        company,
        policy=ScoringPolicy(acceptance_score=70.0001),
    )
    assert exact.final_score == 70.0
    assert exact.final_decision == "accepted"
    assert blocked.final_score == 70.0
    assert blocked.final_decision == "uncertain"
    assert "score_below_acceptance" in blocked.review_reasons


def test_quote_automation_incumbent_conflict_blocks_acceptance_but_never_hard_rejects() -> None:
    """A positive automation relationship is ambiguous context, not a direct-customer hard rule."""
    facts = accepted_facts()
    facts["known_current_direct_competitor_customer"] = (False, 0.99)
    facts["known_quote_automation_or_order_automation_relationship"] = (True, 0.99)
    result = evaluate_company(build_company(facts=facts))

    assert result.final_decision == "uncertain"
    assert result.rejection_reasons == []
    assert "incumbent_exposure_ambiguous" in result.review_reasons


@pytest.mark.parametrize(
    "name",
    [
        "   =SUM(1,2)",
        "\t+cmd",
        "\u2003-payload",
        "\u00a0@payload",
    ],
)
def test_csv_formula_protection_handles_all_prefixes_and_unicode_whitespace(
    tmp_path: Path,
    name: str,
) -> None:
    """Every frozen formula prefix is neutralized after ASCII or Unicode whitespace."""
    run_dir = write_run_inputs(
        tmp_path,
        "formula-red-team",
        [build_company(facts=accepted_facts(), name=name)],
    )
    evaluate_run(EvaluationConfig(run_id="formula-red-team", data_root=tmp_path))

    with (run_dir / "companies_ranked.csv").open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["name"] == "'" + name


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_usage_summary_is_rejected_before_m3_output_mutation(
    tmp_path: Path,
    literal: str,
) -> None:
    """Corrupted non-finite M2 usage state must fail closed before replacing derived artifacts."""
    run_dir = write_run_inputs(
        tmp_path,
        "nonfinite-usage",
        [build_company(facts=accepted_facts())],
    )
    usage = run_dir / "usage.json"
    usage.write_text(
        "{\n"
        '  "providers": {},\n'
        '  "total": {\n'
        '    "request_count": 0,\n'
        '    "input_tokens": 0,\n'
        '    "output_tokens": 0,\n'
        f'    "estimated_cost_usd": {literal},\n'
        '    "exact_cost_usd": null\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        evaluate_run(EvaluationConfig(run_id="nonfinite-usage", data_root=tmp_path))

    for name in (
        "companies_evaluated.jsonl",
        "companies_ranked.csv",
        "companies_rejected.csv",
        "companies_uncertain.csv",
        "calibration_template.csv",
        "run_summary.json",
    ):
        assert not (run_dir / name).exists()


def test_atomic_text_write_failure_preserves_existing_target_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed atomic replace leaves the prior complete file intact and removes its temp file."""
    target = tmp_path / "derived.csv"
    target.write_text("old\n", encoding="utf-8")

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(state_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        state_module.write_text_atomic(target, "new\n")

    assert target.read_text(encoding="utf-8") == "old\n"
    assert list(tmp_path.glob("derived.csv.*.tmp")) == []
