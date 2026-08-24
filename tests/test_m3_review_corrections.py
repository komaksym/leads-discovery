"""Focused M3 regressions confirmed during independent test review."""

from __future__ import annotations

from pathlib import Path

import pytest
from leads_discovery.pipeline.evaluation import EvaluationConfig, evaluate_run
from m3_factories import accepted_facts, build_company, write_run_inputs


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_usage_json_is_rejected_before_m3_output_mutation(
    tmp_path: Path,
    literal: str,
) -> None:
    """Corrupted non-finite usage state fails closed before any derived artifact is written."""
    run_dir = write_run_inputs(
        tmp_path,
        "nonfinite-usage",
        [build_company(facts=accepted_facts())],
    )
    (run_dir / "usage.json").write_text(
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
