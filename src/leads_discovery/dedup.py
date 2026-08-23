"""Pure offline normalization and conservative domain-first company deduplication."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import tldextract

from leads_discovery.models import CompanyRecord, DeduplicationResult, DiscoveryRecord

_EXTRACT = tldextract.TLDExtract(
    cache_dir=None,
    suffix_list_urls=(),
    fallback_to_snapshot=True,
    include_psl_private_domains=True,
)
_DENIED_DOMAINS = {
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
}
_LEGAL_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "co",
    "company",
    "lp",
    "llp",
    "plc",
    "ulc",
    "ltee",
    "ltée",
}
_US_REGIONS = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}
_CA_REGIONS = {
    "alberta": "AB",
    "british columbia": "BC",
    "manitoba": "MB",
    "new brunswick": "NB",
    "newfoundland and labrador": "NL",
    "northwest territories": "NT",
    "nova scotia": "NS",
    "nunavut": "NU",
    "ontario": "ON",
    "prince edward island": "PE",
    "quebec": "QC",
    "québec": "QC",
    "saskatchewan": "SK",
    "yukon": "YT",
}
_REGION_CODES = {
    **{code.casefold(): code for code in _US_REGIONS.values()},
    **{code.casefold(): code for code in _CA_REGIONS.values()},
}
_COUNTRY_ALIASES = {
    "us": "US",
    "u s": "US",
    "usa": "US",
    "u s a": "US",
    "united states": "US",
    "united states of america": "US",
    "america": "US",
    "ca": "CA",
    "can": "CA",
    "canada": "CA",
    "mx": "MX",
    "mex": "MX",
    "mexico": "MX",
    "méxico": "MX",
    "gb": "GB",
    "uk": "GB",
    "united kingdom": "GB",
}
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True, slots=True)
class _Prepared:
    """Hold pure normalized identity data for one deterministic raw-row occurrence."""

    record: DiscoveryRecord
    raw_key: str
    stable_identity: str
    domain: str | None
    name: str | None
    city: str | None
    region: str | None
    country: str | None
    fallback: str | None
    retrieved: datetime


def _normalize_text(value: str | None) -> str | None:
    """Normalize Unicode, ampersands, punctuation, case, and whitespace without inference."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", value).casefold().replace("&", " and ")
    text = "".join(char if char.isalnum() or char.isspace() else " " for char in text)
    text = " ".join(text.split())
    return text or None


def normalize_company_name(name: str | None) -> str | None:
    """Normalize a company name and repeatedly remove only trailing legal suffix tokens."""
    text = _normalize_text(name)
    if text is None:
        return None
    tokens = text.split()
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    normalized = " ".join(tokens)
    return normalized or None


def _registrable_http_domain(url: str | None) -> str | None:
    """Return an offline registrable HTTP(S) domain without applying corporate deny rules."""
    if url is None or not url.strip():
        return None
    try:
        parsed = urlsplit(url.strip())
        if parsed.scheme.casefold() not in {"http", "https"}:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        _ = parsed.port
        host = parsed.hostname
    except ValueError:
        return None
    if host is None:
        return None
    host = host.rstrip(".").casefold()
    if host == "localhost" or host.endswith(".local"):
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    try:
        ascii_host = ".".join(label.encode("idna").decode("ascii") for label in host.split("."))
    except UnicodeError:
        return None
    labels = ascii_host.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL.fullmatch(label) for label in labels):
        return None
    extracted = _EXTRACT(ascii_host)
    if not extracted.domain or not extracted.suffix:
        return None
    domain = extracted.top_domain_under_public_suffix.casefold()
    return domain or None


def normalize_website_domain(url: str | None) -> str | None:
    """Return an offline registrable HTTP(S) corporate domain or None for unsafe/weak URLs."""
    domain = _registrable_http_domain(url)
    if domain is None or domain in _DENIED_DOMAINS:
        return None
    return domain


def _normalize_region(value: str | None) -> str | None:
    """Normalize U.S./Canadian region names and codes, preserving unknown normalized text."""
    text = _normalize_text(value)
    if text is None:
        return None
    if text in _REGION_CODES:
        return _REGION_CODES[text]
    if text in _US_REGIONS:
        return _US_REGIONS[text]
    if text in _CA_REGIONS:
        return _CA_REGIONS[text]
    return text.upper()


def _normalize_country(value: str | None) -> str | None:
    """Normalize recognized country aliases while keeping unknown reported values explicit."""
    text = _normalize_text(value)
    if text is None:
        return None
    if text in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[text]
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return text.upper()


def _parse_timestamp(value: str) -> datetime:
    """Parse a required timezone-aware source timestamp and normalize it to UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            "discovery retrieved_at must be a valid timezone-aware timestamp"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("discovery retrieved_at must be timezone-aware")
    return parsed.astimezone(UTC)


def _raw_key(record: DiscoveryRecord) -> str:
    """Serialize a raw record deterministically for provenance ordering only."""
    return json.dumps(record.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _prepare(record: DiscoveryRecord) -> _Prepared:
    """Normalize all identity fields for one raw record without any I/O or inference."""
    name = normalize_company_name(record.name)
    city = _normalize_text(record.city)
    region = _normalize_region(record.region)
    country = _normalize_country(record.country_code)
    fallback = (
        f"{name}|{city}|{region}|{country}"
        if name is not None and city is not None and region is not None and country is not None
        else None
    )
    return _Prepared(
        record=DiscoveryRecord.from_dict(record.to_dict()),
        raw_key=_raw_key(record),
        stable_identity=record.record_id,
        domain=normalize_website_domain(record.website_url),
        name=name,
        city=city,
        region=region,
        country=country,
        fallback=fallback,
        retrieved=_parse_timestamp(record.retrieved_at),
    )


def _company_id(authority: str) -> str:
    """Build a deterministic canonical company ID from an authoritative identity string."""
    return "cmp_" + hashlib.sha256(authority.encode("utf-8")).hexdigest()[:24]


def _canonical_name(rows: list[_Prepared], domain: str | None) -> tuple[str, str | None, bool]:
    """Choose canonical normalized/raw names with frozen frequency/provider/tie rules."""
    usable = [row for row in rows if row.name is not None and row.record.name is not None]
    if not usable:
        if domain is None:
            raise AssertionError("domainless company group must have a usable name")
        return domain, domain, True
    normalized_names = [row.name for row in usable if row.name is not None]
    counts: Counter[str] = Counter(normalized_names)
    max_count = max(counts.values())
    candidates = [name for name, count in counts.items() if count == max_count]
    candidates.sort(
        key=lambda candidate_name: (
            not any(
                row.record.provider == "exa" and row.name == candidate_name for row in usable
            ),
            candidate_name,
        )
    )
    normalized = candidates[0]
    raw_candidates = [row for row in usable if row.name == normalized]
    raw_candidates.sort(
        key=lambda row: (
            row.record.provider != "exa",
            len((row.record.name or "").strip()),
            (row.record.name or "").strip().casefold(),
            (row.record.name or "").strip(),
        )
    )
    return (raw_candidates[0].record.name or normalized).strip(), normalized, False


def _location(row: _Prepared) -> str | None:
    """Build one normalized location string using only provider-reported components."""
    postal = _normalize_text(row.record.postal_code)
    if postal is not None:
        postal = postal.upper()
    parts = [part for part in (row.city, row.region, postal, row.country) if part is not None]
    return ", ".join(parts) if parts else None


def _build_company(
    rows: list[_Prepared], *, authority: str, domain: str | None, extra_review: Iterable[str] = ()
) -> CompanyRecord:
    """Build one deterministic canonical company while retaining every grouped raw row."""
    rows = sorted(rows, key=lambda row: row.raw_key)
    raw_name, normalized_name, provisional = _canonical_name(rows, domain)
    countries = sorted({row.country for row in rows if row.country is not None})
    review = set(extra_review)
    if provisional:
        review.add("INSUFFICIENT_IDENTITY")
    country: str | None
    if len(countries) == 1:
        country = countries[0]
    elif len(countries) > 1:
        country = None
        review.add("CONFLICTING_COUNTRY")
    else:
        country = None
    if any(row.country is not None and row.country not in {"US", "CA"} for row in rows):
        review.add("OUTSIDE_GEOGRAPHY")
    locations = sorted({location for row in rows if (location := _location(row)) is not None})
    providers = sorted({row.record.provider for row in rows})
    queries = sorted({row.record.query for row in rows if row.record.query is not None})
    created = min(row.retrieved for row in rows).isoformat()
    updated = max(row.retrieved for row in rows).isoformat()
    return CompanyRecord(
        company_id=_company_id(authority),
        name=raw_name,
        normalized_name=normalized_name,
        domain=domain,
        normalized_domain=domain,
        country=country,
        locations_if_known=locations,
        status="active",
        discovery_sources=providers,
        discovery_queries=queries,
        discovery_records=[row.record.to_dict() for row in rows],
        review_reasons=sorted(review),
        stage_status={"deduplication": "completed"},
        created_at=created,
        updated_at=updated,
    )


def deduplicate(records: Iterable[DiscoveryRecord]) -> DeduplicationResult:
    """Deduplicate raw rows using only the frozen domain-first exact identity policy."""
    prepared = sorted((_prepare(record) for record in records), key=lambda row: row.raw_key)
    domain_groups: dict[str, list[_Prepared]] = defaultdict(list)
    domainless: list[_Prepared] = []
    unresolved: list[DiscoveryRecord] = []
    for row in prepared:
        if row.domain is not None:
            domain_groups[row.domain].append(row)
        elif row.name is None:
            unresolved.append(row.record)
        else:
            domainless.append(row)

    fallback_domains: dict[str, set[str]] = defaultdict(set)
    for domain, rows in domain_groups.items():
        for row in rows:
            if row.fallback is not None:
                fallback_domains[row.fallback].add(domain)

    domainless_groups: dict[str, list[_Prepared]] = defaultdict(list)
    singleton_specs: list[tuple[_Prepared, tuple[str, ...]]] = []
    for row in domainless:
        if row.fallback is None:
            singleton_specs.append((row, ("INSUFFICIENT_IDENTITY",)))
            continue
        matched_domains = sorted(fallback_domains.get(row.fallback, set()))
        if len(matched_domains) == 1:
            domain_groups[matched_domains[0]].append(row)
        elif len(matched_domains) > 1:
            singleton_specs.append((row, ("AMBIGUOUS_IDENTITY",)))
        else:
            domainless_groups[row.fallback].append(row)

    companies: list[CompanyRecord] = []
    for domain in sorted(domain_groups):
        companies.append(
            _build_company(domain_groups[domain], authority=f"domain:{domain}", domain=domain)
        )
    for fallback in sorted(domainless_groups):
        companies.append(
            _build_company(
                domainless_groups[fallback],
                authority=f"fallback:{fallback}",
                domain=None,
            )
        )

    duplicate_rank: dict[str, int] = defaultdict(int)
    ordered_singletons = sorted(
        singleton_specs,
        key=lambda item: (item[0].stable_identity, item[0].raw_key, item[1]),
    )
    for row, reasons in ordered_singletons:
        rank = duplicate_rank[row.stable_identity]
        duplicate_rank[row.stable_identity] += 1
        authority = f"singleton:{row.stable_identity}:{rank}"
        companies.append(
            _build_company([row], authority=authority, domain=None, extra_review=reasons)
        )

    companies.sort(key=lambda company: company.company_id)
    unresolved.sort(key=_raw_key)
    return DeduplicationResult(companies=companies, unresolved_records=unresolved)
