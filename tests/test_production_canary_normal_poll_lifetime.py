"""Regression contracts for normal-M4 async polling across canary invocations."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from m4_contract_fixtures import ClayRoutineScript, WireStub, json_body
from test_production_canary_offline_contract import (
    _EMAIL,
    _accepted_company,
    _exa_one,
    _install_contract,
    _run_canary,
)

from leads_discovery import production_canary
from leads_discovery.models import RunCheckpoint
from leads_discovery.pipeline.state import write_checkpoint


def _write_contact_pause(
    data_root: Path,
    run_id: str,
    status: str,
    *,
    pause_reason: str,
    operations: dict[str, object] | None = None,
) -> None:
    """Persist one synthetic contact pause for orchestration admission tests."""
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


def test_pending_clay_read_ceiling_survives_canary_process_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted Clay run gets at most three status GET attempts for its lifetime."""
    run_id = "normal-clay-lifetime-ceiling"
    clay = ClayRoutineScript([{"work_email": _EMAIL}])
    stub = WireStub({"exa": _exa_one, "clay": clay})
    _install_contract(monkeypatch, tmp_path, run_id, _accepted_company(), stub)
    sleeps: list[float] = []
    monkeypatch.setattr(production_canary, "sleep", sleeps.append, raising=False)

    assert _run_canary(tmp_path, run_id) == 2
    assert len(clay.posts) == 1
    assert len(clay.gets) == 3
    assert len(sleeps) == 3

    assert _run_canary(tmp_path, run_id) == 2
    assert len(clay.posts) == 1
    assert len(clay.gets) == 3
    assert len(sleeps) == 3


def test_pending_instantly_read_ceiling_survives_canary_process_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted Instantly verification gets three GET attempts without another POST."""
    run_id = "normal-instantly-lifetime-ceiling"
    clay = ClayRoutineScript([{"work_email": _EMAIL}])
    instantly_posts: list[httpx.Request] = []
    instantly_gets: list[httpx.Request] = []

    def instantly(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/api/v2/email-verification"
            instantly_posts.append(request)
            assert json_body(request)["email"] == _EMAIL
            return httpx.Response(
                202,
                json={
                    "email": _EMAIL,
                    "verification_status": "pending",
                    "credits_used": 1,
                },
            )
        assert request.method == "GET"
        assert request.url.path == f"/api/v2/email-verification/{_EMAIL}"
        instantly_gets.append(request)
        return httpx.Response(
            202,
            json={
                "email": _EMAIL,
                "verification_status": "pending",
                "credits_used": 0,
            },
        )

    stub = WireStub({"exa": _exa_one, "clay": clay, "instantly": instantly})
    _install_contract(monkeypatch, tmp_path, run_id, _accepted_company(), stub)
    sleeps = 0

    def release_clay_then_continue(_delay: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            clay.release_started()

    monkeypatch.setattr(
        production_canary,
        "sleep",
        release_clay_then_continue,
        raising=False,
    )

    assert _run_canary(tmp_path, run_id) == 2
    assert len(clay.posts) == 1
    assert len(instantly_posts) == 1
    assert len(instantly_gets) == 3
    assert sleeps == 4

    assert _run_canary(tmp_path, run_id) == 2
    assert len(clay.posts) == 1
    assert len(instantly_posts) == 1
    assert len(instantly_gets) == 3
    assert sleeps == 4


def test_fresh_process_waits_before_next_persisted_status_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process restart cannot turn the next durable Clay read into a zero-delay poll."""
    run_id = "normal-clay-restart-delay"
    clay = ClayRoutineScript([{"work_email": _EMAIL}])
    stub = WireStub({"exa": _exa_one, "clay": clay})
    _install_contract(monkeypatch, tmp_path, run_id, _accepted_company(), stub)
    first_sleeps = 0

    def interrupt_after_first_read(_delay: float) -> None:
        nonlocal first_sleeps
        first_sleeps += 1
        if first_sleeps == 2:
            raise RuntimeError("simulated process restart")

    monkeypatch.setattr(
        production_canary,
        "sleep",
        interrupt_after_first_read,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="simulated process restart"):
        _run_canary(tmp_path, run_id)
    assert len(clay.posts) == 1
    assert len(clay.gets) == 1

    restart_sleeps: list[float] = []

    def record_restart_sleep(delay: float) -> None:
        if not restart_sleeps:
            assert len(clay.gets) == 1
        restart_sleeps.append(delay)

    monkeypatch.setattr(
        production_canary,
        "sleep",
        record_restart_sleep,
        raising=False,
    )

    assert _run_canary(tmp_path, run_id) == 2
    assert restart_sleeps[0] == pytest.approx(10.0)
    assert len(clay.posts) == 1
    assert len(clay.gets) == 3


@pytest.mark.parametrize("status", ["paused_budget", "paused_unknown"])
def test_non_pending_pause_is_not_redispatched_by_fresh_canary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """A durable non-resumable code-2 checkpoint is never sent through M4 again."""
    run_id = f"fresh-{status}"
    enrich_calls = 0

    def fake_cli(argv: list[str] | None = None) -> int:
        nonlocal enrich_calls
        assert argv is not None
        if argv[0] == "run":
            return 0
        assert argv[0] == "enrich"
        enrich_calls += 1
        _write_contact_pause(
            tmp_path,
            run_id,
            status,
            pause_reason="synthetic_non_resumable",
        )
        return 2

    monkeypatch.setattr(production_canary, "cli_main", fake_cli)
    monkeypatch.setattr(
        production_canary,
        "build_canary_coverage_report",
        lambda _data_root, *, run_id: SimpleNamespace(overall_outcome="inconclusive"),
    )
    monkeypatch.setattr(
        production_canary,
        "run_live_provider_coverage",
        lambda *_args, **_kwargs: pytest.fail("coverage must not run"),
    )

    assert production_canary.main(
        ["--run-id", run_id, "--data-root", str(tmp_path)]
    ) == 2
    assert enrich_calls == 1

    assert production_canary.main(
        ["--run-id", run_id, "--data-root", str(tmp_path)]
    ) == 2
    assert enrich_calls == 1


def test_malformed_pending_identity_fails_closed_before_redispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paused-pending reason without its durable provider identity is not resumable."""
    run_id = "malformed-pending-identity"
    enrich_calls = 0
    sleeps: list[float] = []

    def fake_cli(argv: list[str] | None = None) -> int:
        nonlocal enrich_calls
        assert argv is not None
        if argv[0] == "run":
            return 0
        assert argv[0] == "enrich"
        enrich_calls += 1
        if enrich_calls > 1:
            raise AssertionError("malformed pending work must not be redispatched")
        _write_contact_pause(
            tmp_path,
            run_id,
            "paused_pending",
            pause_reason="clay_pending",
        )
        return 2

    monkeypatch.setattr(production_canary, "cli_main", fake_cli)
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


def test_status_read_admission_survives_crash_before_usage_bookkeeping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatched third Clay GET cannot disappear from the lifetime ceiling on restart."""
    import leads_discovery.pipeline.contact_enrichment as contact_enrichment

    run_id = "normal-clay-crash-after-dispatch"
    clay = ClayRoutineScript([{"work_email": _EMAIL}])
    stub = WireStub({"exa": _exa_one, "clay": clay})
    _install_contract(monkeypatch, tmp_path, run_id, _accepted_company(), stub)
    monkeypatch.setattr(production_canary, "sleep", lambda _delay: None, raising=False)

    original_record_event = contact_enrichment._record_event

    def crash_before_third_status_event(lifecycle: object, event: object) -> None:
        if (
            getattr(event, "provider", None) == "clay"
            and getattr(event, "operation", None) == "work_email_routine_results"
            and len(clay.gets) == 3
        ):
            raise RuntimeError("simulated crash after status GET")
        original_record_event(lifecycle, event)  # type: ignore[arg-type]

    monkeypatch.setattr(contact_enrichment, "_record_event", crash_before_third_status_event)

    with pytest.raises(RuntimeError, match="simulated crash after status GET"):
        _run_canary(tmp_path, run_id)
    assert len(clay.posts) == 1
    assert len(clay.gets) == 3

    monkeypatch.setattr(contact_enrichment, "_record_event", original_record_event)

    assert _run_canary(tmp_path, run_id) == 2
    assert len(clay.posts) == 1
    assert len(clay.gets) == 3
