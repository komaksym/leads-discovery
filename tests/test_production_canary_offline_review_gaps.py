"""Review-gap contract for the fixed production canary composition."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from m4_contract_fixtures import ClayRoutineScript, WireStub, json_body, read_jsonl
from test_production_canary_offline_contract import (
    _EMAIL,
    _PROFILE,
    _accepted_company,
    _exa_one,
    _install_contract,
    _person,
    _rejected_company,
    _run_canary,
    _terminal_instantly,
)

from leads_discovery import production_canary
from leads_discovery.contacts.selection import select_contacts
from leads_discovery.models import CompanyRecord, RunCheckpoint, UsageEvent
from leads_discovery.pipeline.canary_provider_coverage import (
    CanaryProviderCoverageSummary,
    run_live_provider_coverage,
)
from leads_discovery.pipeline.state import append_jsonl, write_checkpoint

_CANONICAL_AND_NORMAL_ARTIFACTS = (
    "companies_evaluated.jsonl",
    "checkpoint.json",
    "contacts.jsonl",
    "leads.csv",
    "contact_usage_events.jsonl",
    "contact_usage.json",
    "contact_checkpoint.json",
)


def _canonical_and_normal_snapshot(run_dir: Path) -> dict[str, bytes | None]:
    """Snapshot canonical artifacts plus authoritative normal M4 state, including absence."""
    snapshot: dict[str, bytes | None] = {}
    for name in _CANONICAL_AND_NORMAL_ARTIFACTS:
        path = run_dir / name
        snapshot[name] = path.read_bytes() if path.exists() else None
    return snapshot


def _write_contact_status(
    data_root: Path,
    run_id: str,
    status: str,
    *,
    pause_reason: str | None = None,
    operations: dict[str, object] | None = None,
) -> None:
    """Persist the durable normal-M4 state that controls canary-only resume admission."""
    run_dir = data_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_checkpoint(
        run_dir / "contact_checkpoint.json",
        RunCheckpoint(
            run_id=run_id,
            status=status,
            pause_reason=pause_reason,
            provider_state={"operations": operations or {}},
        ),
    )


def test_coverage_only_clay_verifies_selected_contact_without_mutating_normal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage Clay may feed verification but cannot authorize Apollo or mutate M4."""
    run_id = "canary-review-coverage-immutability"
    clay = ClayRoutineScript([{"work_email": _EMAIL}])

    def apollo(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "credits_used": 1,
                "person": {"email": _EMAIL, "linkedin_url": _PROFILE},
            },
        )

    stub = WireStub(
        {
            "exa": _exa_one,
            "clay": clay,
            "apollo": apollo,
            "instantly": _terminal_instantly("verified", expected_email=_EMAIL),
        }
    )
    run_dir = _install_contract(monkeypatch, tmp_path, run_id, _rejected_company(), stub)
    real_coverage = run_live_provider_coverage

    def coverage_with_snapshot(
        data_root: Path,
        *,
        run_id: str,
    ) -> CanaryProviderCoverageSummary:
        before = _canonical_and_normal_snapshot(data_root / run_id)
        summary = real_coverage(data_root, run_id=run_id)
        assert _canonical_and_normal_snapshot(data_root / run_id) == before
        return summary

    sleeps: list[float] = []

    def release_on_sleep(delay: float) -> None:
        sleeps.append(delay)
        clay.release_started()

    monkeypatch.setattr(
        production_canary,
        "run_live_provider_coverage",
        coverage_with_snapshot,
    )
    monkeypatch.setattr(production_canary, "sleep", release_on_sleep, raising=False)

    assert _run_canary(tmp_path, run_id) == 2
    assert len(sleeps) == 1

    evaluated_rows = read_jsonl(run_dir / "companies_evaluated.jsonl")
    assert len(evaluated_rows) == 1
    evaluated = CompanyRecord.from_dict(evaluated_rows[0])
    expected_selected = select_contacts(evaluated, [_person()], limit=1)
    assert len(expected_selected) == 1
    expected = expected_selected[0]

    assert len(clay.posts) == 1
    assert len(clay.gets) == 1
    routine_run_id = clay.latest_run_id
    assert routine_run_id is not None
    assert clay.gets[0].url.path == f"/public/v0/routines/run/{routine_run_id}/results"
    clay_item = json_body(clay.posts[0])["items"][0]
    assert clay_item["id"] == expected.contact_id
    assert clay_item["inputs"] == {
        "Full Name": expected.full_name,
        "Company Domain": expected.company_domain,
        "Company Name": expected.company_name,
        "Social Profile URL": expected.linkedin_url or expected.profile_url,
    }

    assert stub.for_provider("apollo") == []
    instantly_requests = stub.for_provider("instantly")
    assert len(instantly_requests) == 1
    assert json_body(instantly_requests[0])["email"] == _EMAIL


def test_normal_m4_completes_after_same_run_pending_resume_without_second_clay_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal M4 waits, resumes its persisted Clay run, and completes in one canary call."""
    run_id = "canary-normal-same-run-resume"
    clay = ClayRoutineScript([{"work_email": _EMAIL}])

    def apollo(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "credits_used": 1,
                "person": {"email": "shadow.owner@acmevalve.com", "linkedin_url": _PROFILE},
            },
        )

    stub = WireStub(
        {
            "exa": _exa_one,
            "clay": clay,
            "apollo": apollo,
            "instantly": _terminal_instantly("verified", expected_email=_EMAIL),
        }
    )
    _install_contract(monkeypatch, tmp_path, run_id, _accepted_company(), stub)
    sleeps: list[float] = []

    def release_on_sleep(delay: float) -> None:
        sleeps.append(delay)
        clay.release_started()

    monkeypatch.setattr(production_canary, "sleep", release_on_sleep, raising=False)

    assert _run_canary(tmp_path, run_id) == 0
    assert len(sleeps) == 1
    assert len(clay.posts) == 1
    assert len(clay.gets) == 1
    routine_run_id = clay.latest_run_id
    assert routine_run_id is not None
    assert clay.gets[0].url.path == f"/public/v0/routines/run/{routine_run_id}/results"


def test_normal_m4_permanent_pending_stops_after_three_same_run_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal M4 never starts replacement Clay work and stops after three pending reads."""
    run_id = "canary-normal-pending-ceiling"
    clay = ClayRoutineScript([{"work_email": _EMAIL}])
    stub = WireStub({"exa": _exa_one, "clay": clay})
    _install_contract(monkeypatch, tmp_path, run_id, _accepted_company(), stub)
    sleeps: list[float] = []
    monkeypatch.setattr(production_canary, "sleep", sleeps.append, raising=False)

    assert _run_canary(tmp_path, run_id) == 2
    assert len(sleeps) == 3
    assert len(clay.posts) == 1
    assert len(clay.gets) == 3
    routine_run_id = clay.latest_run_id
    assert routine_run_id is not None
    assert {request.url.path for request in clay.gets} == {
        f"/public/v0/routines/run/{routine_run_id}/results"
    }


def test_normal_m4_gives_sequential_async_stages_independent_read_ceilings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three Clay reads may transition to Instantly and still allow same-run Instantly polling."""
    run_id = "normal-sequential-pending"
    enrich_calls = 0
    coverage_calls = 0
    sleeps: list[float] = []
    clay_run_id = "routine-sequential"
    instant_email = "owner@example.com"
    usage_path = tmp_path / run_id / "contact_usage_events.jsonl"

    def write_clay_pending() -> None:
        _write_contact_status(
            tmp_path,
            run_id,
            "paused_pending",
            pause_reason="clay_pending",
            operations={
                "clay:batch": {
                    "state": "pending",
                    "routine_run_id": clay_run_id,
                    "contact_ids": [],
                }
            },
        )

    def fake_cli(argv: list[str] | None = None) -> int:
        nonlocal enrich_calls
        assert argv is not None
        if argv[0] == "run":
            return 0
        assert argv[0] == "enrich"
        enrich_calls += 1
        if enrich_calls == 1:
            write_clay_pending()
            usage_path.touch()
            return 2
        if enrich_calls in {2, 3}:
            append_jsonl(
                usage_path,
                UsageEvent(
                    provider="clay",
                    operation="work_email_routine_results",
                    metadata={"routine_run_id": clay_run_id},
                ).to_dict(),
            )
            write_clay_pending()
            return 2
        if enrich_calls == 4:
            append_jsonl(
                usage_path,
                UsageEvent(
                    provider="clay",
                    operation="work_email_routine_results",
                    metadata={"routine_run_id": clay_run_id},
                ).to_dict(),
            )
            _write_contact_status(
                tmp_path,
                run_id,
                "paused_pending",
                pause_reason="instantly:contact-1",
                operations={
                    "clay:batch": {
                        "state": "completed",
                        "routine_run_id": clay_run_id,
                        "contact_ids": [],
                    },
                    "instantly:contact-1": {
                        "state": "pending",
                        "email": instant_email,
                    },
                },
            )
            return 2
        append_jsonl(
            usage_path,
            UsageEvent(
                provider="instantly",
                operation="email_verification_get",
                metadata={"email": instant_email},
            ).to_dict(),
        )
        _write_contact_status(
            tmp_path,
            run_id,
            "completed",
            operations={
                "clay:batch": {
                    "state": "completed",
                    "routine_run_id": clay_run_id,
                    "contact_ids": [],
                },
                "instantly:contact-1": {
                    "state": "completed",
                    "email": instant_email,
                    "status": "verified",
                },
            },
        )
        return 0

    def fake_coverage(
        _data_root: Path,
        *,
        run_id: str,
    ) -> CanaryProviderCoverageSummary:
        nonlocal coverage_calls
        coverage_calls += 1
        return CanaryProviderCoverageSummary(run_id=run_id, status="completed")

    monkeypatch.setattr(production_canary, "cli_main", fake_cli)
    monkeypatch.setattr(production_canary, "run_live_provider_coverage", fake_coverage)
    monkeypatch.setattr(
        production_canary,
        "build_canary_coverage_report",
        lambda _data_root, *, run_id: SimpleNamespace(overall_outcome="success"),
    )
    monkeypatch.setattr(production_canary, "sleep", sleeps.append, raising=False)

    assert production_canary.main(
        ["--run-id", run_id, "--data-root", str(tmp_path)]
    ) == 0
    assert enrich_calls == 5
    assert coverage_calls == 1
    assert len(sleeps) == 4


@pytest.mark.parametrize("status", ["paused_budget", "paused_unknown"])
def test_main_never_redispatches_non_pending_code_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """CLI code 2 is resumable only when the durable M4 checkpoint says paused_pending."""
    run_id = f"non-resumable-{status}"
    enrich_calls = 0
    sleeps: list[float] = []

    def fake_cli(argv: list[str] | None = None) -> int:
        nonlocal enrich_calls
        assert argv is not None
        if argv[0] == "run":
            return 0
        assert argv[0] == "enrich"
        enrich_calls += 1
        _write_contact_status(tmp_path, run_id, status)
        return 2

    def unexpected_coverage(
        _data_root: Path,
        *,
        run_id: str,
    ) -> CanaryProviderCoverageSummary:
        raise AssertionError("coverage must not start after non-resumable normal M4")

    monkeypatch.setattr(production_canary, "cli_main", fake_cli)
    monkeypatch.setattr(production_canary, "run_live_provider_coverage", unexpected_coverage)
    monkeypatch.setattr(
        production_canary,
        "build_canary_coverage_report",
        lambda _data_root, *, run_id: SimpleNamespace(overall_outcome="inconclusive"),
    )
    monkeypatch.setattr(production_canary, "sleep", sleeps.append, raising=False)

    assert production_canary.main(
        ["--run-id", run_id, "--data-root", str(tmp_path)]
    ) == 2
    assert enrich_calls == 1
    assert sleeps == []


def test_main_polls_pending_coverage_to_completion_in_same_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending async coverage is retried with bounded sleeps before reporting success."""
    run_id = "same-run-polling"
    summaries = iter(
        [
            CanaryProviderCoverageSummary(run_id=run_id, status="pending"),
            CanaryProviderCoverageSummary(run_id=run_id, status="pending"),
            CanaryProviderCoverageSummary(run_id=run_id, status="completed"),
        ]
    )
    coverage_calls = 0
    sleeps: list[float] = []
    timeline: list[str] = []

    def fake_coverage(_data_root: Path, *, run_id: str) -> CanaryProviderCoverageSummary:
        nonlocal coverage_calls
        coverage_calls += 1
        timeline.append("coverage")
        return next(summaries)

    def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        timeline.append("sleep")

    monkeypatch.setattr(production_canary, "cli_main", lambda _argv=None: 0)
    monkeypatch.setattr(production_canary, "run_live_provider_coverage", fake_coverage)
    monkeypatch.setattr(
        production_canary,
        "build_canary_coverage_report",
        lambda _data_root, *, run_id: SimpleNamespace(overall_outcome="success"),
    )
    monkeypatch.setattr(production_canary, "sleep", fake_sleep, raising=False)

    assert production_canary.main(
        ["--run-id", run_id, "--data-root", str(tmp_path)]
    ) == 0
    assert coverage_calls == 3
    assert timeline == ["coverage", "sleep", "coverage", "sleep", "coverage"]
    assert all(0 < delay <= 30 for delay in sleeps)


def test_main_stops_polling_pending_coverage_after_fixed_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seven passes cover three Clay reads plus three Instantly reads, then stop."""
    run_id = "bounded-polling"
    coverage_calls = 0
    sleeps: list[float] = []
    timeline: list[str] = []

    def fake_coverage(_data_root: Path, *, run_id: str) -> CanaryProviderCoverageSummary:
        nonlocal coverage_calls
        coverage_calls += 1
        timeline.append("coverage")
        return CanaryProviderCoverageSummary(run_id=run_id, status="pending")

    def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        timeline.append("sleep")

    monkeypatch.setattr(production_canary, "cli_main", lambda _argv=None: 0)
    monkeypatch.setattr(production_canary, "run_live_provider_coverage", fake_coverage)
    monkeypatch.setattr(
        production_canary,
        "build_canary_coverage_report",
        lambda _data_root, *, run_id: SimpleNamespace(overall_outcome="inconclusive"),
    )
    monkeypatch.setattr(production_canary, "sleep", fake_sleep, raising=False)

    assert production_canary.main(
        ["--run-id", run_id, "--data-root", str(tmp_path)]
    ) == 2
    assert coverage_calls == 7
    assert timeline == ["coverage", "sleep"] * 6 + ["coverage"]
    assert all(0 < delay <= 30 for delay in sleeps)
