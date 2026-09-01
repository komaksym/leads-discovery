"Deterministic evidence support for extracted candidate facts."

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from leads_discovery.models import EvidenceBundle, ExtractedFact, FactValue

_EXPLICIT_NEGATION: Final[re.Pattern[str]] = re.compile(
    r"\b(no|not|none|never|without|does\s+not|doesn't|don't|isn't|aren't|lacks?|lacking)\b",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY: Final[re.Pattern[str]] = re.compile(
    r"(?:[.!?;:\n]+|\b(?:and|but|however|whereas|while|yet)\b)",
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
_NEGATED_NOMINAL_RELATION_PREFIX: Final[re.Pattern[str]] = re.compile(
    r"\b(?:is|are|was|were)\s+not\s+"
    r"(?:(?:a|an|any|the|our|their|its)\s+)?"
    r"(?:(?:industrial|process|pvf|carbon|stainless|steel)\s+)*$",
    re.IGNORECASE,
)
_NO_RELATION_PREFIX: Final[re.Pattern[str]] = re.compile(
    r"(?:\bno\b|\bwithout\b)\s+$",
    re.IGNORECASE,
)
_NEGATED_RELATION_COORDINATION: Final[re.Pattern[str]] = re.compile(
    r"\s*(?:,\s*(?:(?:or|nor)\s+)?|(?:or|nor)\s+)",
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


@dataclass(frozen=True, slots=True)
class _RelationMatches:
    """Bundle relation matching state so negation decisions stay internally consistent."""

    relations: tuple[re.Match[str], ...]
    targets: tuple[re.Match[str], ...]
    pairs: tuple[tuple[re.Match[str], re.Match[str]], ...]


_FACT_SUPPORT_TERMS: dict[str, tuple[str, ...]] = {
    "pvf_relevant": ("pvf", "pipe", "piping", "valve", "valves", "fitting", "fittings"),
    "pvf_product_breadth": (
        "pvf",
        "pipe",
        "piping",
        "valve",
        "valves",
        "fitting",
        "fittings",
        "product",
    ),
    "industrial_or_process_customer_focus": ("industrial", "process", "plant", "contractor"),
    "branch_count": ("branch", "location", "facility"),
    "inside_sales_or_estimating_presence": ("inside sales", "estimating", "estimator"),
    "rfq_or_quote_workflow_evidence": ("rfq", "request for quote", "quotation", "quote"),
    "project_or_tender_business": ("project", "tender", "bid"),
    "bom_or_line_item_complexity": ("bom", "bill of materials", "line item"),
    "manufacturer_count_or_breadth": ("manufacturer", "line card", "brand"),
    "relevant_hiring": ("hiring", "career", "job", "position"),
    "employee_count": ("employee", "staff", "team"),
    "revenue_if_reliably_available": ("revenue", "sales", "turnover"),
    "regional_independent_signal": ("regional", "independent", "family-owned"),
    "multi_location_signal": ("location", "branch", "office"),
    "known_current_direct_competitor_customer": ("competitor", "customer", "client"),
    "known_competitor_evaluation_history": ("competitor", "evaluation", "evaluated"),
    "known_quote_automation_or_order_automation_relationship": (
        "automation",
        "quote",
        "order",
    ),
    "direct_quotation_pain_evidence": ("quotation", "quote", "rfq"),
    "manual_workflow_evidence": ("manual", "spreadsheet", "workflow"),
    "explicit_process_bottleneck_evidence": ("bottleneck", "delay", "process"),
}
_NUMERIC_FACT_UNITS: dict[str, tuple[str, ...]] = {
    "branch_count": ("branch", "branches", "location", "locations", "facility", "facilities"),
    "employee_count": ("employee", "employees", "staff", "people", "team members"),
    "manufacturer_count_or_breadth": ("manufacturer", "manufacturers", "brand", "brands"),
}
_FACT_POSITIVE_PREDICATES: dict[str, tuple[tuple[str, ...], ...]] = {
    "pvf_relevant": (
        (
            r"\b(?:sell|sells|selling|distribut(?:e|es|ing)|offer(?:s|ing)?|"
            r"supply|supplies|stock(?:s|ing)?|carry|carries)\b",
        ),
        (r"\b(?:pvf|pipe|piping|valves?|fittings?)\b",),
    ),
    "pvf_product_breadth": (
        (r"\b(?:pipe|piping)\b",),
        (r"\bvalves?\b",),
        (r"\bfittings?\b",),
    ),
    "industrial_or_process_customer_focus": (
        (
            r"\b(?:serv(?:e|es|ing)|supply|supplies|sell(?:s|ing)|"
            r"support(?:s|ing))\s+(?:industrial|process)\b",
            r"\b(?:industrial|process)\s+(?:customers|clients|markets|contractors|plants)\b",
        ),
    ),
    "branch_count": ((r"\b(?:branch|location|facility)\b",),),
    "inside_sales_or_estimating_presence": (
        (r"\binside sales\b", r"\bestimat(?:ing|or|ors)\b"),
    ),
    "rfq_or_quote_workflow_evidence": (
        (r"\b(?:rfq|request for quote|quotation|quote)\b",),
    ),
    "project_or_tender_business": ((r"\b(?:projects?|tenders?|bids?)\b",),),
    "bom_or_line_item_complexity": (
        (r"\b(?:bom|bill of materials|line items?)\b",),
    ),
    "manufacturer_count_or_breadth": (
        (r"\b(?:manufacturers?|line card|brands?)\b",),
    ),
    "relevant_hiring": (
        (r"\b(?:hiring|careers?|open positions?|job openings?)\b",),
    ),
    "employee_count": ((r"\b(?:employees?|staff|team members?)\b",),),
    "revenue_if_reliably_available": ((r"\b(?:revenue|sales|turnover)\b",),),
    "regional_independent_signal": (
        (r"\b(?:regional|independent|family-owned)\b",),
    ),
    "multi_location_signal": (
        (r"\b(?:multi-location|multiple locations?|several locations?)\b",),
    ),
    "known_current_direct_competitor_customer": (
        (r"\bcompetitor\b",),
        (r"\b(?:customer|client)\b",),
    ),
    "known_competitor_evaluation_history": (
        (r"\bcompetitor\b",),
        (r"\b(?:evaluat(?:ed|ion)|assessment)\b",),
    ),
    "known_quote_automation_or_order_automation_relationship": (
        (r"\b(?:quote|quotation|order)\b",),
        (r"\bautomation\b",),
    ),
    "direct_quotation_pain_evidence": (
        (r"\b(?:quote|quotation|rfq|request for quote)\b",),
        (r"\b(?:pain|problem|delay|slow|manual|bottleneck|inefficien)\w*\b",),
    ),
    "manual_workflow_evidence": (
        (r"\b(?:manual workflow|manual process|spreadsheet(?:s)? for)\b",),
    ),
    "explicit_process_bottleneck_evidence": (
        (r"\b(?:process|workflow)\b",),
        (r"\b(?:bottleneck|delay|slow|inefficien)\w*\b",),
    ),
}

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
    "distributor",
    "distributors",
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

if set(_FACT_SUPPORT_TERMS) != set(_SPECS):
    raise RuntimeError("every extracted fact must declare deterministic evidence-support terms")
if set(_FACT_POSITIVE_PREDICATES) != set(_SPECS):
    raise RuntimeError("every extracted fact must declare an affirmative evidence predicate")



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
        or _NEGATED_NOMINAL_RELATION_PREFIX.search(prefix) is not None
        or _NO_RELATION_PREFIX.search(prefix) is not None
        or _NEGATED_COORDINATED_PVF_PREFIX.search(prefix) is not None
    )


def _same_match(left: re.Match[str], right: re.Match[str]) -> bool:
    return left.start() == right.start() and left.end() == right.end()


def _relation_is_negated(
    clause: str,
    relation: re.Match[str],
    matches: _RelationMatches,
) -> bool:
    if _relation_is_directly_negated(clause, relation):
        return True

    relation_index = next(
        (
            index
            for index, candidate in enumerate(matches.relations)
            if _same_match(candidate, relation)
        ),
        None,
    )
    if relation_index is None or relation_index == 0:
        return False

    previous = matches.relations[relation_index - 1]
    bridge = clause[previous.end() : relation.start()]
    coordinated = _NEGATED_RELATION_COORDINATION.fullmatch(bridge) is not None
    if not coordinated:
        coordinated = any(
            _same_match(paired_relation, previous)
            and target.end() <= relation.start()
            and _NEGATED_RELATION_COORDINATION.fullmatch(
                clause[target.end() : relation.start()]
            )
            is not None
            for paired_relation, target in matches.pairs
        )
    if not coordinated:
        return False
    return _relation_is_negated(clause, previous, matches)


def _relation_concept_pairs(
    clause: str,
    spec: _PropositionSpec,
) -> _RelationMatches:
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
    return _RelationMatches(relations=relations, targets=targets, pairs=tuple(pairs))


def _relation_pairs_support_value(
    clause: str,
    matches: _RelationMatches,
    *,
    value: bool,
) -> bool:
    """Return whether relation/concept pairs support the requested boolean value."""
    if value:
        return any(
            not _relation_is_negated(clause, relation, matches)
            for relation, _target in matches.pairs
        )
    return any(
        _relation_is_negated(clause, relation, matches)
        for relation, _target in matches.pairs
    )


def _supports_explicit_positive_pvf_clause(
    clause: str,
    spec: _PropositionSpec,
) -> bool:
    matches = _relation_concept_pairs(clause, spec)
    return _relation_pairs_support_value(
        clause,
        matches,
        value=True,
    )


def _supports_positive_pvf_clause(clause: str, spec: _PropositionSpec) -> bool:
    matches = _relation_concept_pairs(clause, spec)
    if matches.relations:
        return _relation_pairs_support_value(
            clause,
            matches,
            value=True,
        )
    return any(
        not _target_is_under_negated_predicate(clause, target)
        for target in matches.targets
    )


def _supports_negative_pvf_clause(clause: str, spec: _PropositionSpec) -> bool:
    matches = _relation_concept_pairs(clause, spec)
    return _relation_pairs_support_value(
        clause,
        matches,
        value=False,
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
        matches = _relation_concept_pairs(clause, spec)
        if matches.relations:
            return _relation_pairs_support_value(
                clause,
                matches,
                value=value,
            )
    if value:
        return any(not _concept_is_negated(clause, concept) for concept in concepts)
    return any(_concept_is_negated(clause, concept) for concept in concepts)


def _negative_term_is_local(clause: str, term: str) -> bool:
    """Return true only when a negation and target term are locally connected in one clause."""
    term_pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
    targets = tuple(term_pattern.finditer(clause))
    if not targets:
        return False
    for negation in _EXPLICIT_NEGATION.finditer(clause):
        for target in targets:
            start = min(negation.end(), target.end())
            end = max(negation.start(), target.start())
            between = clause[start:end]
            if len(re.findall(r"\b[\w']+\b", between)) <= 4:
                return True
    return False



def _positive_predicate_supports_fact(key: str, clause: str) -> bool:
    """Require all fact-specific affirmative predicate groups in one cited clause."""
    return all(
        any(re.search(pattern, clause, re.IGNORECASE) for pattern in alternatives)
        for alternatives in _FACT_POSITIVE_PREDICATES[key]
    )


def _value_is_explicitly_supported(key: str, value: FactValue, clause: str) -> bool:
    """Require scalar/list values to be stated by the cited proposition, not merely nearby."""
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        spec = _SPECS.get(key)
        return _numeric_value_has_unit(key, str(value), clause) or (
            spec is not None and _value_binds_to_concept(clause, value, spec)
        )
    if isinstance(value, str):
        return value.casefold() in clause
    if isinstance(value, list):
        return bool(value) and all(item.casefold() in clause for item in value)
    return False


def _numeric_value_has_unit(key: str, value: str, clause: str) -> bool:
    """Require a numeric value to be directly attached to its fact's domain unit."""
    if key == "revenue_if_reliably_available":
        normalized = clause.replace(",", "")
        number = rf"(?<![\d.]){re.escape(value)}(?![\d.])"
        revenue = r"(?:revenue|sales|turnover)"
        currency = r"(?:\$|usd\s*)?"
        return re.search(
            rf"(?:{revenue}\s*(?:of|:|was|is)?\s*{currency}{number}|"
            rf"{currency}{number}\s*(?:in\s+)?{revenue})",
            normalized,
            re.IGNORECASE,
        ) is not None
    units = _NUMERIC_FACT_UNITS.get(key)
    if units is None:
        return False
    number = rf"(?<![\d.]){re.escape(value)}(?![\d.])"
    unit = "|".join(re.escape(item) for item in units)
    return re.search(rf"{number}\s+(?:{unit})\b", clause, re.IGNORECASE) is not None



def _supports_clause(key: str, fact: ExtractedFact, clause: str) -> bool:
    value = fact.value
    if key == _PVF_RELEVANCE_KEY and isinstance(value, bool):
        spec = _SPECS[_PVF_RELEVANCE_KEY]
        if value:
            return _supports_positive_pvf_clause(clause, spec)
        return _supports_negative_pvf_clause(clause, spec)

    terms = _FACT_SUPPORT_TERMS.get(key)
    if terms is None or key not in _FACT_POSITIVE_PREDICATES:
        return False
    if value is False:
        return _supports_boolean_clause(clause, False, _SPECS[key])
    return (
        any(
            re.search(rf"\b{re.escape(term)}\b", clause, re.IGNORECASE)
            for term in terms
        )
        and not any(_negative_term_is_local(clause, term) for term in terms)
        and _positive_predicate_supports_fact(key, clause)
        and _value_is_explicitly_supported(key, value, clause)
    )


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
