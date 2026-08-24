"""Pure deterministic M4 current-employment validation, ranking, and deduplication."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from copy import deepcopy
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.dedup import normalize_company_name
from leads_discovery.models import CompanyRecord

_DIRECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("owner", re.compile(r"\b(?:co[- ]?owner|owner)\b")),
    ("ceo", re.compile(r"\b(?:ceo|chief executive officer)\b")),
    ("coo", re.compile(r"\b(?:coo|chief operating officer)\b")),
    ("managing_partner", re.compile(r"\bmanaging partner\b")),
    ("general_manager", re.compile(r"\b(?:general manager|gm)\b")),
)
_SENIOR_PATTERN = re.compile(
    r"\b(?:chief|executive|svp|evp|vp|vice president|head|director)\b"
)
_MANAGER_PATTERN = re.compile(r"\b(?:manager|management|director)\b")
_CORE_FUNCTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("inside_sales", re.compile(r"\binside sales\b")),
    ("estimating", re.compile(r"\bestimat(?:e|es|ing|or|ors)\b")),
    ("sales", re.compile(r"\bsales\b")),
    ("operations", re.compile(r"\b(?:operations|operational)\b")),
    ("commercial", re.compile(r"\bcommercial\b")),
)
_DEPUTY_FUNCTIONS: tuple[tuple[str, re.Pattern[str]], ...] = _CORE_FUNCTIONS + (
    ("branch", re.compile(r"\bbranch\b")),
    ("regional", re.compile(r"\bregional\b")),
)


def normalize_contact_name(value: str) -> str:
    """Normalize a contact name for exact fallback identity and deterministic ordering."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def normalize_profile_url(value: str | None) -> str | None:
    """Normalize a public profile URL without fuzzy identity inference."""
    if value is None or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.casefold() not in {"http", "https"} or parsed.hostname is None:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        _ = parsed.port
    except ValueError:
        return None
    host = parsed.hostname.casefold().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+", "/", parsed.path).rstrip("/") or "/"
    return urlunsplit(("https", host, path, "", ""))


def _normalized_title(value: str) -> str:
    """Canonicalize title punctuation and spacing before deterministic keyword matching."""
    text = unicodedata.normalize("NFKC", value).casefold().replace("&", " and ")
    text = text.replace(".", "")
    text = re.sub(r"[-‐‑‒–—_/,:;|()\[\]{}]+", " ", text)
    text = re.sub(r"[^\w\s]+", " ", text)
    return " ".join(text.split())


def _function_match(
    title: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]
) -> str | None:
    """Return the first explicitly relevant business function found in a normalized title."""
    for name, pattern in patterns:
        if pattern.search(title):
            return name
    return None


def rank_title(title: str) -> tuple[int, str] | None:
    """Classify a title by proximity to the operational software-buying decision."""
    normalized = _normalized_title(title)
    if not normalized:
        return None

    for reason, pattern in _DIRECT_PATTERNS:
        if pattern.search(normalized):
            return 1, f"direct_decision_maker:{reason}"
    if "vice president" not in normalized and re.search(r"\bpresident\b", normalized):
        return 1, "direct_decision_maker:president"

    function = _function_match(normalized, _CORE_FUNCTIONS)
    if function is not None and _SENIOR_PATTERN.search(normalized):
        return 2, f"functional_decision_maker:{function}"

    deputy = _function_match(normalized, _DEPUTY_FUNCTIONS)
    if deputy is not None and _MANAGER_PATTERN.search(normalized):
        return 3, f"operational_deputy:{deputy}"
    return None


def _person_properties(raw: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Return the first structured Exa person entity properties and stable entity ID."""
    entities = raw.get("entities")
    if not isinstance(entities, list):
        return {}, None
    for item in entities:
        if not isinstance(item, dict) or item.get("type") != "person":
            continue
        properties = item.get("properties")
        if not isinstance(properties, dict):
            return {}, None
        entity_id = item.get("id")
        return cast(dict[str, Any], properties), entity_id if isinstance(entity_id, str) else None
    return {}, None


def _current_roles(
    properties: dict[str, Any], company: CompanyRecord
) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Return explicit current roles whose structured employer exactly matches the company."""
    target = normalize_company_name(company.normalized_name or company.name)
    if target is None:
        return ()
    history = properties.get("workHistory")
    if not isinstance(history, list):
        return ()
    roles: list[tuple[str, dict[str, Any]]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        employer = item.get("company")
        dates = item.get("dates")
        title = item.get("title")
        if (
            not isinstance(employer, dict)
            or not isinstance(dates, dict)
            or "to" not in dates
            or dates.get("to") is not None
            or not isinstance(title, str)
            or not title.strip()
        ):
            continue
        employer_name = employer.get("name")
        if not isinstance(employer_name, str):
            continue
        if normalize_company_name(employer_name) == target:
            roles.append((title.strip(), deepcopy(item)))
    return tuple(roles)


def _contact_identity(
    *, company: CompanyRecord, full_name: str, profile_url: str | None
) -> tuple[str, str] | None:
    """Return the frozen dedupe key and stable contact ID, or None when identity is too weak."""
    if profile_url is not None:
        key = f"profile:{profile_url}"
    else:
        domain = (company.normalized_domain or company.domain or "").strip().casefold()
        name = normalize_contact_name(full_name)
        if not name or not domain:
            return None
        key = f"name_domain:{name}|{domain}"
    digest = hashlib.sha256(f"{company.company_id}\0{key}".encode()).hexdigest()[:24]
    return key, "ctc_" + digest


def _candidate_from_result(
    company: CompanyRecord, raw: dict[str, Any]
) -> tuple[ContactRecord, str] | None:
    """Convert one Exa result into its best qualifying current role and exact dedupe key."""
    properties, entity_id = _person_properties(raw)
    full_name = properties.get("name")
    if not isinstance(full_name, str) or not full_name.strip():
        return None

    ranked_roles: list[tuple[int, str, str, dict[str, Any]]] = []
    for title, work_row in _current_roles(properties, company):
        ranking = rank_title(title)
        if ranking is not None:
            rank, reason = ranking
            ranked_roles.append((rank, normalize_contact_name(title), reason, work_row))
    if not ranked_roles:
        return None
    ranked_roles.sort(key=lambda item: (item[0], item[1], item[2]))
    rank, _, reason, work_row = ranked_roles[0]
    title = cast(str, work_row["title"]).strip()

    source_url = raw.get("url") if isinstance(raw.get("url"), str) else None
    profile_url = normalize_profile_url(source_url)
    identity = _contact_identity(
        company=company,
        full_name=full_name.strip(),
        profile_url=profile_url,
    )
    if identity is None:
        return None
    dedupe_key, contact_id = identity
    host = urlsplit(profile_url).hostname if profile_url is not None else None
    linkedin_url = profile_url if host in {"linkedin.com", "www.linkedin.com"} else None
    provider_result_id = raw.get("id") if isinstance(raw.get("id"), str) else None
    domain = (company.normalized_domain or company.domain or "").strip().casefold()
    source = {
        "provider": "exa",
        "profile_url": profile_url,
        "provider_result_id": provider_result_id,
        "person_entity_id": entity_id,
        "current_work_history": work_row,
    }
    contact = ContactRecord(
        contact_id=contact_id,
        company_id=company.company_id,
        company_name=company.name,
        company_domain=domain,
        company_final_score=company.final_score,
        full_name=full_name.strip(),
        title=title,
        decision_rank=rank,
        decision_reason=reason,
        linkedin_url=linkedin_url,
        profile_url=profile_url,
        current_employment_confirmed=True,
        sources=[source],
    )
    return contact, dedupe_key


def select_contacts(
    company: CompanyRecord, results: list[dict[str, Any]], *, limit: int = 3
) -> tuple[ContactRecord, ...]:
    """Select at most three exact-deduped current employees in deterministic decision order."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 3:
        raise ValueError("contact selection limit must be an integer in 1..3")
    candidates: list[tuple[ContactRecord, str]] = []
    for raw in results[:10]:
        if not isinstance(raw, dict):
            raise ValueError("Exa people results must contain objects")
        candidate = _candidate_from_result(company, raw)
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(
        key=lambda item: (
            item[0].decision_rank,
            normalize_contact_name(item[0].full_name),
            item[0].contact_id,
        )
    )
    selected: list[ContactRecord] = []
    seen: set[str] = set()
    for contact, key in candidates:
        if key in seen:
            continue
        seen.add(key)
        selected.append(ContactRecord.from_dict(contact.to_dict()))
        if len(selected) == limit:
            break
    return tuple(selected)
