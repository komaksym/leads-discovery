"""Acceptance tests for bounded paid contact enrichment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.providers import (
    ApolloResult,
    ClayResults,
    ClayStartResult,
    ContactProviderError,
    ExaPeopleResult,
    VerificationResult,
)
from leads_discovery.models import CompanyRecord, UsageEvent
from leads_discovery.pipeline.contact_discovery import (
    ContactDiscoveryConfig,
    run_contact_discovery,
)
from leads_discovery.pipeline.contact_enrichment import (
    ContactEnrichmentConfig,
    run_contact_enrichment,
)


def _company() -> CompanyRecord:
    return CompanyRecord(
        company_id="accepted",
        name="Accepted Co",
        normalized_name="accepted co",
        domain="accepted.example",
        normalized_domain="accepted.example",
        country="US",
        stage_status={"decision": "completed"},
        final_score=90.0,
        final_decision="accepted",
    )


def _write_evaluated(run_dir: Path, company: CompanyRecord) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "companies_evaluated.jsonl").write_text(
        json.dumps(company.to_dict(), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _person(
    company: CompanyRecord,
    person_id: str,
    name: str,
    title: str,
) -> dict[str, object]:
    return {
        "id": person_id,
        "url": f"https://linkedin.com/in/{person_id}",
        "entities": [
            {
                "type": "person",
                "id": f"entity-{person_id}",
                "properties": {
                    "name": name,
                    "workHistory": [
                        {
                            "company": {"name": company.name},
                            "dates": {"to": None},
                            "title": title,
                        }
                    ],
                },
            }
        ],
    }


def _usage(provider: str, operation: str, **metadata: Any) -> UsageEvent:
    return UsageEvent(
        provider=provider,
        operation=operation,
        request_count=1,
        metadata=metadata,
    )


def _discover(tmp_path: Path, run_id: str) -> None:
    company = _company()
    _write_evaluated(tmp_path / run_id, company)

    def search(candidate: CompanyRecord) -> ExaPeopleResult:
        return ExaPeopleResult(
            results=[
                _person(candidate, "president", "Amy President", "President"),
                _person(candidate, "operations", "Ben Operations", "VP Operations"),
                _person(candidate, "branch", "Casey Branch", "Branch Manager"),
            ],
            usage_event=UsageEvent(
                provider="exa",
                operation="people_search",
                request_count=1,
                estimated_cost_usd=0.0,
                metadata={"company_id": candidate.company_id},
            ),
        )

    summary = run_contact_discovery(
        ContactDiscoveryConfig(
            run_id=run_id,
            data_root=tmp_path,
            max_contacts_per_company=3,
            execute_live=True,
        ),
        exa_search=search,
    )
    assert summary.status == "completed"
    assert summary.contact_count == 3


class _Clay:
    def __init__(
        self,
        *,
        prefix: str = "clay",
        missing_last_email: bool = False,
    ) -> None:
        self.prefix = prefix
        self.missing_last_email = missing_last_email
        self.started: list[list[str]] = []
        self.result_calls = 0

    def start(self, contacts: list[ContactRecord]) -> ClayStartResult:
        ids = [contact.contact_id for contact in contacts]
        self.started.append(ids)
        return ClayStartResult(
            routine_run_id=f"{self.prefix}-{len(self.started)}",
            usage_event=_usage(
                "clay",
                "work_email_routine_start",
                submitted_contacts=len(ids),
            ),
        )

    def results(self, routine_run_id: str) -> ClayResults:
        self.result_calls += 1
        index = int(routine_run_id.rsplit("-", 1)[1]) - 1
        ids = self.started[index]
        items = []
        for position, contact_id in enumerate(ids):
            result: dict[str, str] = {
                "work_email": f"clay-{position}@accepted.example"
            }
            if self.missing_last_email and position == len(ids) - 1:
                result = {}
            items.append({"id": contact_id, "result": result})
        return ClayResults(
            status="complete",
            items=items,
            usage_event=_usage(
                "clay",
                "work_email_routine_results",
                routine_run_id=routine_run_id,
            ),
        )


class _Apollo:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def enrich(self, contact: ContactRecord) -> ApolloResult:
        self.calls.append(contact.contact_id)
        return ApolloResult(
            work_email=f"apollo-{len(self.calls)}@accepted.example",
            credits_used=1.0,
            usage_event=_usage(
                "apollo",
                "people_enrichment",
                contact_id=contact.contact_id,
                credits_used=1.0,
            ),
        )


class _Instantly:
    def __init__(self, *, pending: bool = False) -> None:
        self.pending = pending
        self.created: list[str] = []
        self.read: list[str] = []

    def create(self, email: str) -> VerificationResult:
        self.created.append(email)
        return VerificationResult(
            status="pending" if self.pending else "verified",
            usage_event=_usage(
                "instantly",
                "email_verification_create",
                email=email,
            ),
        )

    def get(self, email: str) -> VerificationResult:
        self.read.append(email)
        return VerificationResult(
            status="verified",
            usage_event=_usage(
                "instantly",
                "email_verification_get",
                email=email,
            ),
        )


class _BombClay:
    def start(self, contacts: list[ContactRecord]) -> ClayStartResult:
        raise AssertionError(f"unexpected Clay start for {len(contacts)} contacts")

    def results(self, routine_run_id: str) -> ClayResults:
        raise AssertionError(f"unexpected Clay results read: {routine_run_id}")


class _BombApollo:
    def enrich(self, contact: ContactRecord) -> ApolloResult:
        raise AssertionError(f"unexpected Apollo call: {contact.contact_id}")


class _BombInstantly:
    def create(self, email: str) -> VerificationResult:
        raise AssertionError(f"unexpected Instantly create: {email}")

    def get(self, email: str) -> VerificationResult:
        raise AssertionError(f"unexpected Instantly get: {email}")


def _config(
    tmp_path: Path,
    run_id: str,
    *,
    max_paid: int = 2,
    clay_cap: int = 2,
    apollo_cap: float = 1.0,
    instantly_cap: int = 2,
) -> ContactEnrichmentConfig:
    return ContactEnrichmentConfig(
        run_id=run_id,
        data_root=tmp_path,
        max_paid_contacts_per_company=max_paid,
        clay_contact_cap=clay_cap,
        apollo_credit_cap=apollo_cap,
        instantly_verification_call_cap=instantly_cap,
        execute_live=True,
    )


def test_rank3_only_contact_is_retained_without_paid_provider_calls(
    tmp_path: Path,
) -> None:
    run_id = "rank3-retained"
    company = _company()
    _write_evaluated(tmp_path / run_id, company)

    def search(candidate: CompanyRecord) -> ExaPeopleResult:
        return ExaPeopleResult(
            results=[
                _person(
                    candidate,
                    "operations-manager",
                    "Robin Operations",
                    "Operations Manager",
                )
            ],
            usage_event=UsageEvent(
                provider="exa",
                operation="people_search",
                request_count=1,
                estimated_cost_usd=0.0,
                metadata={"company_id": candidate.company_id},
            ),
        )

    discovery = run_contact_discovery(
        ContactDiscoveryConfig(
            run_id=run_id,
            data_root=tmp_path,
            max_contacts_per_company=3,
            execute_live=True,
        ),
        exa_search=search,
    )
    assert discovery.status == "completed"
    assert discovery.contact_count == 1

    enriched = run_contact_enrichment(
        _config(tmp_path, run_id),
        clay=_BombClay(),
        apollo=_BombApollo(),
        instantly=_BombInstantly(),
    )

    assert enriched.status == "completed"
    rows = [
        json.loads(line)
        for line in enriched.contacts_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["decision_rank"] == 3
    assert rows[0]["provider_attempts"] == []



def test_pending_clay_resume_fails_closed_for_contact_outside_current_paid_boundary(
    tmp_path: Path,
) -> None:
    run_id = "pending-clay-rank3"
    _discover(tmp_path, run_id)
    clay = _Clay()
    config = _config(tmp_path, run_id)

    initial = run_contact_enrichment(
        config,
        clay=clay,
        apollo=_BombApollo(),
        instantly=_BombInstantly(),
    )
    assert initial.status == "paused_pending"
    assert clay.result_calls == 0

    checkpoint_path = tmp_path / run_id / "contact_discovery_checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    operations = checkpoint["provider_state"]["operations"]
    pending_batch = next(
        entry
        for operation_id, entry in operations.items()
        if operation_id.startswith("clay_batch:") and entry["state"] == "pending"
    )

    contacts_path = tmp_path / run_id / "contacts.jsonl"
    rows = [
        json.loads(line)
        for line in contacts_path.read_text(encoding="utf-8").splitlines()
    ]
    rank3 = next(row for row in rows if row["decision_rank"] == 3)
    pending_batch["contact_ids"] = [rank3["contact_id"]]
    checkpoint_path.write_text(
        json.dumps(checkpoint, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    resumed = run_contact_enrichment(
        config,
        clay=clay,
        apollo=_BombApollo(),
        instantly=_BombInstantly(),
    )

    assert resumed.status == "paused_unknown"
    assert clay.result_calls == 0
    persisted = [
        json.loads(line)
        for line in contacts_path.read_text(encoding="utf-8").splitlines()
    ]
    persisted_rank3 = next(
        row for row in persisted if row["contact_id"] == rank3["contact_id"]
    )
    assert persisted_rank3["provider_attempts"] == []


def test_enrichment_is_bounded_exact_cap_and_idempotent(tmp_path: Path) -> None:
    run_id = "bounded"
    _discover(tmp_path, run_id)
    clay = _Clay(missing_last_email=True)
    apollo = _Apollo()
    instantly = _Instantly()
    config = _config(tmp_path, run_id)

    first = run_contact_enrichment(
        config,
        clay=clay,
        apollo=apollo,
        instantly=instantly,
    )
    assert first.status == "paused_pending"

    completed = run_contact_enrichment(
        config,
        clay=clay,
        apollo=apollo,
        instantly=instantly,
    )
    assert completed.status == "completed"
    assert len(clay.started) == 1
    assert len(clay.started[0]) == 2
    assert len(apollo.calls) == 1
    assert len(instantly.created) == 2

    rows = [
        json.loads(line)
        for line in completed.contacts_path.read_text(encoding="utf-8").splitlines()
    ]
    enriched = [
        row
        for row in rows
        if any(
            attempt.get("provider") == "clay"
            and attempt.get("state") == "completed"
            for attempt in row["provider_attempts"]
        )
    ]
    untouched = [row for row in rows if row not in enriched]
    assert len(enriched) == 2
    assert len(untouched) == 1
    assert untouched[0]["work_email"] is None
    assert untouched[0]["provider_attempts"] == []

    usage = json.loads(completed.usage_path.read_text(encoding="utf-8"))
    assert usage["providers"]["clay"]["request_count"] == 2
    assert usage["providers"]["apollo"]["request_count"] == 1
    assert usage["providers"]["instantly"]["request_count"] == 2

    replay = run_contact_enrichment(
        config,
        clay=_BombClay(),
        apollo=_BombApollo(),
        instantly=_BombInstantly(),
    )
    assert replay.status == "completed"


def test_persisted_paused_unknown_freezes_when_operation_evidence_is_torn(
    tmp_path: Path,
) -> None:
    run_id = "torn-unknown"
    _discover(tmp_path, run_id)
    checkpoint_path = tmp_path / run_id / "contact_discovery_checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["status"] = "paused_unknown"
    checkpoint["pause_reason"] = "apollo:ctc_123"
    checkpoint["provider_state"]["operations"] = {}
    checkpoint_path.write_text(
        json.dumps(checkpoint, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    frozen = run_contact_enrichment(
        _config(tmp_path, run_id),
        clay=_BombClay(),
        apollo=_BombApollo(),
        instantly=_BombInstantly(),
    )

    assert frozen.status == "paused_unknown"
    persisted = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "paused_unknown"
    assert persisted["pause_reason"] == "apollo:ctc_123"


def test_crash_after_clay_intent_freezes_without_replay(tmp_path: Path) -> None:
    run_id = "clay-crash"
    _discover(tmp_path, run_id)

    class CrashClay:
        def start(self, contacts: list[ContactRecord]) -> ClayStartResult:
            assert contacts
            raise KeyboardInterrupt

        def results(self, routine_run_id: str) -> ClayResults:
            raise AssertionError(routine_run_id)

    with pytest.raises(KeyboardInterrupt):
        run_contact_enrichment(
            _config(tmp_path, run_id),
            clay=CrashClay(),
            apollo=_BombApollo(),
            instantly=_BombInstantly(),
        )

    resumed = run_contact_enrichment(
        _config(tmp_path, run_id),
        clay=_BombClay(),
        apollo=_BombApollo(),
        instantly=_BombInstantly(),
    )
    assert resumed.status == "paused_unknown"


def test_ambiguous_clay_results_freeze_global_paid_work(tmp_path: Path) -> None:
    run_id = "clay-results-unknown"
    _discover(tmp_path, run_id)

    class AmbiguousClay(_Clay):
        def results(self, routine_run_id: str) -> ClayResults:
            self.result_calls += 1
            raise ContactProviderError(
                provider="clay",
                operation="work_email_routine_results",
                kind="transient",
                retryable=False,
                status_code=None,
                usage_event=_usage(
                    "clay",
                    "work_email_routine_results",
                    routine_run_id=routine_run_id,
                ),
            )

    clay = AmbiguousClay()
    config = _config(tmp_path, run_id)
    assert run_contact_enrichment(
        config,
        clay=clay,
        apollo=_BombApollo(),
        instantly=_BombInstantly(),
    ).status == "paused_pending"

    ambiguous = run_contact_enrichment(
        config,
        clay=clay,
        apollo=_BombApollo(),
        instantly=_BombInstantly(),
    )
    assert ambiguous.status == "paused_unknown"
    assert clay.result_calls == 1

    frozen = run_contact_enrichment(
        config,
        clay=clay,
        apollo=_BombApollo(),
        instantly=_BombInstantly(),
    )
    assert frozen.status == "paused_unknown"
    assert clay.result_calls == 1


def test_usage_repair_and_paid_boundary_expansion_need_no_special_migration(
    tmp_path: Path,
) -> None:
    run_id = "expand"
    _discover(tmp_path, run_id)
    initial_clay = _Clay(prefix="initial")
    initial = _config(
        tmp_path,
        run_id,
        max_paid=1,
        clay_cap=1,
        apollo_cap=0.0,
        instantly_cap=1,
    )

    assert run_contact_enrichment(
        initial,
        clay=initial_clay,
        apollo=_BombApollo(),
        instantly=_Instantly(),
    ).status == "paused_pending"
    completed = run_contact_enrichment(
        initial,
        clay=initial_clay,
        apollo=_BombApollo(),
        instantly=_Instantly(),
    )
    assert completed.status == "completed"

    completed.usage_path.write_text("{", encoding="utf-8")
    repaired = run_contact_enrichment(
        initial,
        clay=_BombClay(),
        apollo=_BombApollo(),
        instantly=_BombInstantly(),
    )
    assert repaired.status == "completed"
    json.loads(repaired.usage_path.read_text(encoding="utf-8"))

    expanded_clay = _Clay(prefix="expanded")
    expanded = _config(
        tmp_path,
        run_id,
        max_paid=2,
        clay_cap=2,
        apollo_cap=0.0,
        instantly_cap=2,
    )
    resumed = run_contact_enrichment(
        expanded,
        clay=expanded_clay,
        apollo=_BombApollo(),
        instantly=_BombInstantly(),
    )
    assert resumed.status == "paused_pending"
    assert len(expanded_clay.started) == 1
    assert len(expanded_clay.started[0]) == 1


def test_instantly_pending_status_read_respects_provider_wide_call_cap(
    tmp_path: Path,
) -> None:
    run_id = "instantly-cap"
    _discover(tmp_path, run_id)
    clay = _Clay()
    instantly = _Instantly(pending=True)
    config = _config(
        tmp_path,
        run_id,
        max_paid=1,
        clay_cap=1,
        apollo_cap=0.0,
        instantly_cap=1,
    )

    assert run_contact_enrichment(
        config,
        clay=clay,
        apollo=_BombApollo(),
        instantly=instantly,
    ).status == "paused_pending"

    pending = run_contact_enrichment(
        config,
        clay=clay,
        apollo=_BombApollo(),
        instantly=instantly,
    )
    assert pending.status == "paused_pending"
    assert len(instantly.created) == 1

    capped = run_contact_enrichment(
        config,
        clay=clay,
        apollo=_BombApollo(),
        instantly=instantly,
    )
    assert capped.status == "paused_budget"
    assert instantly.read == []
