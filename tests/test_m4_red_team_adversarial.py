"""Adversarial M4 integration regressions derived from the written contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NoReturn

import pytest
from m3_factories import accepted_facts, build_company, low_score_facts
from m4_contract_fixtures import prepare_evaluated_run, read_jsonl

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.providers import ClayStartResult, ExaPeopleResult
from leads_discovery.contacts.selection import rank_title
from leads_discovery.models import CompanyRecord, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.contact_enrichment import (
    ContactEnrichmentConfig,
    ContactEnrichmentSummary,
    run_contact_enrichment,
)


class _RejectExa:
    """Reject any Exa dispatch when the current M3 state does not authorize it."""

    def __init__(self) -> None:
        """Initialize the call counter."""
        self.calls = 0

    def search(self, company: CompanyRecord) -> NoReturn:
        """Fail immediately if an unauthorized company reaches People Search."""
        self.calls += 1
        raise AssertionError(f"unexpected Exa search for {company.company_id}")


class _RecordingExa:
    """Record Exa dispatches while returning one valid empty People Search result."""

    def __init__(self) -> None:
        """Initialize the call counter."""
        self.calls = 0

    def search(self, company: CompanyRecord) -> ExaPeopleResult:
        """Return an empty result so any replay is observable without further providers."""
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


class _RecordingClay:
    """Record Clay starts while rejecting any unauthorized result poll."""

    def __init__(self) -> None:
        """Initialize the ordered submitted-contact batches."""
        self.starts: list[list[str]] = []

    def start(self, contacts: list[ContactRecord]) -> ClayStartResult:
        """Record an otherwise valid Clay start without reaching any network."""
        ids = [contact.contact_id for contact in contacts]
        self.starts.append(ids)
        return ClayStartResult(
            routine_run_id="red-team-must-not-run",
            usage_event=UsageEvent(
                provider="clay",
                operation="work_email_routine_start",
                metadata={"submitted_contacts": len(ids)},
            ),
        )

    def results(self, routine_run_id: str) -> NoReturn:
        """Reject result polling when the current M3 state no longer authorizes it."""
        raise AssertionError(f"unexpected Clay result poll for {routine_run_id}")


class _RejectApollo:
    """Reject any Apollo dispatch in an unauthorized or replay regression scenario."""

    def enrich(self, contact: ContactRecord) -> NoReturn:
        """Fail immediately if a stale contact reaches Apollo."""
        raise AssertionError(f"unexpected Apollo enrichment for {contact.contact_id}")


class _RejectInstantly:
    """Reject any Instantly dispatch in an unauthorized or replay regression scenario."""

    def create(self, email: str) -> NoReturn:
        """Fail immediately if a stale email reaches verification creation."""
        raise AssertionError(f"unexpected Instantly POST for {email}")

    def get(self, email: str) -> NoReturn:
        """Fail immediately if a stale email reaches verification polling."""
        raise AssertionError(f"unexpected Instantly GET for {email}")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write one strict deterministic JSON object for a red-team state fixture."""
    path.write_text(
        json.dumps(payload, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _stale_contact(*, rank: int = 1, work_email: str | None = None) -> ContactRecord:
    """Build one previously retained contact for the transition/replay probes."""
    return ContactRecord(
        contact_id="ctc_stale_owner",
        company_id="cmp_stale",
        company_name="Stale Valve",
        company_domain="stale.example",
        company_final_score=90.0,
        full_name="Stale Owner" if rank == 1 else "Stale Sales VP",
        title="Owner" if rank == 1 else "VP Sales",
        decision_rank=rank,
        decision_reason=(
            "direct_decision_maker:owner"
            if rank == 1
            else "functional_decision_maker:sales"
        ),
        current_employment_confirmed=True,
        work_email=work_email,
        email_source="clay" if work_email is not None else None,
    )


def _change_current_m3_decision(run_dir: Path, decision: str) -> None:
    """Change the one canonical M3 decision while preserving its completed decision stage."""
    evaluated = read_jsonl(run_dir / "companies_evaluated.jsonl")
    assert len(evaluated) == 1
    assert evaluated[0]["final_decision"] == "accepted"
    evaluated[0]["final_decision"] = decision
    _write_json(run_dir / "companies_evaluated.jsonl", evaluated[0])


def _run_with_rejecting_providers(
    *,
    tmp_path: Path,
    run_id: str,
) -> tuple[ContactEnrichmentSummary, _RejectExa, _RecordingClay]:
    """Run one probe whose provider fakes make every unauthorized dispatch observable."""
    exa = _RejectExa()
    clay = _RecordingClay()
    summary = run_contact_enrichment(
        ContactEnrichmentConfig(run_id=run_id, data_root=tmp_path, execute_live=True),
        exa=exa,
        clay=clay,
        apollo=_RejectApollo(),
        instantly=_RejectInstantly(),
    )
    return summary, exa, clay


def test_stale_contact_for_nonaccepted_company_never_reaches_paid_provider(
    tmp_path: Path,
) -> None:
    """A stale contacts snapshot cannot bypass the canonical M3 accepted-only gate."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "stale-rejected-contact",
        [
            build_company(
                facts=low_score_facts(),
                company_id="cmp_stale",
                name="Stale Valve",
                domain="stale.example",
            )
        ],
    )
    evaluated = read_jsonl(run_dir / "companies_evaluated.jsonl")
    assert len(evaluated) == 1
    assert evaluated[0]["final_decision"] != "accepted"
    _write_json(run_dir / "contacts.jsonl", _stale_contact().to_dict())

    summary, exa, clay = _run_with_rejecting_providers(
        tmp_path=tmp_path,
        run_id="stale-rejected-contact",
    )

    assert summary.status == "completed"
    assert exa.calls == 0
    assert clay.starts == []


@pytest.mark.parametrize("decision", ["uncertain", "rejected"])
@pytest.mark.parametrize("rank", [1, 2])
@pytest.mark.parametrize("partial", ["clay_pending", "apollo_in_flight", "instantly_pending"])
def test_current_nonaccepted_transition_blocks_all_stale_partial_provider_state(
    tmp_path: Path,
    decision: str,
    rank: int,
    partial: str,
) -> None:
    """Accepted-to-nonaccepted transitions cannot resume stale paid provider state."""
    run_id = f"transition-{decision}-{rank}-{partial}"
    run_dir = prepare_evaluated_run(
        tmp_path,
        run_id,
        [
            build_company(
                facts=accepted_facts(),
                company_id="cmp_stale",
                name="Stale Valve",
                domain="stale.example",
            )
        ],
    )
    email = "stale.owner@stale.example" if partial == "instantly_pending" else None
    contact = _stale_contact(rank=rank, work_email=email)
    if partial == "clay_pending":
        contact.provider_attempts.append(
            {"provider": "clay", "operation": "work_email_routine", "state": "in_flight"}
        )
    elif partial == "apollo_in_flight":
        contact.provider_attempts.extend(
            [
                {"provider": "clay", "operation": "work_email_routine", "state": "completed"},
                {"provider": "apollo", "operation": "people_enrichment", "state": "in_flight"},
            ]
        )
    else:
        contact.provider_attempts.extend(
            [
                {"provider": "clay", "operation": "work_email_routine", "state": "completed"},
                {"provider": "instantly", "operation": "email_verification", "state": "pending"},
            ]
        )
    _write_json(run_dir / "contacts.jsonl", contact.to_dict())

    operations: dict[str, Any] = {
        "exa:cmp_stale": {"state": "completed", "contact_ids": [contact.contact_id]}
    }
    if partial == "clay_pending":
        status = "paused_pending"
        pause_reason = "clay_pending"
        operations["clay:batch"] = {
            "state": "pending",
            "routine_run_id": "stale-routine-123",
            "contact_ids": [contact.contact_id],
        }
    elif partial == "apollo_in_flight":
        status = "paused_unknown"
        pause_reason = f"apollo:{contact.contact_id}"
        operations[pause_reason] = {
            "state": "in_flight",
            "credits_reserved": 1.0,
        }
    else:
        assert email is not None
        status = "paused_pending"
        pause_reason = f"instantly:{contact.contact_id}"
        operations["clay:batch"] = {
            "state": "completed",
            "routine_run_id": "stale-routine-completed",
            "contact_ids": [contact.contact_id],
        }
        operations[pause_reason] = {
            "state": "pending",
            "email": email,
        }
    checkpoint = RunCheckpoint(
        run_id=run_id,
        status=status,
        pause_reason=pause_reason,
        provider_state={"operations": operations},
    )
    _write_json(run_dir / "contact_checkpoint.json", checkpoint.to_dict())

    _change_current_m3_decision(run_dir, decision)
    summary, exa, clay = _run_with_rejecting_providers(tmp_path=tmp_path, run_id=run_id)

    assert summary.status in {"completed", "paused_unknown"}
    assert exa.calls == 0
    assert clay.starts == []


def test_malformed_operation_state_fails_closed_without_exa_replay(tmp_path: Path) -> None:
    """An unknown persisted operation state cannot be treated as permission to dispatch again."""
    run_id = "corrupt-exa-state"
    run_dir = prepare_evaluated_run(
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
    checkpoint = RunCheckpoint(
        run_id=run_id,
        provider_state={
            "operations": {
                "exa:cmp_replay": {"state": "corrupt_after_possible_dispatch"}
            }
        },
    )
    _write_json(run_dir / "contact_checkpoint.json", checkpoint.to_dict())
    prior = UsageEvent(
        provider="exa",
        operation="people_search",
        estimated_cost_usd=0.001,
        metadata={"company_id": "cmp_replay", "result_count": 0},
    )
    _write_json(run_dir / "contact_usage_events.jsonl", prior.to_dict())
    exa = _RecordingExa()

    with pytest.raises(ValueError):
        run_contact_enrichment(
            ContactEnrichmentConfig(
                run_id=run_id,
                data_root=tmp_path,
                exa_people_budget_usd=1.0,
                execute_live=True,
            ),
            exa=exa,
            clay=_RecordingClay(),
            apollo=_RejectApollo(),
            instantly=_RejectInstantly(),
        )

    assert exa.calls == 0


@pytest.mark.parametrize(
    "operation_value",
    [
        pytest.param({}, id="missing-state"),
        pytest.param({"state": 7}, id="state-wrong-type"),
        pytest.param("in_flight", id="operation-wrong-type"),
        pytest.param({"state": "in_flight", "unexpected": True}, id="extra-field"),
        pytest.param({"state": "completed"}, id="partial-completed"),
    ],
)
def test_malformed_operation_shapes_never_dispatch_exa(
    tmp_path: Path,
    operation_value: object,
) -> None:
    """Missing, wrong-type, and partially formed operation data all fail closed."""
    run_id = "malformed-operation-shape"
    run_dir = prepare_evaluated_run(
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
    checkpoint = RunCheckpoint(
        run_id=run_id,
        status="paused_unknown",
        pause_reason="exa:cmp_replay",
        provider_state={"operations": {"exa:cmp_replay": operation_value}},
    )
    _write_json(run_dir / "contact_checkpoint.json", checkpoint.to_dict())
    _write_json(
        run_dir / "contact_usage_events.jsonl",
        UsageEvent(
            provider="exa",
            operation="people_search",
            estimated_cost_usd=0.001,
            metadata={"company_id": "cmp_replay", "result_count": 0},
        ).to_dict(),
    )
    exa = _RecordingExa()

    with pytest.raises(ValueError):
        run_contact_enrichment(
            ContactEnrichmentConfig(
                run_id=run_id,
                data_root=tmp_path,
                exa_people_budget_usd=1.0,
                execute_live=True,
            ),
            exa=exa,
            clay=_RecordingClay(),
            apollo=_RejectApollo(),
            instantly=_RejectInstantly(),
        )

    assert exa.calls == 0


@pytest.mark.parametrize("bad_status", ["mystery", "", None, 7])
def test_malformed_checkpoint_status_never_dispatches_exa(
    tmp_path: Path,
    bad_status: object,
) -> None:
    """Unknown and wrong-type checkpoint statuses fail before any provider dispatch."""
    run_id = "bad-checkpoint-status"
    run_dir = prepare_evaluated_run(
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
    payload = RunCheckpoint(
        run_id=run_id,
        provider_state={"operations": {}},
    ).to_dict()
    payload["status"] = bad_status
    _write_json(run_dir / "contact_checkpoint.json", payload)
    exa = _RecordingExa()

    with pytest.raises(ValueError):
        run_contact_enrichment(
            ContactEnrichmentConfig(run_id=run_id, data_root=tmp_path, execute_live=True),
            exa=exa,
            clay=_RecordingClay(),
            apollo=_RejectApollo(),
            instantly=_RejectInstantly(),
        )

    assert exa.calls == 0


def test_paused_unknown_checkpoint_missing_operation_evidence_cannot_resume_as_fresh(
    tmp_path: Path,
) -> None:
    """A torn unknown-outcome checkpoint cannot silently downgrade into fresh paid work."""
    run_id = "torn-paused-unknown"
    run_dir = prepare_evaluated_run(
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
    checkpoint = RunCheckpoint(
        run_id=run_id,
        status="paused_unknown",
        pause_reason="exa:cmp_replay",
        provider_state={"operations": {}},
    )
    _write_json(run_dir / "contact_checkpoint.json", checkpoint.to_dict())
    exa = _RecordingExa()

    with pytest.raises(ValueError):
        run_contact_enrichment(
            ContactEnrichmentConfig(
                run_id=run_id,
                data_root=tmp_path,
                exa_people_budget_usd=1.0,
                execute_live=True,
            ),
            exa=exa,
            clay=_RecordingClay(),
            apollo=_RejectApollo(),
            instantly=_RejectInstantly(),
        )

    assert exa.calls == 0


@pytest.mark.parametrize(
    ("canonical", "punctuated"),
    [
        ("VP Sales", "V.P., Sales"),
        ("Vice President, Operations", "Vice-President, Operations"),
    ],
)
def test_title_punctuation_does_not_change_decision_proximity(
    canonical: str,
    punctuated: str,
) -> None:
    """Common punctuation variants classify identically to the same canonical title."""
    expected = rank_title(canonical)
    assert expected is not None
    assert rank_title(punctuated) == expected
