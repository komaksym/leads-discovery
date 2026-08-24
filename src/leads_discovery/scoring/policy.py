"""Deterministic M3 scoring, coverage, and precision-first decision policy."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final, Literal, cast

from leads_discovery.models import CompanyRecord, DecisionReason, FactValue

FinalDecision = Literal["accepted", "rejected", "uncertain"]
_SignalTransform = Literal[
    "positive_boolean",
    "inverted_boolean",
    "workload_branch_count",
    "economic_branch_count",
    "employee_count",
    "revenue_usd",
    "manufacturer_breadth",
]
_FactKind = Literal["boolean", "positive_integer", "positive_number", "manufacturer"]


@dataclass(frozen=True, slots=True)
class ScoringPolicy:
    """Configure the versioned M3 decision thresholds."""

    version: str = "m3-v1"
    minimum_fact_confidence: float = 0.60
    critical_relevance_confidence: float = 0.75
    hard_rejection_confidence: float = 0.85
    acceptance_score: float = 70.0
    minimum_overall_coverage: float = 0.70
    minimum_workload_coverage: float = 0.60
    minimum_economic_coverage: float = 0.50

    def __post_init__(self) -> None:
        """Reject blank versions and non-finite, boolean, or out-of-range thresholds."""
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("policy version must be a nonblank string")
        for name in (
            "minimum_fact_confidence",
            "critical_relevance_confidence",
            "hard_rejection_confidence",
            "minimum_overall_coverage",
            "minimum_workload_coverage",
            "minimum_economic_coverage",
        ):
            _validate_threshold(name, getattr(self, name), maximum=1.0)
        _validate_threshold("acceptance_score", self.acceptance_score, maximum=100.0)


@dataclass(frozen=True, slots=True)
class _FeatureRule:
    """Describe one versioned scored fact inside a category."""

    key: str
    weight: float
    transform: _SignalTransform


@dataclass(frozen=True, slots=True)
class _CategoryRule:
    """Describe one versioned score category and its product influence."""

    key: str
    product_weight: float
    features: tuple[_FeatureRule, ...]


@dataclass(frozen=True, slots=True)
class _UsableFact:
    """Hold one validated fact and its evidence-linked confidence."""

    value: FactValue
    confidence: float
    evidence_ids: tuple[str, ...]


_CATEGORY_RULES: Final[tuple[_CategoryRule, ...]] = (
    _CategoryRule(
        "workload",
        40.0,
        (
            _FeatureRule("rfq_or_quote_workflow_evidence", 25.0, "positive_boolean"),
            _FeatureRule("inside_sales_or_estimating_presence", 15.0, "positive_boolean"),
            _FeatureRule("project_or_tender_business", 15.0, "positive_boolean"),
            _FeatureRule("bom_or_line_item_complexity", 15.0, "positive_boolean"),
            _FeatureRule("manufacturer_count_or_breadth", 10.0, "manufacturer_breadth"),
            _FeatureRule("branch_count", 10.0, "workload_branch_count"),
            _FeatureRule("relevant_hiring", 5.0, "positive_boolean"),
            _FeatureRule("industrial_or_process_customer_focus", 5.0, "positive_boolean"),
        ),
    ),
    _CategoryRule(
        "economic_fit",
        25.0,
        (
            _FeatureRule("employee_count", 35.0, "employee_count"),
            _FeatureRule("branch_count", 25.0, "economic_branch_count"),
            _FeatureRule("multi_location_signal", 20.0, "positive_boolean"),
            _FeatureRule("regional_independent_signal", 15.0, "positive_boolean"),
            _FeatureRule("revenue_if_reliably_available", 5.0, "revenue_usd"),
        ),
    ),
    _CategoryRule(
        "low_incumbent_exposure",
        25.0,
        (
            _FeatureRule(
                "known_current_direct_competitor_customer",
                60.0,
                "inverted_boolean",
            ),
            _FeatureRule(
                "known_quote_automation_or_order_automation_relationship",
                40.0,
                "inverted_boolean",
            ),
        ),
    ),
    _CategoryRule(
        "direct_pain",
        10.0,
        (
            _FeatureRule("direct_quotation_pain_evidence", 40.0, "positive_boolean"),
            _FeatureRule("manual_workflow_evidence", 35.0, "positive_boolean"),
            _FeatureRule("explicit_process_bottleneck_evidence", 25.0, "positive_boolean"),
        ),
    ),
)

_FACT_KINDS: Final[Mapping[str, _FactKind]] = MappingProxyType(
    {
        "pvf_relevant": "boolean",
        "industrial_or_process_customer_focus": "boolean",
        "branch_count": "positive_integer",
        "inside_sales_or_estimating_presence": "boolean",
        "rfq_or_quote_workflow_evidence": "boolean",
        "project_or_tender_business": "boolean",
        "bom_or_line_item_complexity": "boolean",
        "manufacturer_count_or_breadth": "manufacturer",
        "relevant_hiring": "boolean",
        "employee_count": "positive_integer",
        "revenue_if_reliably_available": "positive_number",
        "regional_independent_signal": "boolean",
        "multi_location_signal": "boolean",
        "known_current_direct_competitor_customer": "boolean",
        "known_competitor_evaluation_history": "boolean",
        "known_quote_automation_or_order_automation_relationship": "boolean",
        "direct_quotation_pain_evidence": "boolean",
        "manual_workflow_evidence": "boolean",
        "explicit_process_bottleneck_evidence": "boolean",
    }
)

_REVIEW_EXPLANATIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "pvf_relevance_unresolved": (
            "PVF relevance is not positively resolved at the required confidence."
        ),
        "score_below_acceptance": (
            "The usable evidence score is below the acceptance threshold."
        ),
        "score_unavailable": "No scored category has usable evidence.",
        "low_overall_coverage": (
            "Overall usable evidence coverage is below the acceptance threshold."
        ),
        "low_workload_coverage": (
            "Workload evidence coverage is below the acceptance threshold."
        ),
        "low_economic_coverage": (
            "Economic-fit evidence coverage is below the acceptance threshold."
        ),
        "incumbent_exposure_unresolved": "No incumbent-exposure fact is usable.",
        "incumbent_exposure_ambiguous": (
            "Usable evidence indicates a possible incumbent relationship."
        ),
        "competitor_history_review": (
            "Public evidence indicates prior competitor evaluation history."
        ),
    }
)

_REJECTION_EXPLANATIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "confirmed_not_pvf_relevant": (
            "High-confidence cited evidence confirms the company is not PVF relevant."
        ),
        "confirmed_outside_us_canada": (
            "Canonical and retained discovery geography agree the company is outside "
            "the US and Canada."
        ),
        "confirmed_inactive_or_dead": (
            "Canonical company status is explicitly inactive or dead."
        ),
        "confirmed_current_direct_competitor_customer": (
            "High-confidence cited evidence confirms a current direct-competitor relationship."
        ),
        "confirmed_too_small_for_meaningful_quote_workload": (
            "High-confidence cited facts jointly confirm a very small company with no "
            "inside-sales or RFQ workflow signal."
        ),
    }
)


def _validate_threshold(name: str, value: object, *, maximum: float) -> None:
    """Validate one policy threshold without coercing booleans or non-finite values."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= float(value) <= maximum
    ):
        raise ValueError(f"{name} must be a finite number in 0..{maximum:g}")


DEFAULT_POLICY: Final[ScoringPolicy] = ScoringPolicy()


def _now() -> str:
    """Return a fresh UTC timestamp for the derived evaluation snapshot."""
    return datetime.now(UTC).isoformat()


def _valid_confidence(value: object) -> bool:
    """Return whether a fact confidence is a finite numeric value in 0..1."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and 0.0 <= float(value) <= 1.0
    )


def _value_supported(value: object, kind: _FactKind) -> bool:
    """Return whether a fact value has the exact key-specific supported type and range."""
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "positive_integer":
        return not isinstance(value, bool) and isinstance(value, int) and value > 0
    if kind == "positive_number":
        return (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and float(value) > 0.0
        )
    if kind == "manufacturer":
        if not isinstance(value, bool) and isinstance(value, int):
            return value >= 0
        if isinstance(value, list):
            return all(isinstance(item, str) for item in value)
        if isinstance(value, str):
            return value.casefold() in {"none", "narrow", "moderate", "broad"}
    return False


def _retained_evidence_ids(company: CompanyRecord) -> set[str]:
    """Validate retained evidence identity and return the citation lookup set."""
    ids: list[str] = []
    for item in company.evidence:
        if not isinstance(item.evidence_id, str) or not item.evidence_id:
            raise ValueError("company evidence IDs must be nonempty strings")
        ids.append(item.evidence_id)
    if len(ids) != len(set(ids)):
        raise ValueError("company evidence IDs must be unique")
    return set(ids)


def _invalid_reason(key: str) -> DecisionReason:
    """Build the stable review reason used for one malformed or unsupported fact."""
    return DecisionReason(
        code=f"invalid_fact:{key}",
        kind="review",
        explanation=(
            f"Fact {key!r} is malformed, unsupported, or has invalid citations."
        ),
    )


def _resolve_fact(
    company: CompanyRecord,
    key: str,
    kind: _FactKind,
    retained_ids: set[str],
    policy: ScoringPolicy,
) -> tuple[_UsableFact | None, DecisionReason | None]:
    """Resolve a cited fact as usable, unknown, or invalid without making unknown zero."""
    has_value = key in company.features
    has_meta = key in company.feature_confidence
    if not has_value and not has_meta:
        return None, None
    if not has_value or not has_meta:
        return None, _invalid_reason(key)

    value = company.features[key]
    raw_meta = company.feature_confidence[key]
    if not isinstance(raw_meta, dict):
        return None, _invalid_reason(key)
    meta = cast(dict[str, Any], raw_meta)
    if set(meta) != {"confidence", "evidence_ids"}:
        return None, _invalid_reason(key)
    confidence = meta.get("confidence")
    evidence_ids = meta.get("evidence_ids")
    if not _valid_confidence(confidence) or not isinstance(evidence_ids, list):
        return None, _invalid_reason(key)
    if any(not isinstance(item, str) for item in evidence_ids):
        return None, _invalid_reason(key)
    citations = cast(list[str], evidence_ids)
    if len(citations) != len(set(citations)):
        return None, _invalid_reason(key)

    score = float(cast(int | float, confidence))
    if value is None:
        if score == 0.0 and not citations:
            return None, None
        return None, _invalid_reason(key)
    if not citations or any(item not in retained_ids for item in citations):
        return None, _invalid_reason(key)
    if not _value_supported(value, kind):
        return None, _invalid_reason(key)
    if score < policy.minimum_fact_confidence:
        return None, None
    return _UsableFact(cast(FactValue, value), score, tuple(citations)), None


def _manufacturer_count(value: int | list[str]) -> int:
    """Convert manufacturer count/list input to the distinct breadth count."""
    if isinstance(value, int):
        return value
    return len({item.strip().casefold() for item in value if item.strip()})


def _count_signal(count: int) -> float:
    """Score manufacturer breadth by exact distinct-count boundaries."""
    if count == 0:
        return 0.0
    if count <= 4:
        return 25.0
    if count <= 9:
        return 50.0
    if count <= 19:
        return 75.0
    return 100.0


def _signal(value: FactValue, transform: _SignalTransform) -> float:
    """Transform one already validated fact into its exact 0..100 signal score."""
    if transform == "positive_boolean":
        return 100.0 if cast(bool, value) else 0.0
    if transform == "inverted_boolean":
        return 0.0 if cast(bool, value) else 100.0
    if transform == "manufacturer_breadth":
        if isinstance(value, str):
            categories = {
                "none": 0.0,
                "narrow": 25.0,
                "moderate": 60.0,
                "broad": 100.0,
            }
            return categories[value.casefold()]
        count = _manufacturer_count(cast(int | list[str], value))
        return _count_signal(count)

    number = cast(int | float, value)
    if transform == "workload_branch_count":
        count = cast(int, number)
        if count == 1:
            return 25.0
        if count <= 5:
            return 60.0
        if count <= 15:
            return 85.0
        return 100.0
    if transform == "economic_branch_count":
        count = cast(int, number)
        if count == 1:
            return 40.0
        if count <= 15:
            return 100.0
        if count <= 30:
            return 70.0
        return 50.0
    if transform == "employee_count":
        count = cast(int, number)
        if count < 10:
            return 20.0
        if count < 20:
            return 60.0
        if count <= 150:
            return 100.0
        if count <= 500:
            return 70.0
        return 50.0

    revenue = float(number)
    if revenue < 1_000_000:
        return 20.0
    if revenue < 5_000_000:
        return 50.0
    if revenue <= 100_000_000:
        return 100.0
    if revenue <= 500_000_000:
        return 70.0
    return 50.0


def _score_categories(
    facts: Mapping[str, _UsableFact | None],
) -> tuple[dict[str, float], dict[str, float], float | None]:
    """Compute raw category scores, category coverage, and the final score."""
    scores: dict[str, float] = {}
    coverage: dict[str, float] = {}
    numerator = 0.0
    effective_total = 0.0
    overall_numerator = 0.0
    product_total = 0.0

    for category in _CATEGORY_RULES:
        usable_weight = 0.0
        weighted_signal = 0.0
        configured_weight = sum(rule.weight for rule in category.features)
        for rule in category.features:
            fact = facts.get(rule.key)
            if fact is None:
                continue
            usable_weight += rule.weight
            weighted_signal += rule.weight * _signal(fact.value, rule.transform)
        category_coverage = usable_weight / configured_weight
        coverage[category.key] = category_coverage
        if usable_weight:
            score = weighted_signal / usable_weight
            scores[category.key] = score
            effective_weight = category.product_weight * category_coverage
            numerator += score * effective_weight
            effective_total += effective_weight
        overall_numerator += category.product_weight * category_coverage
        product_total += category.product_weight

    coverage["overall"] = overall_numerator / product_total
    final_score = None if effective_total == 0.0 else numerator / effective_total
    return scores, coverage, final_score


def _fact_review(code: str, fact: _UsableFact | None = None) -> DecisionReason:
    """Build a stable review reason with citations when a fact is resolved."""
    return DecisionReason(
        code=code,
        kind="review",
        explanation=_REVIEW_EXPLANATIONS[code],
        confidence=None if fact is None else fact.confidence,
        evidence_ids=[] if fact is None else list(fact.evidence_ids),
    )


def _fact_rejection(code: str, fact: _UsableFact) -> DecisionReason:
    """Build a stable fact-backed hard rejection with retained citations."""
    return DecisionReason(
        code=code,
        kind="rejection",
        explanation=_REJECTION_EXPLANATIONS[code],
        confidence=fact.confidence,
        evidence_ids=list(fact.evidence_ids),
    )


def _structural_rejection(code: str) -> DecisionReason:
    """Build a structural hard rejection from canonical/discovery provenance."""
    return DecisionReason(
        code=code,
        kind="rejection",
        explanation=_REJECTION_EXPLANATIONS[code],
    )


def _confirmed_outside_us_canada(company: CompanyRecord) -> bool:
    """Require canonical and retained discovery country codes to agree outside US/CA."""
    if not isinstance(company.country, str) or not company.country.strip():
        return False
    country = company.country.strip().upper()
    if country in {"US", "CA"}:
        return False
    for record in company.discovery_records:
        if not isinstance(record, dict):
            raise ValueError("discovery_records entries must be objects")
        raw_code = record.get("country_code")
        if isinstance(raw_code, str) and raw_code.strip().upper() == country:
            return True
    return False


def _small_company_rejection(
    facts: Mapping[str, _UsableFact | None],
    policy: ScoringPolicy,
) -> DecisionReason | None:
    """Return the exact four-fact high-confidence too-small rejection when complete."""
    keys = (
        "employee_count",
        "branch_count",
        "inside_sales_or_estimating_presence",
        "rfq_or_quote_workflow_evidence",
    )
    raw_facts = tuple(facts.get(key) for key in keys)
    if any(fact is None for fact in raw_facts):
        return None
    employee, branch, inside, rfq = cast(tuple[_UsableFact, ...], raw_facts)
    selected = (employee, branch, inside, rfq)
    if not all(
        fact.confidence >= policy.hard_rejection_confidence for fact in selected
    ):
        return None
    if not (
        cast(int, employee.value) < 10
        and branch.value == 1
        and inside.value is False
        and rfq.value is False
    ):
        return None
    citations = sorted(
        {
            evidence_id
            for fact in selected
            for evidence_id in fact.evidence_ids
        }
    )
    code = "confirmed_too_small_for_meaningful_quote_workload"
    return DecisionReason(
        code=code,
        kind="rejection",
        explanation=_REJECTION_EXPLANATIONS[code],
        confidence=min(fact.confidence for fact in selected),
        evidence_ids=citations,
    )


def _hard_rejections(
    company: CompanyRecord,
    facts: Mapping[str, _UsableFact | None],
    policy: ScoringPolicy,
) -> list[DecisionReason]:
    """Evaluate only the five exact high-confidence hard rejection rules."""
    reasons: list[DecisionReason] = []
    relevance = facts.get("pvf_relevant")
    if (
        relevance is not None
        and relevance.value is False
        and relevance.confidence >= policy.hard_rejection_confidence
    ):
        reasons.append(_fact_rejection("confirmed_not_pvf_relevant", relevance))
    if _confirmed_outside_us_canada(company):
        reasons.append(_structural_rejection("confirmed_outside_us_canada"))
    if company.status in {"inactive", "dead"}:
        reasons.append(_structural_rejection("confirmed_inactive_or_dead"))

    incumbent = facts.get("known_current_direct_competitor_customer")
    if (
        incumbent is not None
        and incumbent.value is True
        and incumbent.confidence >= policy.hard_rejection_confidence
    ):
        reasons.append(
            _fact_rejection("confirmed_current_direct_competitor_customer", incumbent)
        )
    small = _small_company_rejection(facts, policy)
    if small is not None:
        reasons.append(small)
    return reasons


def _incumbent_review(
    facts: Mapping[str, _UsableFact | None],
) -> DecisionReason | None:
    """Return the exact acceptance-gate review for unresolved or positive incumbency."""
    keys = (
        "known_current_direct_competitor_customer",
        "known_quote_automation_or_order_automation_relationship",
    )
    usable = [fact for key in keys if (fact := facts.get(key)) is not None]
    if not usable:
        return _fact_review("incumbent_exposure_unresolved")
    if not any(fact.value is True for fact in usable):
        return None
    cited = sorted({item for fact in usable for item in fact.evidence_ids})
    return DecisionReason(
        code="incumbent_exposure_ambiguous",
        kind="review",
        explanation=_REVIEW_EXPLANATIONS["incumbent_exposure_ambiguous"],
        confidence=max(fact.confidence for fact in usable),
        evidence_ids=cited,
    )


def _acceptance_reviews(
    facts: Mapping[str, _UsableFact | None],
    final_score: float | None,
    coverage: Mapping[str, float],
    policy: ScoringPolicy,
) -> list[DecisionReason]:
    """Return a review reason for every failed precision-first acceptance gate."""
    reasons: list[DecisionReason] = []
    relevance = facts.get("pvf_relevant")
    if (
        relevance is None
        or relevance.value is not True
        or relevance.confidence < policy.critical_relevance_confidence
    ):
        reasons.append(_fact_review("pvf_relevance_unresolved", relevance))
    if final_score is None:
        reasons.append(_fact_review("score_unavailable"))
    elif final_score < policy.acceptance_score:
        reasons.append(_fact_review("score_below_acceptance"))
    if coverage["overall"] < policy.minimum_overall_coverage:
        reasons.append(_fact_review("low_overall_coverage"))
    if coverage["workload"] < policy.minimum_workload_coverage:
        reasons.append(_fact_review("low_workload_coverage"))
    if coverage["economic_fit"] < policy.minimum_economic_coverage:
        reasons.append(_fact_review("low_economic_coverage"))
    incumbent = _incumbent_review(facts)
    if incumbent is not None:
        reasons.append(incumbent)
    return reasons


def evaluate_company(
    company: CompanyRecord,
    policy: ScoringPolicy = DEFAULT_POLICY,
) -> CompanyRecord:
    """Return a detached deterministic M3 evaluation for an extracted company."""
    if company.stage_status.get("extraction") != "completed":
        raise ValueError("company extraction stage must be completed before evaluation")
    updated = CompanyRecord.from_dict(company.to_dict())
    retained_ids = _retained_evidence_ids(updated)
    facts: dict[str, _UsableFact | None] = {}
    invalid: list[DecisionReason] = []
    for key, kind in _FACT_KINDS.items():
        fact, invalid_reason = _resolve_fact(updated, key, kind, retained_ids, policy)
        facts[key] = fact
        if invalid_reason is not None:
            invalid.append(invalid_reason)

    scores, raw_coverage, raw_final = _score_categories(facts)
    reviews = list(invalid)
    history = facts.get("known_competitor_evaluation_history")
    if history is not None and history.value is True:
        reviews.append(_fact_review("competitor_history_review", history))

    rejections = _hard_rejections(updated, facts, policy)
    if rejections:
        decision: FinalDecision = "rejected"
    else:
        gate_reviews = _acceptance_reviews(facts, raw_final, raw_coverage, policy)
        reviews.extend(gate_reviews)
        decision = "accepted" if not gate_reviews else "uncertain"

    updated.coverage = {
        key: round(value, 4) for key, value in raw_coverage.items()
    }
    updated.score_components = {
        key: round(value, 2) for key, value in scores.items()
    }
    updated.final_score = None if raw_final is None else round(raw_final, 2)
    updated.final_decision = decision
    updated.review_reasons = [reason.code for reason in reviews]
    updated.rejection_reasons = [reason.code for reason in rejections]
    updated.decision_reasons = reviews + rejections
    updated.evaluation_policy_version = policy.version
    updated.stage_status["scoring"] = "completed"
    updated.stage_status["decision"] = "completed"
    updated.updated_at = _now()
    return updated


def evaluate_companies(
    companies: Iterable[CompanyRecord],
    *,
    limit: int = 20,
    policy: ScoringPolicy = DEFAULT_POLICY,
) -> tuple[CompanyRecord, ...]:
    """Evaluate up to twenty completed records in stable company-ID order."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("limit must be an integer in 1..20")
    records = list(companies)
    ids = [record.company_id for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("company IDs must be unique")
    selected = sorted(
        (
            record
            for record in records
            if record.stage_status.get("extraction") == "completed"
        ),
        key=lambda record: record.company_id,
    )[:limit]
    return tuple(evaluate_company(record, policy) for record in selected)
