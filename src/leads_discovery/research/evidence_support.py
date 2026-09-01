"Deterministic evidence support for extracted candidate facts."

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from leads_discovery.models import EvidenceBundle, ExtractedFact

_CLAUSE_BOUNDARY: Final[re.Pattern[str]] = re.compile(
    r"(?:[.!?;\n]+|\b(?:but|however|whereas|while|yet)\b)",
    re.IGNORECASE,
)
_WORD: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9']+")
_DIRECT_CONCEPT_NEGATION: Final[re.Pattern[str]] = re.compile(
    r"(?:\bno\b|\bwithout\b|\bnot\b)\s+"
    r"(?:(?:a|an|any|the|our|their|its)\s+)?$",
    re.IGNORECASE,
)
_NEGATED_EXISTENCE: Final[re.Pattern[str]] = re.compile(
    r"(?:\b(?:do|does|did)\s+not|\b(?:don't|doesn't|didn't)|\bnever)\s+"
    r"(?:have|offer|provide|employ|operate|run|use|maintain)\s+"
    r"(?:(?:a|an|any|the|our|their|its)\s+)?$",
    re.IGNORECASE,
)
_NEGATED_RELATION_PREFIX: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    r"\b(?:do|does|did)\s+not|"
    r"\b(?:don't|doesn't|didn't)|"
    r"\bnever|"
    r"\b(?:is|are|was|were)\s+not|"
    r"\b(?:isn't|aren't|wasn't|weren't)"
    r")\s+"
    r"(?:(?:currently|actively|directly|typically|normally)\s+)?$",
    re.IGNORECASE,
)
_NO_RELATION_PREFIX: Final[re.Pattern[str]] = re.compile(
    r"(?:\bno\b|\bwithout\b)\s+$",
    re.IGNORECASE,
)
_NEGATED_COORDINATED_PVF_PREFIX: Final[re.Pattern[str]] = re.compile(
    r"(?:\b(?:do|does|did)\s+not|\b(?:don't|doesn't|didn't)|\bnever)\s+"
    r"(?:manufactur(?:e|es|ing)|install(?:s|ing)?|fabricat(?:e|es|ing)|"
    r"mak(?:e|es|ing)|produc(?:e|es|ing))"
    r"(?:\s+(?:a|an|any|the|industrial|process|pvf|carbon|stainless|steel|"
    r"pipe|piping|valves?|fittings?)){0,4}\s+(?:or|nor)\s+$",
    re.IGNORECASE,
)
_NEGATED_PREDICATE_PREFIX: Final[re.Pattern[str]] = re.compile(
    r"(?:\b(?:do|does|did)\s+not|\b(?:don't|doesn't|didn't)|\bnever)\s+"
    r"(?:[a-z][a-z'-]*\s+){1,3}$",
    re.IGNORECASE,
)
_RELATION_OBJECT_BRIDGE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:(?:a|an|any|the|industrial|process|pvf|carbon|stainless|steel)\s+)*"
    r"(?:of\s+)?$",
    re.IGNORECASE,
)
_ATTRIBUTE_BINDER: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:"
    r"[,=/\-–—:]\s*|"
    r"\b(?:count|number|total|value|level|breadth|of|is|are|was|were|"
    r"equals?|include|includes|including|comprise|comprises|comprising)\b\s*"
    r")*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _PropositionSpec:
    """Describe the observable evidence vocabulary for one canonical fact."""

    concepts: tuple[str, ...]
    relations: tuple[str, ...] = ()


_PVF_RELEVANCE_KEY: Final = "pvf_relevant"
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
_CUSTOMER_FOCUS_RELATIONS: Final[tuple[str, ...]] = (
    "serve",
    "serves",
    "serving",
    "served",
)

_SPECS: Final[dict[str, _PropositionSpec]] = {
    _PVF_RELEVANCE_KEY: _PropositionSpec(
        ("pvf", "pipe", "piping", "valve", "valves", "fitting", "fittings"),
        _PVF_RELATIONS,
    ),
    "pvf_product_breadth": _PropositionSpec(
        ("pvf", "pipe", "valve", "fitting", "product")
    ),
    "industrial_or_process_customer_focus": _PropositionSpec(
        ("industrial", "process", "customer", "facility"),
        _CUSTOMER_FOCUS_RELATIONS,
    ),
    "branch_count": _PropositionSpec(("branch", "branches", "location", "locations")),
    "inside_sales_or_estimating_presence": _PropositionSpec(
        ("inside sales", "estimating", "estimator", "estimators")
    ),
    "rfq_or_quote_workflow_evidence": _PropositionSpec(
        ("rfq", "request for quote", "quote", "quotation")
    ),
    "project_or_tender_business": _PropositionSpec(("project", "tender", "bid")),
    "bom_or_line_item_complexity": _PropositionSpec(
        ("bom", "bill of materials", "line item", "line items")
    ),
    "manufacturer_count_or_breadth": _PropositionSpec(
        ("manufacturer", "manufacturers", "brand", "brands", "line card")
    ),
    "relevant_hiring": _PropositionSpec(("hiring", "job", "jobs", "career", "careers")),
    "employee_count": _PropositionSpec(("employee", "employees", "staff", "headcount")),
    "revenue_if_reliably_available": _PropositionSpec(("revenue", "sales")),
    "regional_independent_signal": _PropositionSpec(("regional", "independent")),
    "multi_location_signal": _PropositionSpec(
        ("location", "locations", "branch", "branches")
    ),
    "known_current_direct_competitor_customer": _PropositionSpec(
        ("competitor", "customer", "customers")
    ),
    "known_competitor_evaluation_history": _PropositionSpec(
        ("competitor", "evaluation", "evaluated", "pilot")
    ),
    "known_quote_automation_or_order_automation_relationship": _PropositionSpec(
        ("quote automation", "order automation", "automation")
    ),
    "direct_quotation_pain_evidence": _PropositionSpec(
        ("quote", "quotation", "rfq", "delay", "manual")
    ),
    "manual_workflow_evidence": _PropositionSpec(
        ("manual", "spreadsheet", "email", "workflow")
    ),
    "explicit_process_bottleneck_evidence": _PropositionSpec(
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


def _concept_is_negated(clause: str, concept: re.Match[str]) -> bool:
    prefix = clause[: concept.start()]
    return (
        _DIRECT_CONCEPT_NEGATION.search(prefix) is not None
        or _NEGATED_EXISTENCE.search(prefix) is not None
    )


def _target_is_under_negated_predicate(clause: str, target: re.Match[str]) -> bool:
    prefix = clause[: target.start()]
    return (
        _concept_is_negated(clause, target)
        or _NEGATED_PREDICATE_PREFIX.search(prefix) is not None
    )


def _relation_is_directly_negated(clause: str, relation: re.Match[str]) -> bool:
    prefix = clause[: relation.start()]
    return (
        _NEGATED_RELATION_PREFIX.search(prefix) is not None
        or _NO_RELATION_PREFIX.search(prefix) is not None
        or _NEGATED_COORDINATED_PVF_PREFIX.search(prefix) is not None
    )


def _relation_is_negated(
    clause: str,
    relation: re.Match[str],
    relations: tuple[re.Match[str], ...],
) -> bool:
    if _relation_is_directly_negated(clause, relation):
        return True
    for previous in relations:
        if previous.start() >= relation.start():
            break
        if not _relation_is_directly_negated(clause, previous):
            continue
        bridge = clause[previous.end() : relation.start()]
        if re.fullmatch(r"\s*,?\s*(?:or|nor)\s+", bridge, re.IGNORECASE):
            return True
    return False


def _relation_concept_pairs(
    clause: str,
    spec: _PropositionSpec,
) -> tuple[
    tuple[re.Match[str], ...],
    tuple[re.Match[str], ...],
    tuple[tuple[re.Match[str], re.Match[str]], ...],
]:
    relations = _matches(clause, spec.relations)
    targets = _matches(clause, spec.concepts)
    pairs: list[tuple[re.Match[str], re.Match[str]]] = []
    for target in targets:
        preceding = [relation for relation in relations if relation.end() <= target.start()]
        if not preceding:
            continue
        relation = preceding[-1]
        bridge = clause[relation.end() : target.start()]
        if _RELATION_OBJECT_BRIDGE.fullmatch(bridge):
            pairs.append((relation, target))
    return relations, targets, tuple(pairs)


def _supports_explicit_positive_pvf_clause(
    clause: str,
    spec: _PropositionSpec,
) -> bool:
    relations, _targets, pairs = _relation_concept_pairs(clause, spec)
    return any(
        not _relation_is_negated(clause, relation, relations)
        for relation, _target in pairs
    )


def _supports_positive_pvf_clause(clause: str, spec: _PropositionSpec) -> bool:
    relations, targets, pairs = _relation_concept_pairs(clause, spec)
    if relations:
        return any(
            not _relation_is_negated(clause, relation, relations)
            for relation, _target in pairs
        )
    return any(
        not _target_is_under_negated_predicate(clause, target) for target in targets
    )


def _supports_negative_pvf_clause(clause: str, spec: _PropositionSpec) -> bool:
    relations, _targets, pairs = _relation_concept_pairs(clause, spec)
    return any(
        _relation_is_negated(clause, relation, relations)
        for relation, _target in pairs
    )


def _value_matches(clause: str, value: object) -> tuple[re.Match[str], ...]:
    if isinstance(value, bool) or value is None:
        return ()
    if isinstance(value, (int, float)):
        rendered = format(value, "g")
        pattern = re.compile(rf"(?<![\w.]){re.escape(rendered)}(?![\w.])")
        return tuple(pattern.finditer(clause))
    if isinstance(value, str):
        if not value.strip():
            return ()
        return tuple(_phrase_pattern(value.strip()).finditer(clause))
    if isinstance(value, list):
        if not value or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            return ()
        all_matches: list[re.Match[str]] = []
        for item in value:
            matches = tuple(_phrase_pattern(item.strip()).finditer(clause))
            if not matches:
                return ()
            all_matches.append(matches[0])
        return tuple(sorted(all_matches, key=lambda item: item.start()))
    return ()


def _value_binds_to_concept(
    clause: str,
    value: object,
    spec: _PropositionSpec,
) -> bool:
    concepts = _matches(clause, spec.concepts)
    values = _value_matches(clause, value)
    if not concepts or not values:
        return False
    for concept in concepts:
        for value_match in values:
            if value_match.end() <= concept.start():
                bridge = clause[value_match.end() : concept.start()]
                if not _WORD.search(bridge):
                    return True
            elif concept.end() <= value_match.start():
                bridge = clause[concept.end() : value_match.start()]
                if _ATTRIBUTE_BINDER.fullmatch(bridge):
                    return True
    return False


def _supports_boolean_clause(
    clause: str,
    value: bool,
    spec: _PropositionSpec,
) -> bool:
    concepts = _matches(clause, spec.concepts)
    if not concepts:
        return False
    if spec.relations:
        relations, _targets, pairs = _relation_concept_pairs(clause, spec)
        if relations:
            if value:
                return any(
                    not _relation_is_negated(clause, relation, relations)
                    for relation, _target in pairs
                )
            return any(
                _relation_is_negated(clause, relation, relations)
                for relation, _target in pairs
            )
    if value:
        return any(not _concept_is_negated(clause, concept) for concept in concepts)
    return any(_concept_is_negated(clause, concept) for concept in concepts)


def _supports_clause(key: str, fact: ExtractedFact, clause: str) -> bool:
    spec = _SPECS.get(key)
    if spec is None:
        return False
    value = fact.value
    if key == _PVF_RELEVANCE_KEY and isinstance(value, bool):
        if value:
            return _supports_positive_pvf_clause(clause, spec)
        return _supports_negative_pvf_clause(clause, spec)
    if isinstance(value, bool):
        return _supports_boolean_clause(clause, value, spec)
    return _value_binds_to_concept(clause, value, spec)


def evidence_supports(key: str, fact: ExtractedFact, bundle: EvidenceBundle) -> bool:
    """Return whether cited retained evidence deterministically supports the proposition."""
    if fact.value is None:
        return True
    cited = set(fact.evidence_ids)
    if not cited:
        return False

    support_found = False
    pvf_negative = key == _PVF_RELEVANCE_KEY and fact.value is False
    for item in bundle.items:
        if item.evidence_id not in cited:
            continue
        for text in (item.title, item.excerpt):
            if not text:
                continue
            for clause in _CLAUSE_BOUNDARY.split(text):
                normalized = clause.casefold()
                if pvf_negative:
                    spec = _SPECS[_PVF_RELEVANCE_KEY]
                    if _supports_explicit_positive_pvf_clause(normalized, spec):
                        return False
                    if _supports_negative_pvf_clause(normalized, spec):
                        support_found = True
                elif _supports_clause(key, fact, normalized):
                    return True
    return support_found


def canonicalize_supported_fact(
    key: str,
    fact: ExtractedFact,
    bundle: EvidenceBundle,
) -> ExtractedFact:
    """Convert unsupported non-null candidate facts to the canonical unknown."""
    if evidence_supports(key, fact, bundle):
        return fact
    return ExtractedFact(value=None, confidence=0.0, evidence_ids=[])


__all__ = ["canonicalize_supported_fact", "evidence_supports"]
