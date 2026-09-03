"""M4 contact selection and provider interfaces."""

from leads_discovery.contacts.models import ContactRecord, VerificationStatus
from leads_discovery.contacts.selection import (
    normalize_contact_name,
    normalize_profile_url,
    rank_title,
    select_contacts,
)

__all__ = [
    "ContactRecord",
    "VerificationStatus",
    "normalize_contact_name",
    "normalize_profile_url",
    "rank_title",
    "select_contacts",
]
