"""Shared durable normal-M4 evidence used by production-canary composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.providers import usable_work_email
from leads_discovery.contacts.selection import contact_decision_order_key
from leads_discovery.models import CompanyRecord, UsageEvent


@dataclass(frozen=True, slots=True)
class ApolloShadowAuthorization:
    """Identify the normal Clay-success contact allowed one Apollo shadow call."""

    company_id: str
    contact_id: str
    work_email: str


def _requests(
    usage_events: Sequence[UsageEvent],
    provider: str,
    operation: str,
) -> int:
    """Count authoritative requests for one normal provider operation."""
    return sum(
        event.request_count
        for event in usage_events
        if event.provider == provider and event.operation == operation
    )


def normal_apollo_shadow_authorization(
    *,
    companies: Sequence[CompanyRecord],
    contacts: Sequence[ContactRecord],
    operations: Mapping[str, Mapping[str, Any]],
    usage_events: Sequence[UsageEvent],
) -> ApolloShadowAuthorization | None:
    """Authorize Apollo only when durable normal M4 proves it was skipped after Clay success."""
    accepted = [
        company
        for company in companies
        if company.stage_status.get("decision") == "completed"
        and company.final_decision == "accepted"
    ]
    if len(accepted) != 1:
        return None
    company = accepted[0]

    exa_entry = operations.get(f"exa:{company.company_id}")
    clay_entry = operations.get("clay:batch")
    if (
        exa_entry is None
        or exa_entry.get("state") != "completed"
        or clay_entry is None
        or clay_entry.get("state") != "completed"
    ):
        return None
    if _requests(usage_events, "exa", "people_search") <= 0:
        return None
    if _requests(usage_events, "clay", "work_email_routine_start") <= 0:
        return None
    if _requests(usage_events, "clay", "work_email_routine_results") <= 0:
        raise ValueError(
            "normal Clay completed provider operation lacks authoritative results usage"
        )

    exa_ids = exa_entry.get("contact_ids")
    clay_ids = clay_entry.get("contact_ids")
    if not isinstance(exa_ids, list) or not isinstance(clay_ids, list):
        return None

    contacts_by_id = {contact.contact_id: contact for contact in contacts}
    selected = [
        contacts_by_id[contact_id]
        for contact_id in exa_ids
        if isinstance(contact_id, str)
        and contact_id in contacts_by_id
        and contacts_by_id[contact_id].company_id == company.company_id
    ]
    if not selected:
        return None
    selected.sort(key=contact_decision_order_key)
    contact = selected[0]
    if contact.contact_id not in clay_ids or contact.email_source != "clay":
        return None
    email = usable_work_email(contact.work_email)
    if email is None:
        return None

    normal_apollo_used = any(key.startswith("apollo:") for key in operations) or (
        _requests(usage_events, "apollo", "people_enrichment") > 0
    )
    if normal_apollo_used:
        return None
    return ApolloShadowAuthorization(company.company_id, contact.contact_id, email)


__all__ = ["ApolloShadowAuthorization", "normal_apollo_shadow_authorization"]
