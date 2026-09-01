"""Adversarial cross-field replay-consistency probes for M4."""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from m3_factories import accepted_facts, build_company
from m4_contract_fixtures import prepare_evaluated_run

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.providers import (
    ApolloResult,
    ClayResults,
    ClayStartResult,
    ExaPeopleResult,
    VerificationResult,
)
from leads_discovery.models import CompanyRecord, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.contact_enrichment import (
    ContactEnrichmentConfig,
    ContactEnrichmentSummary,
    run_contact_enrichment,
)


class _ExaRecorder:
    """Record Exa calls while returning a harmless empty result."""

    def __init__(self) -> None:
        """Initialize the call counter."""
        self.calls = 0

    def search(self, company: CompanyRecord) -> ExaPeopleResult:
        """Return an empty result if replay safety unexpectedly permits Exa."""
        self.calls += 1
        return ExaPeopleResult(
            results=[],
            usage_event=UsageEvent(
                provider="exa",
                operation="people_search",
                estimated_cost_usd=0.001,
                metadata={"company_id": company.company_id, "result_count": 0},
            ),
        )


class _ClayRecorder:
    """Record Clay POST/GET calls without any external network."""

    def __init__(self) -> None:
        """Initialize POST and GET counters."""
        self.starts = 0
        self.results_calls = 0

    def start(self, contacts: list[ContactRecord]) -> ClayStartResult:
        """Return one inert pending run if a forbidden fresh Clay start occurs."""
        self.starts += 1
        return ClayStartResult(
            routine_run_id="red-team-unexpected-clay-run",
            usage_event=UsageEvent(
                provider="clay",
                operation="work_email_routine_start",
                metadata={"submitted_contacts": len(contacts)},
            ),
        )

    def results(self, routine_run_id: str) -> ClayResults:
        """Return pending if a forbidden Clay resume occurs."""
        self.results_calls += 1
        return ClayResults(
            status="pending",
            items=[],
            usage_event=UsageEvent(
                provider="clay",
                operation="work_email_routine_results",
                metadata={"routine_run_id": routine_run_id},
            ),
        )


class _ApolloRecorder:
    """Record Apollo calls while returning a harmless miss."""

    def __init__(self) -> None:
        """Initialize the call counter."""
        self.calls = 0

    def enrich(self, contact: ContactRecord) -> ApolloResult:
        """Return a one-credit miss if replay safety unexpectedly permits Apollo."""
        self.calls += 1
        return ApolloResult(
            work_email=None,
            credits_used=1.0,
            usage_event=UsageEvent(
                provider="apollo",
                operation="people_enrichment",
                metadata={"contact_id": contact.contact_id, "credits_used": 1.0},
            ),
        )


class _InstantlyRecorder:
    """Record Instantly verification POST/GET calls without external network."""

    def __init__(self) -> None:
        """Initialize POST and GET counters."""
        self.creates = 0
        self.gets = 0

    def create(self, email: str) -> VerificationResult:
        """Return pending if a forbidden verification POST occurs."""
        self.creates += 1
        return VerificationResult(
            status="pending",
            credits_used=1.0,
            usage_event=UsageEvent(
                provider="instantly",
                operation="email_verification_create",
                metadata={"email": email, "credits_used": 1.0},
            ),
        )

    def get(self, email: str) -> VerificationResult:
        """Return pending if a forbidden verification GET occurs."""
        self.gets += 1
        return VerificationResult(
            status="pending",
            credits_used=0.0,
            usage_event=UsageEvent(
                provider="instantly",
                operation="email_verification_get",
                metadata={"email": email, "credits_used": 0.0},
            ),
        )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write one strict deterministic JSON fixture."""
    path.write_text(
        json.dumps(payload, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _prepare_run(tmp_path: Path, run_id: str) -> Path:
    """Create one current M3-accepted company for replay probes."""
    return prepare_evaluated_run(
        tmp_path,
        run_id,
        [
            build_company(
                facts=accepted_facts(),
                company_id="cmp_replay",
                name="Replay Valve",
                domain="replay.example",
            )
        ],
    )


def _contact(*, work_email: str | None = None) -> ContactRecord:
    """Build one retained rank-one contact for later-provider state probes."""
    return ContactRecord(
        contact_id="ctc_replay",
        company_id="cmp_replay",
        company_name="Replay Valve",
        company_domain="replay.example",
        company_final_score=90.0,
        full_name="Replay Owner",
        title="Owner",
        decision_rank=1,
        decision_reason="direct_decision_maker:owner",
        current_employment_confirmed=True,
        work_email=work_email,
        email_source="clay" if work_email is not None else None,
    )


def _run_probe(
    tmp_path: Path,
    run_id: str,
) -> tuple[
    ContactEnrichmentSummary | None,
    _ExaRecorder,
    _ClayRecorder,
    _ApolloRecorder,
    _InstantlyRecorder,
]:
    """Run one replay probe and preserve provider counters even when validation rejects state."""
    exa = _ExaRecorder()
    clay = _ClayRecorder()
    apollo = _ApolloRecorder()
    instantly = _InstantlyRecorder()
    summary: ContactEnrichmentSummary | None = None
    with suppress(Exception):
        summary = run_contact_enrichment(
            ContactEnrichmentConfig(
                run_id=run_id,
                data_root=tmp_path,
                exa_people_budget_usd=1.0,
                execute_live=True,
            ),
            exa=exa,
            clay=clay,
            apollo=apollo,
            instantly=instantly,
        )
    return summary, exa, clay, apollo, instantly


def _assert_fail_closed_without_calls(
    result: tuple[
        ContactEnrichmentSummary | None,
        _ExaRecorder,
        _ClayRecorder,
        _ApolloRecorder,
        _InstantlyRecorder,
    ],
) -> None:
    """Require rejection or an unknown pause and zero provider activity."""
    summary, exa, clay, apollo, instantly = result
    if summary is not None:
        assert summary.status == "paused_unknown"
    assert exa.calls == 0
    assert clay.starts == 0
    assert clay.results_calls == 0
    assert apollo.calls == 0
    assert instantly.creates == 0
    assert instantly.gets == 0


@pytest.mark.parametrize(
    "pause_reason",
    [
        "exa:cmp_replay",
        "clay_start_unknown",
        "apollo:ctc_replay",
        "instantly:ctc_replay",
    ],
)
def test_paused_unknown_with_empty_operations_never_becomes_fresh_work(
    tmp_path: Path,
    pause_reason: str,
) -> None:
    """Unknown provider reasons without matching operation evidence fail closed globally."""
    run_id = f"empty-ops-{pause_reason.split(':', 1)[0]}"
    run_dir = _prepare_run(tmp_path, run_id)
    checkpoint = RunCheckpoint(
        run_id=run_id,
        status="paused_unknown",
        pause_reason=pause_reason,
        provider_state={"operations": {}},
    )
    _write_json(run_dir / "contact_checkpoint.json", checkpoint.to_dict())

    _assert_fail_closed_without_calls(_run_probe(tmp_path, run_id))


def test_paused_unknown_without_pause_reason_fails_closed(tmp_path: Path) -> None:
    """Top-level unknown status cannot omit the evidence locator that explains the pause."""
    run_id = "missing-pause-reason"
    run_dir = _prepare_run(tmp_path, run_id)
    checkpoint = RunCheckpoint(
        run_id=run_id,
        status="paused_unknown",
        provider_state={"operations": {"exa:cmp_replay": {"state": "in_flight"}}},
    )
    _write_json(run_dir / "contact_checkpoint.json", checkpoint.to_dict())

    _assert_fail_closed_without_calls(_run_probe(tmp_path, run_id))


@pytest.mark.parametrize(
    ("pending_company_id", "pending_stage"),
    [
        ("cmp_replay", None),
        (None, "contact_enrichment"),
        ("cmp_other", "research"),
    ],
)
def test_m2_pending_fields_cannot_contaminate_m4_unknown_state(
    tmp_path: Path,
    pending_company_id: str | None,
    pending_stage: str | None,
) -> None:
    """Mismatched M2 pending-company/stage fields must not authorize M4 replay."""
    run_id = "mismatched-pending-fields"
    run_dir = _prepare_run(tmp_path, run_id)
    checkpoint = RunCheckpoint(
        run_id=run_id,
        status="paused_unknown",
        pause_reason="exa:cmp_replay",
        pending_company_id=pending_company_id,
        pending_stage=pending_stage,
        provider_state={"operations": {"exa:cmp_replay": {"state": "in_flight"}}},
    )
    _write_json(run_dir / "contact_checkpoint.json", checkpoint.to_dict())

    _assert_fail_closed_without_calls(_run_probe(tmp_path, run_id))


def test_removed_operation_with_unknown_status_cannot_reopen_paid_work(tmp_path: Path) -> None:
    """Deleting durable operation evidence while retaining unknown status stays fail closed."""
    run_id = "removed-operation"
    run_dir = _prepare_run(tmp_path, run_id)
    payload = RunCheckpoint(
        run_id=run_id,
        status="paused_unknown",
        pause_reason="exa:cmp_replay",
        provider_state={"operations": {"exa:cmp_replay": {"state": "in_flight"}}},
    ).to_dict()
    payload["provider_state"]["operations"].pop("exa:cmp_replay")
    _write_json(run_dir / "contact_checkpoint.json", payload)

    _assert_fail_closed_without_calls(_run_probe(tmp_path, run_id))


@pytest.mark.parametrize(
    "provider_state",
    [
        pytest.param([], id="provider-state-list"),
        pytest.param({"operations": []}, id="operations-list"),
        pytest.param({"operations": "in_flight"}, id="operations-string"),
    ],
)
def test_malformed_provider_state_container_never_dispatches(
    tmp_path: Path,
    provider_state: object,
) -> None:
    """Wrong-type provider-state containers may crash closed but may never reach a provider."""
    run_id = "bad-provider-state"
    run_dir = _prepare_run(tmp_path, run_id)
    payload = RunCheckpoint(
        run_id=run_id,
        status="paused_unknown",
        pause_reason="exa:cmp_replay",
        provider_state={"operations": {"exa:cmp_replay": {"state": "in_flight"}}},
    ).to_dict()
    payload["provider_state"] = provider_state
    _write_json(run_dir / "contact_checkpoint.json", payload)

    _assert_fail_closed_without_calls(_run_probe(tmp_path, run_id))


def test_completed_work_plus_unrelated_torn_unknown_state_stays_fail_closed(
    tmp_path: Path,
) -> None:
    """Valid completed work cannot mask an unrelated unknown reason lacking its operation."""
    run_id = "completed-plus-torn"
    run_dir = _prepare_run(tmp_path, run_id)
    checkpoint = RunCheckpoint(
        run_id=run_id,
        status="paused_unknown",
        pause_reason="apollo:ctc_missing",
        provider_state={
            "operations": {
                "exa:cmp_replay": {"state": "completed", "contact_ids": []}
            }
        },
    )
    _write_json(run_dir / "contact_checkpoint.json", checkpoint.to_dict())

    _assert_fail_closed_without_calls(_run_probe(tmp_path, run_id))


@pytest.mark.parametrize("later_provider", ["clay", "apollo", "instantly"])
def test_later_unknown_state_cannot_reopen_missing_exa_work(
    tmp_path: Path,
    later_provider: str,
) -> None:
    """A valid later-provider unknown marker must freeze missing earlier paid work too."""
    run_id = f"later-unknown-{later_provider}"
    run_dir = _prepare_run(tmp_path, run_id)
    email = "replay.owner@replay.example" if later_provider == "instantly" else None
    contact = _contact(work_email=email)
    _write_json(run_dir / "contacts.jsonl", contact.to_dict())

    if later_provider == "clay":
        reason = "clay_start_unknown"
        operations: dict[str, Any] = {
            "clay:batch": {
                "state": "in_flight",
                "contact_ids": [contact.contact_id],
            }
        }
    elif later_provider == "apollo":
        reason = f"apollo:{contact.contact_id}"
        operations = {
            reason: {"state": "in_flight", "credits_reserved": 1.0}
        }
    else:
        assert email is not None
        reason = f"instantly:{contact.contact_id}"
        operations = {
            reason: {"state": "in_flight", "email": email}
        }
    checkpoint = RunCheckpoint(
        run_id=run_id,
        status="paused_unknown",
        pause_reason=reason,
        provider_state={"operations": operations},
    )
    _write_json(run_dir / "contact_checkpoint.json", checkpoint.to_dict())

    _assert_fail_closed_without_calls(_run_probe(tmp_path, run_id))
