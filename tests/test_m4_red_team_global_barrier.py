"""Adversarial global replay-barrier and prerequisite probes for M4."""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from typing import Any, NoReturn

import pytest
from m3_factories import accepted_facts, build_company
from m4_contract_fixtures import prepare_evaluated_run

from leads_discovery.contacts.models import ContactRecord
from leads_discovery.models import CompanyRecord, RunCheckpoint
from leads_discovery.pipeline.contact_enrichment import (
    ContactEnrichmentConfig,
    ContactEnrichmentSummary,
    run_contact_enrichment,
)


class _RejectExa:
    """Count and reject every unexpected Exa dispatch."""

    def __init__(self) -> None:
        """Initialize the dispatch counter."""
        self.calls = 0

    def search(self, company: CompanyRecord) -> NoReturn:
        """Record an unexpected Exa call and abort the probe."""
        self.calls += 1
        raise AssertionError(f"unexpected Exa dispatch for {company.company_id}")


class _RejectClay:
    """Count and reject every unexpected Clay POST or GET."""

    def __init__(self) -> None:
        """Initialize Clay call counters."""
        self.starts = 0
        self.results_calls = 0

    def start(self, contacts: list[ContactRecord]) -> NoReturn:
        """Record an unexpected Clay POST and abort the probe."""
        self.starts += 1
        raise AssertionError(f"unexpected Clay start for {len(contacts)} contacts")

    def results(self, routine_run_id: str) -> NoReturn:
        """Record an unexpected Clay GET and abort the probe."""
        self.results_calls += 1
        raise AssertionError(f"unexpected Clay GET for {routine_run_id}")


class _RejectApollo:
    """Count and reject every unexpected Apollo dispatch."""

    def __init__(self) -> None:
        """Initialize the dispatch counter."""
        self.calls = 0

    def enrich(self, contact: ContactRecord) -> NoReturn:
        """Record an unexpected Apollo call and abort the probe."""
        self.calls += 1
        raise AssertionError(f"unexpected Apollo dispatch for {contact.contact_id}")


class _RejectInstantly:
    """Count and reject every unexpected Instantly POST or GET."""

    def __init__(self) -> None:
        """Initialize verification call counters."""
        self.creates = 0
        self.gets = 0

    def create(self, email: str) -> NoReturn:
        """Record an unexpected verification POST and abort the probe."""
        self.creates += 1
        raise AssertionError(f"unexpected Instantly POST for {email}")

    def get(self, email: str) -> NoReturn:
        """Record an unexpected verification GET and abort the probe."""
        self.gets += 1
        raise AssertionError(f"unexpected Instantly GET for {email}")


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
    """Build one retained paid-eligible contact for durable-stage probes."""
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
    _RejectExa,
    _RejectClay,
    _RejectApollo,
    _RejectInstantly,
]:
    """Run one probe while preserving all provider counters on failure."""
    exa = _RejectExa()
    clay = _RejectClay()
    apollo = _RejectApollo()
    instantly = _RejectInstantly()
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


def _assert_zero_provider_calls(
    result: tuple[
        ContactEnrichmentSummary | None,
        _RejectExa,
        _RejectClay,
        _RejectApollo,
        _RejectInstantly,
    ],
) -> ContactEnrichmentSummary | None:
    """Require zero activity across every M4 provider and return the optional summary."""
    summary, exa, clay, apollo, instantly = result
    assert exa.calls == 0
    assert clay.starts == 0
    assert clay.results_calls == 0
    assert apollo.calls == 0
    assert instantly.creates == 0
    assert instantly.gets == 0
    return summary


def test_structurally_valid_exa_paused_unknown_is_global_provider_barrier(
    tmp_path: Path,
) -> None:
    """A valid unresolved Exa outcome returns paused_unknown without any provider dispatch."""
    run_id = "valid-exa-unknown-barrier"
    run_dir = _prepare_run(tmp_path, run_id)
    checkpoint = RunCheckpoint(
        run_id=run_id,
        status="paused_unknown",
        pause_reason="exa:cmp_replay",
        provider_state={"operations": {"exa:cmp_replay": {"state": "in_flight"}}},
    )
    _write_json(run_dir / "contact_checkpoint.json", checkpoint.to_dict())

    summary = _assert_zero_provider_calls(_run_probe(tmp_path, run_id))
    assert summary is not None
    assert summary.status == "paused_unknown"


def test_m2_unknown_outcome_freezes_m4_before_any_provider_dispatch(tmp_path: Path) -> None:
    """An unresolved upstream paid outcome bars every downstream M4 paid edge."""
    run_id = "m2-unknown-barrier"
    run_dir = _prepare_run(tmp_path, run_id)
    checkpoint = RunCheckpoint(
        run_id=run_id,
        status="paused_unknown",
        pause_reason="unknown_in_flight:extraction:cmp_replay",
        provider_state={
            "operations": {"extraction:cmp_replay": {"state": "in_flight"}}
        },
    )
    _write_json(run_dir / "checkpoint.json", checkpoint.to_dict())

    summary = _assert_zero_provider_calls(_run_probe(tmp_path, run_id))
    assert summary is not None
    assert summary.status == "paused_unknown"


@pytest.mark.parametrize("later_stage", ["clay", "instantly"])
@pytest.mark.parametrize("exa_evidence", ["missing", "torn-selection"])
def test_paused_pending_with_missing_exa_prerequisite_never_replays(
    tmp_path: Path,
    later_stage: str,
    exa_evidence: str,
) -> None:
    """Later pending work with missing/torn Exa provenance fails closed before all providers."""
    run_id = f"pending-{later_stage}-{exa_evidence}"
    run_dir = _prepare_run(tmp_path, run_id)
    email = "replay.owner@replay.example" if later_stage == "instantly" else None
    contact = _contact(work_email=email)
    _write_json(run_dir / "contacts.jsonl", contact.to_dict())

    operations: dict[str, Any] = {}
    if exa_evidence == "torn-selection":
        operations["exa:cmp_replay"] = {"state": "completed", "contact_ids": []}

    if later_stage == "clay":
        operations["clay:batch"] = {
            "state": "pending",
            "routine_run_id": "clay-pending-run",
            "contact_ids": [contact.contact_id],
        }
        reason = "clay_pending"
    else:
        assert email is not None
        operations["clay:batch"] = {
            "state": "completed",
            "routine_run_id": "clay-completed-run",
            "contact_ids": [contact.contact_id],
        }
        reason = f"instantly:{contact.contact_id}"
        operations[reason] = {"state": "pending", "email": email}

    checkpoint = RunCheckpoint(
        run_id=run_id,
        status="paused_pending",
        pause_reason=reason,
        provider_state={"operations": operations},
    )
    _write_json(run_dir / "contact_checkpoint.json", checkpoint.to_dict())

    summary = _assert_zero_provider_calls(_run_probe(tmp_path, run_id))
    assert summary is None
