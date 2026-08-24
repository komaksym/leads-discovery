"""Frozen-contract tests for M3 precision-first decisions and hard rejections."""

from __future__ import annotations

from copy import deepcopy

import pytest

from leads_discovery.scoring import evaluate_company
from m3_factories import FactInput, accepted_facts, build_company, low_score_facts


def _without(*keys: str) -> dict[str, FactInput]:
    """Return accepted facts without selected keys."""
    facts = accepted_facts()
    for key in keys:
        facts.pop(key)
    return facts


def test_each_acceptance_gate_independently_forces_uncertain() -> None:
    """A single failure of each precision gate yields uncertain, never rejected."""
    relevance = accepted_facts()
    relevance["pvf_relevant"] = (True, .7499)

    workload = _without(
        "inside_sales_or_estimating_presence",
        "project_or_tender_business",
        "bom_or_line_item_complexity",
        "manufacturer_count_or_breadth",
        "relevant_hiring",
        "industrial_or_process_customer_focus",
    )
    economic = _without(
        "employee_count",
        "multi_location_signal",
        "regional_independent_signal",
        "revenue_if_reliably_available",
    )
    incumbent = _without(
        "known_current_direct_competitor_customer",
        "known_quote_automation_or_order_automation_relationship",
    )

    low_overall: dict[str, FactInput] = {
        "pvf_relevant": (True, .90),
        "rfq_or_quote_workflow_evidence": (True, .90),
        "inside_sales_or_estimating_presence": (True, .90),
        "project_or_tender_business": (True, .90),
        "employee_count": (50, .90),
        "branch_count": (3, .90),
        "known_current_direct_competitor_customer": (False, .90),
        "direct_quotation_pain_evidence": (True, .90),
    }
    cases: list[tuple[dict[str, FactInput], str]] = [
        (relevance, "pvf_relevance_unresolved"),
        (low_score_facts(), "score_below_acceptance"),
        (low_overall, "low_overall_coverage"),
        (workload, "low_workload_coverage"),
        (economic, "low_economic_coverage"),
        (incumbent, "incumbent_exposure_unresolved"),
    ]
    for facts, reason in cases:
        result = evaluate_company(build_company(facts=facts))
        assert result.final_decision == "uncertain"
        assert result.rejection_reasons == []
        assert reason in result.review_reasons


def test_incumbent_true_below_hard_threshold_is_ambiguous() -> None:
    """Current competitor evidence at 0.8499 blocks acceptance but does not reject."""
    facts = accepted_facts()
    facts["known_current_direct_competitor_customer"] = (True, .8499)
    result = evaluate_company(build_company(facts=facts))
    assert result.final_decision == "uncertain"
    assert result.rejection_reasons == []
    assert "incumbent_exposure_ambiguous" in result.review_reasons


def test_acceptance_thresholds_are_inclusive() -> None:
    """Critical relevance, category coverage, and score thresholds accept at exact boundaries."""
    facts = accepted_facts()
    facts["pvf_relevant"] = (True, .75)
    result = evaluate_company(build_company(facts=facts))
    assert result.final_score is not None and result.final_score >= 70
    assert result.final_decision == "accepted"


@pytest.mark.parametrize("confidence", [.8499, .85])
def test_not_pvf_relevant_hard_threshold(confidence: float) -> None:
    """PVF false rejects only at the exact 0.85 hard-confidence boundary."""
    company = build_company(facts={"pvf_relevant": (False, confidence)})
    result = evaluate_company(company)
    if confidence < .85:
        assert result.final_decision == "uncertain"
        assert "confirmed_not_pvf_relevant" not in result.rejection_reasons
    else:
        assert result.final_decision == "rejected"
        assert result.rejection_reasons == ["confirmed_not_pvf_relevant"]
        reason = next(r for r in result.decision_reasons if r.code == result.rejection_reasons[0])
        assert reason.kind == "rejection"
        assert reason.confidence == .85
        assert reason.evidence_ids


@pytest.mark.parametrize("status", ["inactive", "dead"])
def test_inactive_or_dead_structural_rejection(status: str) -> None:
    """Only exact inactive/dead canonical status values hard reject."""
    result = evaluate_company(build_company(facts=accepted_facts(), status=status))
    assert result.final_decision == "rejected"
    assert "confirmed_inactive_or_dead" in result.rejection_reasons
    reason = next(r for r in result.decision_reasons if r.code == "confirmed_inactive_or_dead")
    assert reason.evidence_ids == []
    assert reason.explanation


@pytest.mark.parametrize("status", ["Active", "inactive ", "unknown"])
def test_status_near_misses_do_not_reject(status: str) -> None:
    """Status normalization is not invented by M3 hard rejection."""
    result = evaluate_company(build_company(facts=accepted_facts(), status=status))
    assert "confirmed_inactive_or_dead" not in result.rejection_reasons


def test_country_rejection_needs_matching_discovery_provenance() -> None:
    """Outside-US/CA requires canonical country and retained matching country code."""
    rejected = evaluate_company(
        build_company(
            facts=accepted_facts(),
            country="MX",
            discovery_country_code="MX",
        )
    )
    mismatch = evaluate_company(
        build_company(
            facts=accepted_facts(),
            country="MX",
            discovery_country_code="US",
        )
    )
    assert "confirmed_outside_us_canada" in rejected.rejection_reasons
    assert rejected.final_decision == "rejected"
    assert "confirmed_outside_us_canada" not in mismatch.rejection_reasons


@pytest.mark.parametrize("confidence", [.8499, .85])
def test_current_competitor_rejects_only_at_exact_threshold(confidence: float) -> None:
    """Current direct competitor truth hard rejects at 0.85, not one epsilon below."""
    facts = accepted_facts()
    facts["known_current_direct_competitor_customer"] = (True, confidence)
    result = evaluate_company(build_company(facts=facts))
    if confidence < .85:
        assert result.final_decision == "uncertain"
        assert "confirmed_current_direct_competitor_customer" not in result.rejection_reasons
    else:
        assert result.final_decision == "rejected"
        assert "confirmed_current_direct_competitor_customer" in result.rejection_reasons


def test_too_small_rule_requires_all_four_exact_high_confidence_facts() -> None:
    """The composite too-small rejection has no partial or low-confidence shortcut."""
    full: dict[str, FactInput] = {
        "employee_count": (9, .85),
        "branch_count": (1, .85),
        "inside_sales_or_estimating_presence": (False, .85),
        "rfq_or_quote_workflow_evidence": (False, .85),
    }
    rejected = evaluate_company(build_company(facts=full))
    assert rejected.final_decision == "rejected"
    assert "confirmed_too_small_for_meaningful_quote_workload" in rejected.rejection_reasons
    reason = next(
        r
        for r in rejected.decision_reasons
        if r.code == "confirmed_too_small_for_meaningful_quote_workload"
    )
    expected = sorted(
        evidence_id
        for key in full
        for evidence_id in build_company(facts=full).feature_confidence[key]["evidence_ids"]
    )
    assert reason.evidence_ids == expected

    for key in full:
        missing = deepcopy(full)
        missing.pop(key)
        assert "confirmed_too_small_for_meaningful_quote_workload" not in (
            evaluate_company(build_company(facts=missing)).rejection_reasons
        )

    low_conf = deepcopy(full)
    low_conf["employee_count"] = (9, .8499)
    assert "confirmed_too_small_for_meaningful_quote_workload" not in (
        evaluate_company(build_company(facts=low_conf)).rejection_reasons
    )


def test_hard_rejection_outweighs_otherwise_acceptable_company() -> None:
    """Hard rejection executes before the acceptance gates."""
    company = build_company(facts=accepted_facts(), status="inactive")
    result = evaluate_company(company)
    assert result.final_score is not None and result.final_score >= 70
    assert result.coverage["overall"] >= .70
    assert result.final_decision == "rejected"


def test_low_score_alone_never_rejects() -> None:
    """A fully covered low score remains uncertain until a future explicit soft rule exists."""
    result = evaluate_company(build_company(facts=low_score_facts()))
    assert result.final_score is not None and result.final_score < 70
    assert result.coverage["overall"] == 1.0
    assert result.final_decision == "uncertain"
    assert result.rejection_reasons == []
    assert "score_below_acceptance" in result.review_reasons


@pytest.mark.parametrize("history", [True, False])
def test_competitor_history_is_review_only(history: bool) -> None:
    """Historical evaluation never changes score or hard-rejects."""
    base = accepted_facts()
    without = evaluate_company(build_company(facts=base))

    with_history = accepted_facts()
    with_history["known_competitor_evaluation_history"] = (history, .90)
    result = evaluate_company(build_company(facts=with_history))

    assert result.final_score == without.final_score
    assert result.coverage == without.coverage
    assert result.rejection_reasons == []
    if history:
        assert "competitor_history_review" in result.review_reasons
    else:
        assert "competitor_history_review" not in result.review_reasons


def test_recompute_replaces_old_reason_codes() -> None:
    """Re-evaluation never accumulates stale review or rejection reasons."""
    company = build_company(facts=accepted_facts())
    company.review_reasons = ["score_below_acceptance", "stale"]
    company.rejection_reasons = ["confirmed_inactive_or_dead", "stale"]
    result = evaluate_company(company)
    assert result.final_decision == "accepted"
    assert result.review_reasons == []
    assert result.rejection_reasons == []
