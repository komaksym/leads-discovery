"""Frozen-contract tests for deterministic M3 scoring and model compatibility."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from m3_factories import accepted_facts, build_company, exact_threshold_facts

from leads_discovery.models import CompanyRecord, DecisionReason
from leads_discovery.scoring import (
    DEFAULT_POLICY,
    ScoringPolicy,
    evaluate_companies,
    evaluate_company,
)


@pytest.mark.parametrize(
    ("fact", "value", "category", "category_cov", "overall_cov"),
    [
        ("rfq_or_quote_workflow_evidence", True, "workload", .25, .10),
        ("inside_sales_or_estimating_presence", True, "workload", .15, .06),
        ("project_or_tender_business", True, "workload", .15, .06),
        ("bom_or_line_item_complexity", True, "workload", .15, .06),
        ("manufacturer_count_or_breadth", 20, "workload", .10, .04),
        ("relevant_hiring", True, "workload", .05, .02),
        ("industrial_or_process_customer_focus", True, "workload", .05, .02),
        ("employee_count", 50, "economic_fit", .35, .0875),
        ("multi_location_signal", True, "economic_fit", .20, .05),
        ("regional_independent_signal", True, "economic_fit", .15, .0375),
        ("revenue_if_reliably_available", 10_000_000.0, "economic_fit", .05, .0125),
        (
            "known_current_direct_competitor_customer",
            False,
            "low_incumbent_exposure",
            .60,
            .15,
        ),
        (
            "known_quote_automation_or_order_automation_relationship",
            False,
            "low_incumbent_exposure",
            .40,
            .10,
        ),
        ("direct_quotation_pain_evidence", True, "direct_pain", .40, .04),
        ("manual_workflow_evidence", True, "direct_pain", .35, .035),
        ("explicit_process_bottleneck_evidence", True, "direct_pain", .25, .025),
    ],
)
def test_exact_nonshared_weights(
    fact: str,
    value: object,
    category: str,
    category_cov: float,
    overall_cov: float,
) -> None:
    """A lone perfect fact exposes its exact local and product weight."""
    company = build_company(facts={fact: (value, .90)})  # type: ignore[arg-type]
    result = evaluate_company(company)

    assert result.score_components == {category: 100.0}
    assert result.coverage[category] == pytest.approx(category_cov)
    assert result.coverage["overall"] == pytest.approx(overall_cov)
    assert result.final_score == 100.0


@pytest.mark.parametrize(
    ("count", "workload", "economic"),
    [(1, 25, 40), (2, 60, 100), (5, 60, 100), (6, 85, 100), (15, 85, 100),
     (16, 100, 70), (30, 100, 70), (31, 100, 50)],
)
def test_branch_boundaries(count: int, workload: float, economic: float) -> None:
    """Branch count uses the two exact versioned transforms at every edge."""
    result = evaluate_company(build_company(facts={"branch_count": (count, .90)}))
    assert result.score_components["workload"] == workload
    assert result.score_components["economic_fit"] == economic
    assert result.coverage["workload"] == .10
    assert result.coverage["economic_fit"] == .25
    assert result.coverage["overall"] == .1025


@pytest.mark.parametrize(
    ("value", "score"),
    [(1, 20), (9, 20), (10, 60), (19, 60), (20, 100), (150, 100),
     (151, 70), (500, 70), (501, 50)],
)
def test_employee_boundaries(value: int, score: float) -> None:
    """Employee transform boundaries are exact."""
    result = evaluate_company(build_company(facts={"employee_count": (value, .90)}))
    assert result.score_components["economic_fit"] == score


@pytest.mark.parametrize(
    ("value", "score"),
    [(1.0, 20), (999_999.0, 20), (1_000_000.0, 50), (4_999_999.0, 50),
     (5_000_000.0, 100), (100_000_000.0, 100), (100_000_001.0, 70),
     (500_000_000.0, 70), (500_000_001.0, 50)],
)
def test_revenue_boundaries(value: float, score: float) -> None:
    """Revenue transform boundaries are exact."""
    result = evaluate_company(
        build_company(facts={"revenue_if_reliably_available": (value, .90)})
    )
    assert result.score_components["economic_fit"] == score


@pytest.mark.parametrize(
    ("value", "score"),
    [(0, 0), (1, 25), (4, 25), (5, 50), (9, 50), (10, 75), (19, 75), (20, 100),
     ("none", 0), ("NARROW", 25), ("Moderate", 60), ("broad", 100),
     (["", "  "], 0),
     (["A", " a ", "", "B", "b", "C", "D", "E"], 50)],
)
def test_manufacturer_breadth(value: object, score: float) -> None:
    """Breadth counts, exact categories, and casefolded list deduplication are frozen."""
    company = build_company(
        facts={"manufacturer_count_or_breadth": (value, .90)}  # type: ignore[arg-type]
    )
    assert evaluate_company(company).score_components["workload"] == score


def test_exact_category_and_final_formula() -> None:
    """Usable-weight normalization and coverage-weighted category influence are exact."""
    result = evaluate_company(
        build_company(
            facts={
                "rfq_or_quote_workflow_evidence": (True, .90),
                "inside_sales_or_estimating_presence": (False, .90),
                "employee_count": (20, .90),
                "known_current_direct_competitor_customer": (False, .90),
            }
        )
    )
    assert result.score_components == {
        "workload": 62.5,
        "economic_fit": 100.0,
        "low_incumbent_exposure": 100.0,
    }
    assert result.coverage == {
        "workload": .4,
        "economic_fit": .35,
        "low_incumbent_exposure": .6,
        "direct_pain": 0.0,
        "overall": .3975,
    }
    assert result.final_score == 84.91


def test_score_and_coverage_diverge_and_exact_70_accepts() -> None:
    """Sparse perfect evidence stays uncertain while full score 70 passes inclusively."""
    sparse = evaluate_company(
        build_company(facts={"rfq_or_quote_workflow_evidence": (True, .90)})
    )
    exact = evaluate_company(build_company(facts=exact_threshold_facts()))
    assert sparse.final_score == 100.0
    assert sparse.coverage["overall"] == .10
    assert sparse.final_decision == "uncertain"
    assert exact.final_score == 70.0
    assert exact.coverage["overall"] == 1.0
    assert exact.final_decision == "accepted"


def test_decision_uses_unrounded_score() -> None:
    """A gate between raw 70.418... and persisted 70.42 must fail."""
    facts = {
        "pvf_relevant": (True, .90),
        "inside_sales_or_estimating_presence": (False, .90),
        "project_or_tender_business": (False, .90),
        "bom_or_line_item_complexity": (False, .90),
        "manufacturer_count_or_breadth": (0, .90),
        "industrial_or_process_customer_focus": (True, .90),
        "employee_count": (50, .90),
        "multi_location_signal": (True, .90),
        "regional_independent_signal": (True, .90),
        "revenue_if_reliably_available": (500_000.0, .90),
        "known_current_direct_competitor_customer": (False, .90),
        "known_quote_automation_or_order_automation_relationship": (False, .90),
        "direct_quotation_pain_evidence": (True, .90),
        "manual_workflow_evidence": (True, .90),
        "explicit_process_bottleneck_evidence": (True, .90),
    }
    result = evaluate_company(
        build_company(facts=facts),
        policy=ScoringPolicy(acceptance_score=70.419),
    )
    assert result.final_score == 70.42
    assert result.coverage["overall"] == .7775
    assert result.final_decision == "uncertain"
    assert "score_below_acceptance" in result.review_reasons


@pytest.mark.parametrize(
    ("fact", "bad"),
    [
        ("employee_count", True), ("employee_count", 0), ("employee_count", -1),
        ("employee_count", "50"), ("branch_count", False), ("branch_count", 0),
        ("revenue_if_reliably_available", 0.0),
        ("revenue_if_reliably_available", float("nan")),
        ("revenue_if_reliably_available", float("inf")),
        ("manufacturer_count_or_breadth", -1),
        ("manufacturer_count_or_breadth", "wide"),
    ],
)
def test_unsupported_values_stay_unknown(fact: str, bad: object) -> None:
    """Unsupported facts never coerce to zero and receive an invalid-fact review code."""
    company = build_company(facts={fact: (1, .90)})
    company.features[fact] = bad
    result = evaluate_company(company)
    assert f"invalid_fact:{fact}" in result.review_reasons
    assert result.final_score is None


@pytest.mark.parametrize("confidence", [0.0, .5999])
def test_low_confidence_stays_unknown(confidence: float) -> None:
    """Below 0.60 a fact contributes no score or coverage."""
    result = evaluate_company(build_company(facts={"employee_count": (50, confidence)}))
    assert "economic_fit" not in result.score_components
    assert result.coverage["economic_fit"] == 0.0


@pytest.mark.parametrize("bad_conf", [True, -.1, 1.1, float("nan"), float("inf")])
def test_bad_confidence_stays_unknown(bad_conf: object) -> None:
    """Malformed confidence metadata is invalid rather than usable."""
    company = build_company(facts={"employee_count": (50, .90)})
    company.feature_confidence["employee_count"]["confidence"] = bad_conf
    result = evaluate_company(company)
    assert "invalid_fact:employee_count" in result.review_reasons
    assert "economic_fit" not in result.score_components


@pytest.mark.parametrize("mode", ["none", "missing", "duplicate"])
def test_bad_citations_stay_unknown(mode: str) -> None:
    """Usable non-null facts require nonempty unique retained evidence IDs."""
    company = build_company(facts={"employee_count": (50, .90)})
    ids = company.feature_confidence["employee_count"]["evidence_ids"]
    if mode == "none":
        company.feature_confidence["employee_count"]["evidence_ids"] = []
    elif mode == "missing":
        company.feature_confidence["employee_count"]["evidence_ids"] = ["ev_missing"]
    else:
        company.feature_confidence["employee_count"]["evidence_ids"] = [ids[0], ids[0]]
    result = evaluate_company(company)
    assert "invalid_fact:employee_count" in result.review_reasons
    assert result.coverage["economic_fit"] == 0.0


def test_standard_unknown_and_minimum_confidence() -> None:
    """M2 null/zero/[] is normal unknown and confidence exactly 0.60 is usable."""
    unknown = evaluate_company(build_company(facts={"employee_count": (None, 0.0)}))
    known = evaluate_company(build_company(facts={"employee_count": (50, .60)}))
    assert "invalid_fact:employee_count" not in unknown.review_reasons
    assert unknown.final_score is None
    assert "score_unavailable" in unknown.review_reasons
    assert known.score_components["economic_fit"] == 100.0


def test_exact_output_keys_and_default_policy() -> None:
    """Coverage keys and all seven default policy thresholds are inspectable and frozen."""
    result = evaluate_company(build_company(facts={"employee_count": (50, .90)}))
    assert set(result.coverage) == {
        "workload", "economic_fit", "low_incumbent_exposure", "direct_pain", "overall"
    }
    assert set(result.score_components) == {"economic_fit"}
    assert ScoringPolicy(
        version="m3-v1",
        minimum_fact_confidence=.60,
        critical_relevance_confidence=.75,
        hard_rejection_confidence=.85,
        acceptance_score=70.0,
        minimum_overall_coverage=.70,
        minimum_workload_coverage=.60,
        minimum_economic_coverage=.50,
    ) == DEFAULT_POLICY
    with pytest.raises(FrozenInstanceError):
        DEFAULT_POLICY.acceptance_score = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"version": ""}, {"version": "   "}, {"minimum_fact_confidence": True},
        {"minimum_fact_confidence": -.01}, {"minimum_fact_confidence": 1.01},
        {"critical_relevance_confidence": float("nan")},
        {"hard_rejection_confidence": float("inf")}, {"acceptance_score": True},
        {"acceptance_score": -.01}, {"acceptance_score": 100.01},
        {"minimum_overall_coverage": -.01}, {"minimum_workload_coverage": 1.01},
        {"minimum_economic_coverage": float("-inf")},
    ],
)
def test_policy_validation(kwargs: dict[str, Any]) -> None:
    """Booleans, non-finite thresholds, bad ranges, and blank versions are rejected."""
    with pytest.raises((TypeError, ValueError)):
        ScoringPolicy(**kwargs)


def test_recompute_is_defensive_and_replaces_stale_m3_state() -> None:
    """Evaluation returns a detached record and preserves every M2-owned nested value."""
    company = build_company(facts=accepted_facts())
    company.coverage = {"stale": 1.0}
    company.score_components = {"stale": 1.0}
    company.final_score = 1.0
    company.final_decision = "rejected"
    company.review_reasons = ["stale"]
    company.rejection_reasons = ["stale"]
    company.decision_reasons = [
        DecisionReason("stale", "review", "old", evidence_ids=["ev_old"])
    ]
    company.evaluation_policy_version = "old"
    original = company.to_dict()

    result = evaluate_company(company)

    assert company.to_dict() == original
    assert result is not company
    assert result.features == company.features
    assert result.feature_confidence == company.feature_confidence
    assert result.evidence == company.evidence
    assert result.discovery_records == company.discovery_records
    assert result.final_decision == "accepted"
    assert "stale" not in result.review_reasons + result.rejection_reasons
    assert all(reason.code != "stale" for reason in result.decision_reasons)
    result.features["new"] = "detached"
    assert "new" not in company.features


def test_new_reason_and_old_payload_defensive_roundtrip() -> None:
    """New nested reason objects copy defensively and old M1/M2 payloads still load."""
    ids = ["ev_1"]
    reason = DecisionReason("review", "review", "Needs review", evidence_ids=ids)
    ids.append("ev_2")
    company = CompanyRecord("cmp_copy", "Copy Valve", decision_reasons=[reason])
    reason.evidence_ids.append("ev_3")
    assert company.decision_reasons[0].evidence_ids == ["ev_1"]

    payload = build_company(facts=accepted_facts()).to_dict()
    payload.pop("decision_reasons", None)
    payload.pop("evaluation_policy_version", None)
    original = deepcopy(payload)
    loaded = CompanyRecord.from_dict(payload)
    assert payload == original
    assert loaded.decision_reasons == []
    assert loaded.evaluation_policy_version is None
    assert CompanyRecord.from_dict(loaded.to_dict()).to_dict() == loaded.to_dict()


def test_batch_cap_sort_filter_and_duplicate_rules() -> None:
    """Batch scoring filters incomplete records, sorts IDs, caps at 20, and rejects duplicates."""
    companies = [
        build_company(
            facts=accepted_facts(),
            company_id=f"cmp_{i:02d}",
            extraction_completed=i != 5,
        )
        for i in range(22, -1, -1)
    ]
    result = evaluate_companies(companies, limit=20)
    assert isinstance(result, tuple)
    assert len(result) == 20
    assert [item.company_id for item in result] == [
        f"cmp_{i:02d}" for i in range(20) if i != 5
    ] + ["cmp_20"]

    for bad in (0, 21, True):
        with pytest.raises((TypeError, ValueError)):
            evaluate_companies(
                [build_company(facts=accepted_facts())],
                limit=bad,  # type: ignore[arg-type]
            )
    duplicate = build_company(facts=accepted_facts(), company_id="cmp_dup")
    with pytest.raises(ValueError):
        evaluate_companies([duplicate, deepcopy(duplicate)])


def test_incomplete_extraction_is_not_scored() -> None:
    """Single-company evaluation rejects incomplete extraction before doing M3 work."""
    with pytest.raises(ValueError):
        evaluate_company(build_company(facts=accepted_facts(), extraction_completed=False))
