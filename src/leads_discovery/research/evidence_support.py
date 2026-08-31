"""Deterministic evidence support for extracted candidate facts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from leads_discovery.models import EvidenceBundle, ExtractedFact

_NEGATION: Final[re.Pattern[str]] = re.compile(
    r"\b(?:no|not|none|never|without|does\s+not|doesn't|don't|isn't|aren't|lacks?|lacking)\b",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY: Final[re.Pattern[str]] = re.compile(
    r"(?:[.!?;:\n]+|\b(?:but|however|whereas|while|yet)\b)",
    re.IGNORECASE,
)
_WORD: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9']+")


@dataclass(frozen=True, slots=True)
class PropositionSpec:
    """Describe the observable evidence vocabulary for one canonical fact."""

    concepts: tuple[str, ...]
    relations: tuple[str, ...] = ()


_PVF_RELATIONS: Final[tuple[str, ...]] = (
    "sell",
    "sells",
    "selling",
    "sold",
    "distribute",
    "distributes",
    "distributed",
    "distribution",
    "offer",
    "offers",
    "offering",
    "provide",
    "provides",
    "providing",
    "supply",
    "supplies",
    "supplying",
    "carry",
    "carries",
    "stock",
    "stocks",
)

_SPECS: Final[dict[str, PropositionSpec]] = {
    "pvf_relevant": PropositionSpec(
        ("pvf", "pipe", "piping", "valve", "valves", "fitting", "fittings"),
        _PVF_RELATIONS,
    ),
    "pvf_product_breadth": PropositionSpec(("pvf", "pipe", "valve", "fitting", "product")),
    "industrial_or_process_customer_focus": PropositionSpec(
        ("industrial", "process", "customer", "facility")
    ),
    "branch_count": PropositionSpec(("branch", "branches", "location", "locations")),
    "inside_sales_or_estimating_presence": PropositionSpec(
        ("inside sales", "estimating", "estimator", "estimators")
    ),
    "rfq_or_quote_workflow_evidence": PropositionSpec(
        ("rfq", "request for quote", "quote", "quotation")
    ),
    "project_or_tender_business": PropositionSpec(("project", "tender", "bid")),
    "bom_or_line_item_complexity": PropositionSpec(
        ("bom", "bill of materials", "line item", "line items")
    ),
    "manufacturer_count_or_breadth": PropositionSpec(
        ("manufacturer", "manufacturers", "brand", "brands", "line card")
    ),
    "relevant_hiring": PropositionSpec(("hiring", "job", "jobs", "career", "careers")),
    "employee_count": PropositionSpec(("employee", "employees", "staff", "headcount")),
    "revenue_if_reliably_available": PropositionSpec(("revenue", "sales")),
    "regional_independent_signal": PropositionSpec(("regional", "independent")),
    "multi_location_signal": PropositionSpec(("location", "locations", "branch", "branches")),
    "known_current_direct_competitor_customer": PropositionSpec(
        ("competitor", "customer", "customers")
    ),
    "known_competitor_evaluation_history": PropositionSpec(
        ("competitor", "evaluation", "evaluated", "pilot")
    ),
    "known_quote_automation_or_order_automation_relationship": PropositionSpec(
        ("quote automation", "order automation", "automation")
    ),
    "direct_quotation_pain_evidence": PropositionSpec(
        ("quote", "quotation", "rfq", "delay", "manual")
    ),
    "manual_workflow_evidence": PropositionSpec(
        ("manual", "spreadsheet", "email", "workflow")
    ),
    "explicit_process_bottleneck_evidence": PropositionSpec(
        ("bottleneck", "delay", "slow", "backlog", "process")
    ),
}


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    escaped = re.escape(phrase).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


def _matches(clause: str, phrases: tuple[str, ...]) -> tuple[re.Match[str], ...]:
    matches: list[re.Match[str]] = []
    for phrase in phrases:
        matches.extend(_phrase_pattern(phrase).finditer(clause))
    return tuple(sorted(matches, key=lambda item: item.start()))


def _word_distance(text: str, start: int, end: int) -> int:
    left, right = sorted((start, end))
    return len(_WORD.findall(text[left:right]))


def _locally_negated(clause: str, concepts: tuple[str, ...]) -> bool:
    concept_matches = _matches(clause, concepts)
    if not concept_matches:
        return False
    for negation in _NEGATION.finditer(clause):
        for concept in concept_matches:
            if _word_distance(clause, negation.end(), concept.start()) <= 6:
                return True
    return False


def _supports_pvf_boolean(clause: str, value: bool, spec: PropositionSpec) -> bool:
    targets = _matches(clause, spec.concepts)
    if not targets:
        return False
    if value:
        return not _locally_negated(clause, spec.concepts)
    relations = _matches(clause, spec.relations)
    if not relations:
        return False
    for negation in _NEGATION.finditer(clause):
        for relation in relations:
            if relation.start() < negation.end():
                continue
            if _word_distance(clause, negation.end(), relation.start()) > 5:
                continue
            for target in targets:
                if target.start() < relation.end():
                    continue
                if _word_distance(clause, relation.end(), target.start()) <= 6:
                    return True
    return False


def _value_is_stated(clause: str, value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        rendered = format(value, "g")
        return re.search(rf"(?<![\w.]){re.escape(rendered)}(?![\w.])", clause) is not None
    if isinstance(value, str):
        return _phrase_pattern(value.strip()).search(clause) is not None if value.strip() else False
    if isinstance(value, list):
        return bool(value) and all(
            isinstance(item, str)
            and bool(item.strip())
            and _phrase_pattern(item.strip()).search(clause) is not None
            for item in value
        )
    return False


def _supports_clause(key: str, fact: ExtractedFact, clause: str) -> bool:
    spec = _SPECS.get(key)
    if spec is None:
        return False
    value = fact.value
    if key == "pvf_relevant" and isinstance(value, bool):
        return _supports_pvf_boolean(clause, value, spec)
    concepts_present = bool(_matches(clause, spec.concepts))
    if not concepts_present:
        return False
    if isinstance(value, bool):
        negated = _locally_negated(clause, spec.concepts)
        return negated if value is False else not negated
    return _value_is_stated(clause, value)


def evidence_supports(key: str, fact: ExtractedFact, bundle: EvidenceBundle) -> bool:
    """Return whether cited retained evidence deterministically supports the proposition."""
    if fact.value is None:
        return True
    cited = set(fact.evidence_ids)
    if not cited:
        return False
    for item in bundle.items:
        if item.evidence_id not in cited:
            continue
        for text in (item.title, item.excerpt):
            if not text:
                continue
            for clause in _CLAUSE_BOUNDARY.split(text):
                if _supports_clause(key, fact, clause.casefold()):
                    return True
    return False


def canonicalize_supported_fact(
    key: str,
    fact: ExtractedFact,
    bundle: EvidenceBundle,
) -> ExtractedFact:
    """Convert unsupported non-null candidate facts to the repository's canonical unknown."""
    if evidence_supports(key, fact, bundle):
        return fact
    return ExtractedFact(value=None, confidence=0.0, evidence_ids=[])


__all__ = ["PropositionSpec", "canonicalize_supported_fact", "evidence_supports"]
