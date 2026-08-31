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


def test_positive_pvf_claim_requires_positive_sales_relation() -> None:
    supported = apply_extraction(
        _company(),
        _bundle("We distribute industrial valves and fittings."),
        _result("pvf_relevant", True),
    )
    unsupported = apply_extraction(
        _company(),
        _bundle("We install industrial valves for customers."),
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
