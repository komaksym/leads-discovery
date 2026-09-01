"""Behavioral contracts for proposition-aware evidence support."""

from __future__ import annotations

from typing import Any

import pytest

from leads_discovery.models import (
    CompanyRecord,
    EvidenceBundle,
    EvidenceItem,
    ExtractedFact,
    ExtractionResult,
    UsageEvent,
)
from leads_discovery.research.extract import FACT_KEYS, apply_extraction
from leads_discovery.scoring import evaluate_company

_EVIDENCE_ID = "ev_support_contract"


def _company() -> CompanyRecord:
    return CompanyRecord(
        company_id="cmp_support",
        name="Support Industrial",
        normalized_name="support industrial",
        domain="support.example",
        normalized_domain="support.example",
        country="US",
    )


def _bundle(excerpt: str) -> EvidenceBundle:
    return EvidenceBundle(
        company_id="cmp_support",
        items=[
            EvidenceItem(
                evidence_id=_EVIDENCE_ID,
                url="https://support.example/about",
                title="About Support Industrial",
                excerpt=excerpt,
                source_type="web",
                provider="exa",
                retrieved_at="2026-08-31T00:00:00+00:00",
            )
        ],
        raw_records=[],
        usage_events=[],
    )


def _result(key: str, value: Any, confidence: float = 0.99) -> ExtractionResult:
    facts = {name: ExtractedFact(None, 0.0, []) for name in FACT_KEYS}
    facts[key] = ExtractedFact(value, confidence, [_EVIDENCE_ID])
    return ExtractionResult(
        company_id="cmp_support",
        model="deepseek-v4-flash",
        facts=facts,
        usage_event=UsageEvent(provider="deepseek", operation="structured_extraction"),
    )


@pytest.mark.parametrize(
    "excerpt",
    [
        "We do not manufacture pipe. We distribute industrial pipe, valves, and fittings.",
        "We do not install pipe. We distribute industrial pipe, valves, and fittings.",
        "We do not sell electrical equipment. We distribute industrial valves.",
        "We are not a manufacturer. We distribute industrial valves.",
    ],
)
def test_unrelated_negation_becomes_unknown_before_evaluation(excerpt: str) -> None:
    extracted = apply_extraction(_company(), _bundle(excerpt), _result("pvf_relevant", False))
    evaluated = evaluate_company(extracted)

    assert extracted.features["pvf_relevant"] is None
    assert extracted.feature_confidence["pvf_relevant"] == {
        "confidence": 0.0,
        "evidence_ids": [],
    }
    assert "confirmed_not_pvf_relevant" not in evaluated.rejection_reasons


def test_genuine_negative_pvf_proposition_remains_canonical() -> None:
    extracted = apply_extraction(
        _company(),
        _bundle("We do not sell or distribute pipe, valves, or fittings."),
        _result("pvf_relevant", False),
    )

    assert extracted.features["pvf_relevant"] is False


def test_positive_pvf_claim_requires_direct_pvf_evidence() -> None:
    supported = apply_extraction(
        _company(),
        _bundle("Industrial pipe, valves, and fittings."),
        _result("pvf_relevant", True),
    )
    unsupported = apply_extraction(
        _company(),
        _bundle("We build electrical control panels."),
        _result("pvf_relevant", True),
    )

    assert supported.features["pvf_relevant"] is True
    assert unsupported.features["pvf_relevant"] is None


def test_numeric_fact_requires_value_and_fact_concept_in_cited_evidence() -> None:
    supported = apply_extraction(
        _company(),
        _bundle("We operate 3 branch locations across the region."),
        _result("branch_count", 3, 0.9),
    )
    unsupported = apply_extraction(
        _company(),
        _bundle("The company was founded in 3 states decades ago."),
        _result("branch_count", 3, 0.9),
    )

    assert supported.features["branch_count"] == 3
    assert unsupported.features["branch_count"] is None


def test_coordinated_unrelated_pvf_negation_does_not_bind_to_distribution() -> None:
    extracted = apply_extraction(
        _company(),
        _bundle("We do not manufacture pipe and distribute industrial valves."),
        _result("pvf_relevant", False),
    )
    evaluated = evaluate_company(extracted)

    assert extracted.features["pvf_relevant"] is None
    assert evaluated.final_decision == "uncertain"
    assert "confirmed_not_pvf_relevant" not in evaluated.rejection_reasons


def test_contradictory_pvf_evidence_cannot_validate_negative_relevance() -> None:
    extracted = apply_extraction(
        _company(),
        _bundle("We do not sell pipe, but we distribute industrial valves."),
        _result("pvf_relevant", False),
    )
    evaluated = evaluate_company(extracted)

    assert extracted.features["pvf_relevant"] is None
    assert "confirmed_not_pvf_relevant" not in evaluated.rejection_reasons


def test_numeric_fact_requires_value_to_describe_fact_concept() -> None:
    extracted = apply_extraction(
        _company(),
        _bundle("3 employees work at our branch."),
        _result("branch_count", 3, 0.9),
    )

    assert extracted.features["branch_count"] is None
    assert extracted.feature_confidence["branch_count"] == {
        "confidence": 0.0,
        "evidence_ids": [],
    }


def test_boolean_negative_requires_negation_of_fact_concept() -> None:
    extracted = apply_extraction(
        _company(),
        _bundle("Our RFQ workflow is online, not manual."),
        _result("rfq_or_quote_workflow_evidence", False),
    )

    assert extracted.features["rfq_or_quote_workflow_evidence"] is None
    assert extracted.feature_confidence["rfq_or_quote_workflow_evidence"] == {
        "confidence": 0.0,
        "evidence_ids": [],
    }


def test_coordinated_negative_pvf_relation_remains_canonical() -> None:
    extracted = apply_extraction(
        _company(),
        _bundle("We do not manufacture or sell pipe."),
        _result("pvf_relevant", False),
    )

    assert extracted.features["pvf_relevant"] is False


def test_bare_pvf_mention_does_not_veto_explicit_negative_relation() -> None:
    extracted = apply_extraction(
        _company(),
        _bundle("Pipe products. We do not sell pipe."),
        _result("pvf_relevant", False),
    )

    assert extracted.features["pvf_relevant"] is False


def test_genuine_negative_pvf_proposition_triggers_rejection() -> None:
    extracted = apply_extraction(
        _company(),
        _bundle("We do not distribute industrial valves."),
        _result("pvf_relevant", False),
    )
    evaluated = evaluate_company(extracted)

    assert extracted.features["pvf_relevant"] is False
    assert evaluated.final_decision == "rejected"
    assert "confirmed_not_pvf_relevant" in evaluated.rejection_reasons


def test_boolean_relation_negative_binds_to_fact_proposition() -> None:
    extracted = apply_extraction(
        _company(),
        _bundle("We do not serve industrial customers."),
        _result("industrial_or_process_customer_focus", False),
    )

    assert extracted.features["industrial_or_process_customer_focus"] is False


def test_supported_facts_can_reach_accepted_after_canonicalization() -> None:
    supported_values: dict[str, Any] = {
        "pvf_relevant": True,
        "industrial_or_process_customer_focus": True,
        "branch_count": 5,
        "inside_sales_or_estimating_presence": True,
        "rfq_or_quote_workflow_evidence": True,
        "project_or_tender_business": True,
        "bom_or_line_item_complexity": True,
        "manufacturer_count_or_breadth": 20,
        "relevant_hiring": True,
        "employee_count": 50,
        "regional_independent_signal": True,
        "multi_location_signal": True,
        "known_current_direct_competitor_customer": False,
        "known_quote_automation_or_order_automation_relationship": False,
        "direct_quotation_pain_evidence": True,
        "manual_workflow_evidence": True,
        "explicit_process_bottleneck_evidence": True,
    }
    facts = {name: ExtractedFact(None, 0.0, []) for name in FACT_KEYS}
    for key, value in supported_values.items():
        facts[key] = ExtractedFact(value, 0.99, [_EVIDENCE_ID])
    result = ExtractionResult(
        company_id="cmp_support",
        model="deepseek-v4-flash",
        facts=facts,
        usage_event=UsageEvent(provider="deepseek", operation="structured_extraction"),
    )
    excerpt = (
        "Industrial pipe, valves, and fittings. "
        "We serve industrial customers. "
        "Branch count 5. Inside sales team. RFQ workflow. "
        "Project tender business. BOM line items. Manufacturer count 20. "
        "Relevant hiring. Employee count 50. Regional independent distributor. "
        "Multiple locations. No competitor customer. No quote automation. "
        "Direct quotation pain. Manual workflow. Process bottleneck."
    )

    extracted = apply_extraction(_company(), _bundle(excerpt), result)
    evaluated = evaluate_company(extracted)

    assert evaluated.final_decision == "accepted"


def test_nominal_positive_distribution_vetoes_negative_relevance() -> None:
    extracted = apply_extraction(
        _company(),
        _bundle("We do not sell pipe. We are a PVF distributor of valves."),
        _result("pvf_relevant", False),
    )
    evaluated = evaluate_company(extracted)

    assert extracted.features["pvf_relevant"] is None
    assert evaluated.final_decision == "uncertain"
    assert "confirmed_not_pvf_relevant" not in evaluated.rejection_reasons


def test_comma_coordinated_negative_pvf_relations_remain_canonical() -> None:
    extracted = apply_extraction(
        _company(),
        _bundle("We do not sell, distribute, or stock pipe."),
        _result("pvf_relevant", False),
    )
    evaluated = evaluate_company(extracted)

    assert extracted.features["pvf_relevant"] is False
    assert evaluated.final_decision == "rejected"
    assert "confirmed_not_pvf_relevant" in evaluated.rejection_reasons


def test_negative_pvf_relation_propagates_across_coordinated_object() -> None:
    extracted = apply_extraction(
        _company(),
        _bundle("We do not sell pipe or distribute valves."),
        _result("pvf_relevant", False),
    )
    evaluated = evaluate_company(extracted)

    assert extracted.features["pvf_relevant"] is False
    assert evaluated.final_decision == "rejected"
    assert "confirmed_not_pvf_relevant" in evaluated.rejection_reasons


def test_nominal_negative_distribution_remains_canonical() -> None:
    extracted = apply_extraction(
        _company(),
        _bundle("We are not a PVF distributor of valves."),
        _result("pvf_relevant", False),
    )
    evaluated = evaluate_company(extracted)

    assert extracted.features["pvf_relevant"] is False
    assert evaluated.final_decision == "rejected"
    assert "confirmed_not_pvf_relevant" in evaluated.rejection_reasons

