"""Contract tests for Clay's managed Work Email function over the Public API."""

from __future__ import annotations

import json

import httpx

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.providers import ClayContactProvider, clay_item_email


def _contact() -> ContactRecord:
    return ContactRecord(
        contact_id="contact-clay-managed",
        company_id="cmp_clay_managed",
        company_name="Managed Valve",
        company_domain="managed.example",
        company_final_score=1.0,
        full_name="Casey Managed",
        title="President",
        decision_rank=1,
        decision_reason="owner",
        linkedin_url="https://www.linkedin.com/in/casey-managed",
    )


def test_clay_start_uses_managed_work_email_function_schema() -> None:
    """Direct API enrichment must use Clay's managed Work Email input names."""
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json={"routine_run_id": "run-managed-1"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = ClayContactProvider(
            api_key="test",
            routine_id="function:t_managed_work_email",
            client=client,
        ).start([_contact()])

    assert result.routine_run_id == "run-managed-1"
    assert len(observed) == 1
    request = observed[0]
    assert request.url.path == "/public/v0/routines/function:t_managed_work_email/run"
    payload = json.loads(request.content)
    assert payload == {
        "items": [
            {
                "id": "contact-clay-managed",
                "inputs": {
                    "Full Name": "Casey Managed",
                    "Company Domain": "managed.example",
                    "Company Name": "Managed Valve",
                    "Social Profile URL": "https://www.linkedin.com/in/casey-managed",
                },
            }
        ]
    }


def test_clay_managed_work_email_result_key_is_supported() -> None:
    """The managed function returns the documented title-cased Work Email key."""
    assert (
        clay_item_email({"result": {"Work Email": " Casey@Managed.Example "}})
        == "casey@managed.example"
    )
