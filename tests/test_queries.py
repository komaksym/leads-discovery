"""Contract tests for deterministic M2 discovery request planning."""

from __future__ import annotations

from typing import Any, cast

import pytest

from leads_discovery.discovery.queries import build_discovery_requests

EXA_FAMILIES = (
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
        "Regional PVF suppliers in {country} serving process plants, contractors, "
        "waterworks, energy, chemical, or industrial projects",
    ),
)
MAPS_TERMS = (
    "pipe valve fitting supplier",
    "industrial valve supplier",
    "industrial pipe and flow control supplier",
)
COUNTRIES = (("US", "us", "the United States"), ("CA", "ca", "Canada"))


def _expected_exa_ids() -> list[str]:
    """Return the exact ordered Exa request IDs from the frozen catalog."""
    return [
        f"exa:{country_slug}:{family}:v1"
        for _country_code, country_slug, _country_name in COUNTRIES
        for family, _query in EXA_FAMILIES
    ]


def test_exa_only_default_catalog_is_exact_and_bounded() -> None:
    """The default Exa-only plan is ten ordered requests of ten rows each."""
    requests = build_discovery_requests(include_apify=False)

    assert [request.request_id for request in requests] == _expected_exa_ids()
    assert len(requests) == 10
    assert sum(request.max_results_total for request in requests) == 100
    assert all(request.provider == "exa" for request in requests)
    assert all(request.max_results_total == 10 for request in requests)
    assert all(request.max_results_per_query == 10 for request in requests)
    assert all(request.max_cost_usd is None for request in requests)

    expected_queries = [
        query.format(country=country_name)
        for _country_code, _country_slug, country_name in COUNTRIES
        for _family, query in EXA_FAMILIES
    ]
    assert [request.queries[0] for request in requests] == expected_queries
    assert [request.target_country_code for request in requests] == ["US"] * 5 + ["CA"] * 5
    assert not any("Mexico" in query for request in requests for query in request.queries)


def test_combined_default_plan_is_exact_70_30_split() -> None:
    """The default combined plan gives Exa 70 rows and Apify 30 under a $0.25 cap."""
    requests = build_discovery_requests(include_apify=True)

    assert [request.request_id for request in requests[:10]] == _expected_exa_ids()
    assert [request.request_id for request in requests[10:]] == [
        "apify:us:maps-pvf:v1",
        "apify:ca:maps-pvf:v1",
    ]
    assert [request.max_results_total for request in requests[:10]] == [7] * 10
    assert [request.max_results_total for request in requests[10:]] == [15, 15]
    assert [request.max_results_per_query for request in requests[10:]] == [5, 5]
    assert [request.queries for request in requests[10:]] == [MAPS_TERMS, MAPS_TERMS]
    assert [request.max_cost_usd for request in requests[10:]] == [0.125, 0.125]
    assert sum(request.max_results_total for request in requests) == 100


def test_zero_apify_budget_allocates_every_row_to_exa() -> None:
    """A zero Apify budget disables Apify rather than dropping candidate capacity."""
    requests = build_discovery_requests(
        include_apify=True,
        max_candidates=100,
        apify_budget_usd=0,
    )

    assert [request.request_id for request in requests] == _expected_exa_ids()
    assert [request.max_results_total for request in requests] == [10] * 10
    assert sum(request.max_results_total for request in requests) == 100


@pytest.mark.parametrize(
    ("max_candidates", "expected_ids", "expected_totals"),
    [
        (1, ["exa:us:core-pvf:v1"], [1]),
        (
            3,
            ["exa:us:core-pvf:v1", "exa:us:process-flow:v1", "exa:us:project-rfq:v1"],
            [1, 1, 1],
        ),
        (
            4,
            [
                "exa:us:core-pvf:v1",
                "exa:us:process-flow:v1",
                "exa:us:project-rfq:v1",
                "apify:us:maps-pvf:v1",
            ],
            [1, 1, 1, 1],
        ),
        (
            11,
            _expected_exa_ids()[:8] + ["apify:us:maps-pvf:v1", "apify:ca:maps-pvf:v1"],
            [1] * 8 + [2, 1],
        ),
    ],
)
def test_small_totals_use_stable_quotient_remainder_allocation(
    max_candidates: int,
    expected_ids: list[str],
    expected_totals: list[int],
) -> None:
    """Small totals retain catalog order and omit only zero-allocation requests."""
    requests = build_discovery_requests(include_apify=True, max_candidates=max_candidates)

    assert [request.request_id for request in requests] == expected_ids
    assert [request.max_results_total for request in requests] == expected_totals
    assert sum(request.max_results_total for request in requests) == max_candidates


def test_single_active_apify_country_receives_the_supplied_budget() -> None:
    """When only one Apify country has rows, it receives the entire supplied cap."""
    requests = build_discovery_requests(
        include_apify=True,
        max_candidates=4,
        apify_budget_usd=0.25,
    )
    apify_request = requests[-1]

    assert apify_request.request_id == "apify:us:maps-pvf:v1"
    assert apify_request.max_results_total == 1
    assert apify_request.max_results_per_query == 1
    assert apify_request.max_cost_usd == 0.25


def test_explicit_one_dollar_apify_maximum_is_split_but_never_increased() -> None:
    """The explicit $1.00 aggregate maximum becomes two $0.50 country caps."""
    requests = build_discovery_requests(include_apify=True, apify_budget_usd=1.0)
    apify_requests = [request for request in requests if request.provider == "apify"]

    assert [request.max_cost_usd for request in apify_requests] == [0.5, 0.5]
    assert sum(request.max_cost_usd or 0 for request in apify_requests) == 1.0


@pytest.mark.parametrize("max_candidates", [0, -1, 101])
def test_invalid_candidate_boundaries_fail(max_candidates: int) -> None:
    """Candidate totals outside 1..100 fail before any provider can be involved."""
    with pytest.raises((TypeError, ValueError)):
        build_discovery_requests(include_apify=True, max_candidates=max_candidates)


@pytest.mark.parametrize("budget", [-0.001, 1.001])
def test_invalid_apify_budget_boundaries_fail(budget: float) -> None:
    """Apify aggregate budgets outside 0..1 are rejected."""
    with pytest.raises((TypeError, ValueError)):
        build_discovery_requests(include_apify=True, apify_budget_usd=budget)


def test_maximum_plan_never_exceeds_one_hundred_rows() -> None:
    """The largest legal plan requests exactly, and never more than, 100 raw rows."""
    requests = build_discovery_requests(include_apify=True, max_candidates=100)

    assert sum(request.max_results_total for request in requests) == 100
    assert all(1 <= request.max_results_total <= 100 for request in requests)


def test_operator_search_configuration_targets_selected_geographies() -> None:
    """Custom market criteria are included in every bounded request for selected countries."""
    requests = build_discovery_requests(
        include_apify=True,
        max_candidates=12,
        market="industrial pumps",
        search_terms=("regional distributors", "RFQ workflow"),
        target_geographies=("CA",),
    )

    assert requests
    assert {request.target_country_code for request in requests} == {"CA"}
    assert sum(request.max_results_total for request in requests) == 12
    assert all(
        "industrial pumps" in query
        and "regional distributors" in query
        and "RFQ workflow" in query
        for request in requests
        for query in request.queries
    )
    assert all(
        request.request_id.startswith("exa:ca:")
        for request in requests
        if request.provider == "exa"
    )
    assert all(
        request.request_id.startswith("apify:ca:")
        for request in requests
        if request.provider == "apify"
    )


def test_custom_criteria_get_a_distinct_stable_operation_identity() -> None:
    """A resumed M2 run cannot mistake results from changed criteria for current output."""
    pumps = build_discovery_requests(
        include_apify=False,
        market="industrial pumps",
        target_geographies=("US",),
    )
    compressors = build_discovery_requests(
        include_apify=False,
        market="industrial compressors",
        target_geographies=("US",),
    )

    assert [request.request_id for request in pumps] != [
        request.request_id for request in compressors
    ]
    assert all(":c" in request.request_id for request in pumps)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"market": "   "}, "market"),
        ({"market": "industrial\npumps"}, "market"),
        ({"search_terms": ("",)}, "search_terms"),
        ({"search_terms": ("term",) * 6}, "search_terms"),
        ({"target_geographies": ("MX",)}, "target_geographies"),
        ({"target_geographies": ("US", "US")}, "target_geographies"),
        ({"target_geographies": ()}, "target_geographies"),
    ],
)
def test_operator_search_configuration_rejects_unsafe_or_unsupported_values(
    kwargs: dict[str, object], message: str
) -> None:
    """Invalid operator criteria fail before a provider request can be planned."""
    with pytest.raises(ValueError, match=message):
        build_discovery_requests(include_apify=False, **cast(Any, kwargs))
