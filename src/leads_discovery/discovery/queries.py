"""Pure deterministic request planning for the bounded M2 discovery catalog."""

from __future__ import annotations

import math
from collections.abc import Sequence

from leads_discovery.models import CountryCode, DiscoveryRequest

_DEFAULT_MARKET = "PVF"
_DEFAULT_TARGET_GEOGRAPHIES: tuple[CountryCode, ...] = ("US", "CA")
_MAX_MARKET_LENGTH = 80
_MAX_SEARCH_TERMS = 5
_MAX_SEARCH_TERM_LENGTH = 120

_EXA_FAMILIES = (
    (
        "core-pvf",
        "Independent and regional distributors of pipe, valves, and fittings (PVF) "
        "serving industrial customers in {country}",
    ),
    (
        "process-flow",
        "Regional process piping, industrial valve, actuation, and flow-control "
        "distributors in {country}",
    ),
    (
        "project-rfq",
        "Industrial distributors in {country} that quote RFQs, BOMs, takeoffs, or "
        "project packages for pipe, valves, and fittings",
    ),
    (
        "line-card",
        "Independent distributors in {country} with multi-manufacturer line cards for "
        "industrial valves, pipe, fittings, or flow control",
    ),
    (
        "project-markets",
        "Regional PVF suppliers in {country} serving process plants, contractors, waterworks, "
        "energy, chemical, or industrial projects",
    ),
)
_APIFY_QUERIES: tuple[str, ...] = (
    "pipe valve fitting supplier",
    "industrial valve supplier",
    "industrial pipe and flow control supplier",
)
_COUNTRIES: tuple[tuple[CountryCode, str], ...] = (
    ("US", "the United States"),
    ("CA", "Canada"),
)
_COUNTRY_ALIASES: dict[str, CountryCode] = {
    "us": "US",
    "usa": "US",
    "united states": "US",
    "united states of america": "US",
    "ca": "CA",
    "can": "CA",
    "canada": "CA",
}


def _validate_text(name: str, value: object, maximum: int) -> str:
    """Validate one bounded human-entered search value without accepting control text."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be blank")
    if len(normalized) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    if any(not character.isprintable() for character in normalized):
        raise ValueError(f"{name} must not contain control characters")
    return normalized


def normalize_discovery_configuration(
    *,
    market: str = _DEFAULT_MARKET,
    search_terms: Sequence[str] = (),
    target_geographies: Sequence[str] = _DEFAULT_TARGET_GEOGRAPHIES,
) -> tuple[str, tuple[str, ...], tuple[CountryCode, ...]]:
    """Validate and normalize operator search criteria for bounded M2 discovery."""
    normalized_market = _validate_text("market", market, _MAX_MARKET_LENGTH)
    if isinstance(search_terms, str):
        raise ValueError("search_terms must be a sequence of strings")
    normalized_terms = tuple(
        _validate_text("search_terms item", term, _MAX_SEARCH_TERM_LENGTH)
        for term in search_terms
    )
    if len(normalized_terms) > _MAX_SEARCH_TERMS:
        raise ValueError(f"search_terms must contain at most {_MAX_SEARCH_TERMS} items")
    folded_terms = tuple(term.casefold() for term in normalized_terms)
    if len(set(folded_terms)) != len(folded_terms):
        raise ValueError("search_terms must not contain duplicates")

    if isinstance(target_geographies, str):
        raise ValueError("target_geographies must be a sequence of US/CA values")
    raw_geographies: list[str] = []
    for value in target_geographies:
        if not isinstance(value, str):
            raise ValueError("target_geographies must contain strings")
        raw_geographies.extend(value.split(","))
    normalized_set: set[CountryCode] = set()
    for value in raw_geographies:
        key = value.strip().casefold()
        country = _COUNTRY_ALIASES.get(key)
        if country is None:
            raise ValueError("target_geographies must contain only US or CA")
        if country in normalized_set:
            raise ValueError("target_geographies must not contain duplicates")
        normalized_set.add(country)
    normalized_geographies = tuple(
        country for country, _country_name in _COUNTRIES if country in normalized_set
    )
    if not normalized_geographies:
        raise ValueError("target_geographies must contain at least one geography")
    return normalized_market, normalized_terms, normalized_geographies


def _allocate(total: int, count: int) -> tuple[int, ...]:
    """Allocate a total with stable quotient/remainder distribution."""
    quotient, remainder = divmod(total, count)
    return tuple(quotient + (1 if index < remainder else 0) for index in range(count))


def build_discovery_requests(
    *,
    include_apify: bool,
    max_candidates: int = 100,
    apify_budget_usd: float = 0.25,
    market: str = _DEFAULT_MARKET,
    search_terms: Sequence[str] = (),
    target_geographies: Sequence[str] = _DEFAULT_TARGET_GEOGRAPHIES,
) -> tuple[DiscoveryRequest, ...]:
    """Build the complete bounded M2 discovery plan from safe operator criteria."""
    if (
        isinstance(max_candidates, bool)
        or not isinstance(max_candidates, int)
        or not 1 <= max_candidates <= 100
    ):
        raise ValueError("max_candidates must be an integer in 1..100")
    if (
        isinstance(apify_budget_usd, bool)
        or not isinstance(apify_budget_usd, (int, float))
        or not 0 <= apify_budget_usd <= 1
    ):
        raise ValueError("apify_budget_usd must be a number in 0..1")

    normalized_market, normalized_terms, countries = normalize_discovery_configuration(
        market=market,
        search_terms=search_terms,
        target_geographies=target_geographies,
    )

    apify_total = min(30, math.floor(max_candidates * 0.30))
    if not include_apify or apify_budget_usd == 0 or apify_total == 0:
        apify_total = 0
    exa_total = max_candidates - apify_total

    requests: list[DiscoveryRequest] = []
    exa_allocations = _allocate(exa_total, len(countries) * len(_EXA_FAMILIES))
    allocation_index = 0
    country_names = dict(_COUNTRIES)
    use_default_catalog = (
        normalized_market.casefold() == _DEFAULT_MARKET.casefold() and not normalized_terms
    )
    for country_code in countries:
        country_name = country_names[country_code]
        for family, template in _EXA_FAMILIES:
            total = exa_allocations[allocation_index]
            allocation_index += 1
            if total == 0:
                continue
            query = template.format(country=country_name)
            if not use_default_catalog:
                criteria = " ".join((normalized_market, *normalized_terms))
                query = f"{criteria} distributor prospects in {country_name}; {query}"
            requests.append(
                DiscoveryRequest(
                    request_id=f"exa:{country_code.lower()}:{family}:v1",
                    provider="exa",
                    query_family=family,
                    target_country_code=country_code,
                    queries=(query,),
                    max_results_per_query=total,
                    max_results_total=total,
                )
            )

    if apify_total:
        apify_allocations = _allocate(apify_total, len(countries))
        active_count = sum(1 for total in apify_allocations if total > 0)
        per_request_budget = apify_budget_usd / active_count
        apify_queries = _APIFY_QUERIES
        if not use_default_catalog:
            criteria = " ".join((normalized_market, *normalized_terms))
            apify_queries = tuple(f"{criteria} {query}" for query in _APIFY_QUERIES)
        for country_code, total in zip(countries, apify_allocations, strict=True):
            if total == 0:
                continue
            requests.append(
                DiscoveryRequest(
                    request_id=f"apify:{country_code.lower()}:maps-pvf:v1",
                    provider="apify",
                    query_family="maps-pvf",
                    target_country_code=country_code,
                    queries=apify_queries,
                    max_results_per_query=math.ceil(total / len(apify_queries)),
                    max_results_total=total,
                    max_cost_usd=per_request_budget,
                )
            )

    if sum(request.max_results_total for request in requests) != max_candidates:
        raise AssertionError("internal discovery allocation error")
    return tuple(requests)
