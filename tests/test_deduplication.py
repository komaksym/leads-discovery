"""Contract tests for offline normalization and conservative company identity resolution."""

from __future__ import annotations

import json
import socket
from itertools import permutations
from typing import Any

import pytest

from leads_discovery.dedup import deduplicate, normalize_company_name, normalize_website_domain
from leads_discovery.models import DiscoveryRecord

DENIED_DOMAINS = (
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "google.com",
    "yelp.com",
    "yellowpages.com",
    "yellowpages.ca",
    "mapquest.com",
    "crunchbase.com",
    "bloomberg.com",
    "zoominfo.com",
    "dnb.com",
    "pitchbook.com",
    "opencorporates.com",
)


def _record(
    record_id: str,
    *,
    provider: str = "exa",
    name: str | None = "Acme Valve",
    website_url: str | None = None,
    source_url: str | None = None,
    city: str | None = "Houston",
    region: str | None = "Texas",
    country_code: str | None = "United States",
    postal_code: str | None = "77001",
    query: str | None = "pvf query",
    provider_result_id: str | None = None,
    retrieved_at: str = "2026-08-23T10:00:00+00:00",
    raw_metadata: dict[str, Any] | None = None,
) -> DiscoveryRecord:
    """Create a discovery row with explicit reported identity fields."""
    return DiscoveryRecord(
        record_id=record_id,
        provider=provider,
        request_id=f"{provider}:us:test:v1",
        target_country_code="US",
        query=query,
        provider_result_id=provider_result_id,
        name=name,
        source_url=source_url,
        website_url=website_url,
        city=city,
        region=region,
        postal_code=postal_code,
        country_code=country_code,
        title=None,
        snippet=None,
        raw_metadata={"record": record_id} if raw_metadata is None else raw_metadata,
        retrieved_at=retrieved_at,
    )


def _serialize_result(records: list[DiscoveryRecord]) -> str:
    """Return deterministic serialized deduplication output for invariance assertions."""
    return json.dumps(deduplicate(records).to_dict(), sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.Example.COM/path?q=1", "example.com"),
        ("http://sub.example.co.uk/a", "example.co.uk"),
        ("https://bücher.de/catalog", "xn--bcher-kva.de"),
        ("https://acme.wixsite.com/catalog", "acme.wixsite.com"),
        ("https://beta.wixsite.com/catalog", "beta.wixsite.com"),
        (None, None),
        ("ftp://example.com", None),
        ("https://user:pass@example.com", None),
        ("https://localhost", None),
        ("https://thing.local", None),
        ("https://127.0.0.1", None),
        ("https://[::1]", None),
        ("https://example.invalidtld", None),
        ("https://-bad.example.com", None),
        ("https://example.com:bad", None),
    ],
)
def test_offline_website_normalization(url: str | None, expected: str | None) -> None:
    """Corporate identity accepts only valid offline-resolved HTTP(S) registrable domains."""
    assert normalize_website_domain(url) == expected


def test_private_suffix_tenants_remain_distinct() -> None:
    """Private suffix support prevents unrelated hosted-site tenants from collapsing."""
    assert normalize_website_domain("https://acme.wixsite.com") == "acme.wixsite.com"
    assert normalize_website_domain("https://beta.wixsite.com") == "beta.wixsite.com"


@pytest.mark.parametrize("domain", DENIED_DOMAINS)
def test_every_forbidden_corporate_identity_domain_is_rejected(domain: str) -> None:
    """Social, directory, search, and data-broker domains never become company identity."""
    assert normalize_website_domain(f"https://sub.{domain}/company/acme") is None


def test_normalization_performs_no_dns_or_suffix_network_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Domain normalization works with DNS disabled and the bundled suffix snapshot only."""

    def forbidden_dns(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("DNS/network access is forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_dns)
    assert normalize_website_domain("https://www.example.com/path") == "example.com"
    assert normalize_website_domain("https://acme.wixsite.com/path") == "acme.wixsite.com"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Acme & Sons, Inc. LLC  ", "acme and sons"),
        ("ＡＣＭＥ　ＶＡＬＶＥ　ＣＯＲＰ", "acme valve"),
        ("Acme Ltd. Limited Corporation", "acme"),
        ("Company Valve Systems", "company valve systems"),
        ("Acme Company Valve", "acme company valve"),
        (None, None),
        ("---", None),
    ],
)
def test_company_name_normalization_has_exact_suffix_boundaries(
    raw: str | None,
    expected: str | None,
) -> None:
    """Name normalization removes only repeated trailing legal suffix tokens."""
    assert normalize_company_name(raw) == expected


def test_ded01_valid_domain_groups_by_exact_registrable_domain() -> None:
    """Rows with the same valid domain merge even when weaker fields differ."""
    result = deduplicate(
        [
            _record("raw_a", website_url="https://www.acme.com/a", city="Houston"),
            _record(
                "raw_b",
                provider="apify",
                website_url="http://shop.acme.com/b",
                city="Dallas",
                query=None,
            ),
        ]
    )

    assert len(result.companies) == 1
    company = result.companies[0]
    assert company.domain == "acme.com"
    assert company.normalized_domain == "acme.com"
    assert company.discovery_sources == ["apify", "exa"]
    assert len(company.discovery_records) == 2
    assert result.unresolved_records == []


def test_ded02_different_domains_never_merge_through_same_fallback() -> None:
    """Two valid corporate domains remain separate even with identical name/location."""
    result = deduplicate(
        [
            _record("raw_a", website_url="https://acme-one.com"),
            _record("raw_b", website_url="https://acme-two.com"),
        ]
    )

    assert len(result.companies) == 2
    assert {company.domain for company in result.companies} == {"acme-one.com", "acme-two.com"}


def test_ded03_domainless_full_fallback_attaches_to_exactly_one_domain_group() -> None:
    """A complete domainless fallback may attach only when exactly one domain group matches."""
    result = deduplicate(
        [
            _record("raw_domain", website_url="https://acme.com"),
            _record("raw_domainless", provider="apify", website_url=None),
        ]
    )

    assert len(result.companies) == 1
    assert len(result.companies[0].discovery_records) == 2


def test_ded04_ambiguous_domainless_fallback_becomes_review_singleton() -> None:
    """A fallback matching multiple domain groups cannot choose between them."""
    result = deduplicate(
        [
            _record("raw_one", website_url="https://one.example.com"),
            _record("raw_two", website_url="https://two.example.net"),
            _record("raw_ambiguous", website_url=None),
        ]
    )

    assert len(result.companies) == 3
    singleton = next(
        company
        for company in result.companies
        if any(row["record_id"] == "raw_ambiguous" for row in company.discovery_records)
    )
    assert "AMBIGUOUS_IDENTITY" in singleton.review_reasons
    assert singleton.domain is None


def test_ded05_domainless_exact_fallback_records_merge() -> None:
    """Domainless rows merge only on the complete normalized fallback key."""
    result = deduplicate(
        [
            _record("raw_a", website_url=None, region="TX", country_code="US"),
            _record(
                "raw_b",
                website_url=None,
                name="ACME VALVE LLC",
                city="Houston!",
                region="Texas",
                country_code="United States",
            ),
        ]
    )

    assert len(result.companies) == 1
    assert result.companies[0].domain is None
    assert len(result.companies[0].discovery_records) == 2


def test_ded06_incomplete_domainless_named_record_is_review_singleton() -> None:
    """A named row missing any fallback component cannot weakly merge."""
    result = deduplicate([_record("raw_a", website_url=None, country_code=None)])

    assert len(result.companies) == 1
    assert "INSUFFICIENT_IDENTITY" in result.companies[0].review_reasons


def test_ded07_domain_without_usable_name_uses_domain_provisionally() -> None:
    """A valid-domain group without a usable name survives with review metadata."""
    result = deduplicate([_record("raw_a", name=None, website_url="https://acme.com")])

    assert len(result.companies) == 1
    company = result.companies[0]
    assert company.name == "acme.com"
    assert company.normalized_name == "acme.com"
    assert "INSUFFICIENT_IDENTITY" in company.review_reasons


def test_ded08_row_without_name_or_domain_is_unresolved() -> None:
    """A row with neither usable name nor valid domain creates no company."""
    record = _record("raw_a", name=None, website_url=None)
    result = deduplicate([record])

    assert result.companies == []
    assert [row.record_id for row in result.unresolved_records] == ["raw_a"]


def test_source_url_is_provenance_not_identity() -> None:
    """A corporate-looking source URL never substitutes for a missing website URL."""
    result = deduplicate(
        [
            _record(
                "raw_a",
                name="Alpha Valve",
                website_url=None,
                source_url="https://same.example/company",
                country_code=None,
            ),
            _record(
                "raw_b",
                name="Beta Valve",
                website_url=None,
                source_url="https://same.example/company",
                country_code=None,
            ),
        ]
    )

    assert len(result.companies) == 2
    assert all(company.domain is None for company in result.companies)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (
            _record("raw_a", website_url=None, provider_result_id="shared", city="Houston"),
            _record("raw_b", website_url=None, provider_result_id="shared", city="Dallas"),
        ),
        (
            _record("raw_a", website_url=None, postal_code="99999", city="Houston"),
            _record("raw_b", website_url=None, postal_code="99999", city="Dallas"),
        ),
        (
            _record("raw_a", website_url=None, country_code=None),
            _record("raw_b", website_url=None, country_code=None),
        ),
        (
            _record("raw_a", website_url=None, name="Acme", city="Houston"),
            _record("raw_b", website_url=None, name="Acme Valve", city="Houston"),
        ),
    ],
)
def test_forbidden_weaker_identity_signals_do_not_merge(
    left: DiscoveryRecord,
    right: DiscoveryRecord,
) -> None:
    """Provider IDs, postal/partial locations, and string similarity never merge rows."""
    result = deduplicate([left, right])

    assert len(result.companies) == 2


def test_country_conflict_and_outside_geography_are_review_metadata_not_filters() -> None:
    """Conflicting/out-of-scope countries survive while receiving deterministic review codes."""
    conflict = deduplicate(
        [
            _record("raw_us", website_url="https://acme.com", country_code="US"),
            _record("raw_ca", website_url="https://acme.com", country_code="Canada"),
        ]
    ).companies[0]
    outside = deduplicate(
        [_record("raw_mx", website_url="https://outside.com", country_code="Mexico")]
    ).companies[0]

    assert conflict.country is None
    assert "CONFLICTING_COUNTRY" in conflict.review_reasons
    assert outside.country == "MX"
    assert "OUTSIDE_GEOGRAPHY" in outside.review_reasons


def test_canonical_name_prefers_frequency_then_exa_and_preserves_m1_defaults() -> None:
    """Canonical values are deterministic and deduplication does not perform M3 scoring."""
    result = deduplicate(
        [
            _record(
                "raw_a",
                provider="exa",
                name="Acme Valve, Inc.",
                website_url="https://acme.com",
                retrieved_at="2026-08-23T09:00:00+00:00",
            ),
            _record(
                "raw_b",
                provider="apify",
                name="ACME VALVE LLC",
                website_url="https://acme.com",
                retrieved_at="2026-08-23T11:00:00+00:00",
            ),
            _record(
                "raw_c",
                provider="apify",
                name="Acme Flow",
                website_url="https://acme.com",
                retrieved_at="2026-08-23T10:00:00+00:00",
            ),
        ]
    )
    company = result.companies[0]

    assert company.normalized_name == "acme valve"
    assert company.name == "Acme Valve, Inc."
    assert company.status == "active"
    assert company.stage_status == {"deduplication": "completed"}
    assert company.coverage == {}
    assert company.score_components == {}
    assert company.final_score is None
    assert company.final_decision is None
    assert company.rejection_reasons == []
    assert company.created_at == "2026-08-23T09:00:00+00:00"
    assert company.updated_at == "2026-08-23T11:00:00+00:00"


def test_duplicate_singleton_rows_receive_unique_deterministic_company_ids() -> None:
    """Even duplicate raw singleton rows cannot collide on company ID within one result."""
    row = _record("raw_duplicate", website_url=None, country_code=None)
    result = deduplicate([row, row])

    assert len(result.companies) == 2
    assert len({company.company_id for company in result.companies}) == 2
    assert all(company.company_id.startswith("cmp_") for company in result.companies)


def test_singleton_company_id_ignores_retrieval_time_for_same_stable_raw_identity() -> None:
    """Stable singleton identity must not change only because the same raw row was retrieved later."""
    earlier = _record(
        "raw_stable",
        website_url=None,
        country_code=None,
        retrieved_at="2026-08-23T09:00:00+00:00",
    )
    later = _record(
        "raw_stable",
        website_url=None,
        country_code=None,
        retrieved_at="2026-08-23T11:00:00+00:00",
    )

    earlier_id = deduplicate([earlier]).companies[0].company_id
    later_id = deduplicate([later]).companies[0].company_id

    assert earlier_id == later_id


def test_permutation_invariance_and_exact_raw_row_conservation() -> None:
    """Every permutation serializes identically and every input occurrence is conserved once."""
    records = [
        _record("raw_a", website_url="https://acme.com"),
        _record("raw_b", provider="apify", website_url=None),
        _record("raw_c", website_url="https://bravo.com", name=None),
        _record("raw_d", name=None, website_url=None),
    ]

    serialized = {_serialize_result(list(order)) for order in permutations(records)}
    assert len(serialized) == 1

    result = deduplicate(records)
    canonical_rows = [row for company in result.companies for row in company.discovery_records]
    unresolved_rows = [row.to_dict() for row in result.unresolved_records]
    all_rows = canonical_rows + unresolved_rows
    assert len(all_rows) == len(records)
    assert sorted(row["record_id"] for row in all_rows) == sorted(row.record_id for row in records)
    assert all(company.company_id.startswith("cmp_") for company in result.companies)
    assert all(len(company.company_id) == 28 for company in result.companies)
