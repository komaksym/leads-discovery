"""Pure deterministic request planning for the bounded M2 discovery catalog."""

from __future__ import annotations

import math

from leads_discovery.models import CountryCode, DiscoveryRequest

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
_APIFY_QUERIES = (
    "pipe valve fitting supplier",
    "industrial valve supplier",
    "industrial pipe and flow control supplier",
)
_COUNTRIES: tuple[tuple[CountryCode, str], ...] = (
    ("US", "the United States"),
    ("CA", "Canada"),
)


def _allocate(total: int, count: int) -> tuple[int, ...]:
    """Allocate a total with stable quotient/remainder distribution."""
    quotient, remainder = divmod(total, count)
    return tuple(quotient + (1 if index < remainder else 0) for index in range(count))


def build_discovery_requests(
    *,
    include_apify: bool,
    max_candidates: int = 100,
    apify_budget_usd: float = 0.25,
) -> tuple[DiscoveryRequest, ...]:
    """Build the complete bounded M2 discovery plan."""
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

    apify_total = min(30, math.floor(max_candidates * 0.30))
    if not include_apify or apify_budget_usd == 0 or apify_total == 0:
        apify_total = 0
    exa_total = max_candidates - apify_total

    requests: list[DiscoveryRequest] = []
    exa_allocations = _allocate(exa_total, len(_COUNTRIES) * len(_EXA_FAMILIES))
    allocation_index = 0
    for country_code, country_name in _COUNTRIES:
        for family, template in _EXA_FAMILIES:
            total = exa_allocations[allocation_index]
            allocation_index += 1
            if total == 0:
                continue
            query = template.format(country=country_name)
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
        apify_allocations = _allocate(apify_total, len(_COUNTRIES))
        active_count = sum(1 for total in apify_allocations if total > 0)
        per_request_budget = apify_budget_usd / active_count
        for (country_code, _), total in zip(_COUNTRIES, apify_allocations, strict=True):
            if total == 0:
                continue
            requests.append(
                DiscoveryRequest(
                    request_id=f"apify:{country_code.lower()}:maps-pvf:v1",
                    provider="apify",
                    query_family="maps-pvf",
                    target_country_code=country_code,
                    queries=_APIFY_QUERIES,
                    max_results_per_query=math.ceil(total / len(_APIFY_QUERIES)),
                    max_results_total=total,
                    max_cost_usd=per_request_budget,
                )
            )

    if sum(request.max_results_total for request in requests) != max_candidates:
        raise AssertionError("internal discovery allocation error")
    return tuple(requests)
