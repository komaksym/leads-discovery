"""Behavioral contracts for accepted-only deterministic contact discovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leads_discovery.contacts.providers import ExaPeopleResult
from leads_discovery.models import CompanyRecord, UsageEvent
from leads_discovery.pipeline.contact_discovery import (
    ContactDiscoveryConfig,
    run_contact_discovery,
)


def _company(company_id: str, name: str, decision: str) -> CompanyRecord:
    return CompanyRecord(
        company_id=company_id,
        name=name,
        normalized_name=name.casefold(),
        domain=f"{company_id}.example",
        normalized_domain=f"{company_id}.example",
        country="US",
        stage_status={"decision": "completed"},
        final_score=80.0 if decision == "accepted" else 40.0,
        final_decision=decision,
    )


def _write_evaluated(run_dir: Path, companies: list[CompanyRecord]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(company.to_dict(), sort_keys=True) + "\n" for company in companies
    )
    (run_dir / "companies_evaluated.jsonl").write_text(text, encoding="utf-8")


def _person(
    company: CompanyRecord,
    *,
    person_id: str,
    name: str,
    title: str,
    current: bool = True,
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
                            "dates": {"to": None if current else "2025-01"},
                            "title": title,
                        }
                    ],
                },
            }
        ],
    }


def _result(company: CompanyRecord) -> ExaPeopleResult:
    return ExaPeopleResult(
        results=[
            _person(company, person_id="manager", name="Zed Manager", title="Branch Manager"),
            _person(company, person_id="president", name="Amy President", title="President"),
            _person(
                company,
                person_id="former",
                name="Former Owner",
                title="Owner",
                current=False,
            ),
            _person(
                company,
                person_id="operations",
                name="Ben Operations",
                title="VP Operations",
            ),
        ],
        usage_event=UsageEvent(
            provider="exa",
            operation="people_search",
            request_count=1,
            estimated_cost_usd=0.0,
            metadata={"company_id": company.company_id},
        ),
    )


def _config(tmp_path: Path, run_id: str, **overrides: object) -> ContactDiscoveryConfig:
    values: dict[str, object] = {
        "run_id": run_id,
        "data_root": tmp_path,
        "execute_live": True,
    }
    values.update(overrides)
    return ContactDiscoveryConfig(**values)  # type: ignore[arg-type]


def test_rejected_and_uncertain_companies_make_zero_people_calls(tmp_path: Path) -> None:
    run_id = "accepted-only"
    _write_evaluated(
        tmp_path / run_id,
        [
            _company("rejected", "Rejected Co", "rejected"),
            _company("uncertain", "Uncertain Co", "uncertain"),
        ],
    )
    calls = 0

    def bomb(_company: CompanyRecord) -> ExaPeopleResult:
        nonlocal calls
        calls += 1
        raise AssertionError("non-accepted companies must not authorize people search")

    summary = run_contact_discovery(_config(tmp_path, run_id), exa_search=bomb)

    assert summary.status == "completed"
    assert summary.accepted_company_count == 0
    assert summary.contact_count == 0
    assert calls == 0


def test_shortlist_is_current_employment_only_ranked_and_capped(tmp_path: Path) -> None:
    run_id = "ranking"
    company = _company("accepted", "Accepted Co", "accepted")
    _write_evaluated(tmp_path / run_id, [company])

    summary = run_contact_discovery(
        _config(tmp_path, run_id, max_contacts_per_company=2),
        exa_search=_result,
    )
    rows = [
        json.loads(line)
        for line in summary.contacts_path.read_text(encoding="utf-8").splitlines()
        if line
    ]

    assert [row["full_name"] for row in rows] == ["Amy President", "Ben Operations"]
    assert all(row["current_employment_confirmed"] is True for row in rows)
    assert summary.contact_count == 2


def test_changed_m3_accepted_set_invalidates_stale_completed_shortlist(tmp_path: Path) -> None:
    run_id = "stale-input"
    run_dir = tmp_path / run_id
    first_company = _company("alpha", "Alpha Co", "accepted")
    second_company = _company("beta", "Beta Co", "accepted")
    _write_evaluated(run_dir, [first_company])
    calls: list[str] = []

    def search(company: CompanyRecord) -> ExaPeopleResult:
        calls.append(company.company_id)
        return _result(company)

    first = run_contact_discovery(_config(tmp_path, run_id), exa_search=search)
    assert first.status == "completed"
    assert calls == ["alpha"]

    _write_evaluated(
        run_dir,
        [
            _company("alpha", "Alpha Co", "rejected"),
            second_company,
        ],
    )
    second = run_contact_discovery(_config(tmp_path, run_id), exa_search=search)
    assert second.status == "completed"
    assert calls == ["alpha", "beta"]
    rows = [
        json.loads(line)
        for line in second.contacts_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert {row["company_id"] for row in rows} == {"beta"}

    def bomb(_company: CompanyRecord) -> ExaPeopleResult:
        raise AssertionError("healthy completed replay must make zero provider calls")

    third = run_contact_discovery(_config(tmp_path, run_id), exa_search=bomb)
    assert third.status == "completed"
    assert third.contact_count == second.contact_count


def test_crash_after_durable_intent_freezes_resume_without_duplicate_call(
    tmp_path: Path,
) -> None:
    run_id = "unknown-outcome"
    company = _company("accepted", "Accepted Co", "accepted")
    _write_evaluated(tmp_path / run_id, [company])
    calls = 0

    def crash(_company: CompanyRecord) -> ExaPeopleResult:
        nonlocal calls
        calls += 1
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_contact_discovery(_config(tmp_path, run_id), exa_search=crash)
    assert calls == 1

    def bomb(_company: CompanyRecord) -> ExaPeopleResult:
        raise AssertionError("unknown paid outcome must not be replayed")

    resumed = run_contact_discovery(_config(tmp_path, run_id), exa_search=bomb)

    assert resumed.status == "paused_unknown"
    assert calls == 1


def test_exact_exa_people_budget_is_admitted(tmp_path: Path) -> None:
    run_id = "exact-budget"
    company = _company("accepted", "Accepted Co", "accepted")
    _write_evaluated(tmp_path / run_id, [company])
    calls = 0

    def search(candidate: CompanyRecord) -> ExaPeopleResult:
        nonlocal calls
        calls += 1
        return _result(candidate)

    completed = run_contact_discovery(
        _config(tmp_path, run_id, exa_people_budget_usd=0.017),
        exa_search=search,
    )

    assert completed.status == "completed"
    assert calls == 1
