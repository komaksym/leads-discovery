"""Highest-seam regressions for canary-private state across runner loss."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_canary_provider_coverage as coverage_helpers

from leads_discovery import production_canary
from leads_discovery.contacts.models import ContactRecord
from leads_discovery.contacts.providers import (
    ClayResults,
    ClayStartResult,
    VerificationResult,
)
from leads_discovery.contacts.selection import select_contacts
from leads_discovery.pipeline import state as state_module
from leads_discovery.pipeline.canary_provider_coverage import run_provider_coverage

_JOURNAL_BRANCH = "canary-operation-journal"
_JOURNAL_KEY = "test-canary-journal-key-32-bytes-minimum"
_LEADS_HEADER = "company_id,contact_id,work_email,email_verification_status,email_source\n"


def _git(cwd: Path, *args: str) -> str:
    """Run one local Git command and return captured stdout for fixture assertions."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _configure_identity(work: Path) -> None:
    """Configure commit-tree identity used by the production Git journal."""
    _git(work, "config", "user.name", "Test Bot")
    _git(work, "config", "user.email", "test@example.invalid")


def _journal_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    nonempty_tree: bool,
) -> tuple[Path, Path]:
    """Create a durable journal remote and one ephemeral runner checkout."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    work = tmp_path / "runner-a"
    work.mkdir()
    _git(work, "init")
    _configure_identity(work)
    if nonempty_tree:
        (work / "seed").write_text("journal\n", encoding="utf-8")
        _git(work, "add", "seed")
        _git(work, "commit", "-m", "seed non-empty journal")
    else:
        _git(work, "commit", "--allow-empty", "-m", "Initialize empty journal")
    _git(work, "branch", "-M", _JOURNAL_BRANCH)
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-u", "origin", _JOURNAL_BRANCH)
    _configure_journal_env(monkeypatch, work)
    return remote, work


def _initial_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    """Create the canonical empty-tree durable journal and first runner."""
    return _journal_workspace(tmp_path, monkeypatch, nonempty_tree=False)


def _fresh_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote: Path,
    name: str,
) -> Path:
    """Clone a fresh checkout with no prior runner-local canary files."""
    work = tmp_path / name
    _git(tmp_path, "clone", "--branch", _JOURNAL_BRANCH, str(remote), str(work))
    _configure_identity(work)
    _configure_journal_env(monkeypatch, work)
    return work


def _configure_journal_env(
    monkeypatch: pytest.MonkeyPatch,
    work: Path,
) -> None:
    """Point the production journal at the fixture's non-publication branch."""
    monkeypatch.setenv("GITHUB_WORKSPACE", str(work))
    monkeypatch.setenv("LEADS_GIT_JOURNAL_BRANCH", _JOURNAL_BRANCH)
    monkeypatch.setenv("LEADS_GIT_JOURNAL_REMOTE", "origin")
    monkeypatch.setenv("LEADS_GIT_JOURNAL_KEY", _JOURNAL_KEY)


def _normal_run_dir(work: Path, run_id: str) -> Path:
    """Persist the resolved normal state needed to enter shadow composition."""
    data = work / "data"
    data.mkdir()
    run_dir = data / run_id
    coverage_helpers._write_normal_state(run_dir, coverage_helpers._company("rejected"))
    (run_dir / "leads.csv").write_text(_LEADS_HEADER, encoding="utf-8")
    return run_dir


class _RestartableClay:
    """Make Clay polling independent of process-local state from the original start."""

    def __init__(self, contact_id: str) -> None:
        self.contact_id = contact_id
        self.starts: list[list[ContactRecord]] = []
        self.result_ids: list[str] = []

    def start(self, contacts: list[ContactRecord]) -> ClayStartResult:
        self.starts.append([ContactRecord.from_dict(item.to_dict()) for item in contacts])
        return ClayStartResult(
            routine_run_id="shadow-clay-run",
            usage_event=coverage_helpers._event(
                "clay",
                "work_email_routine_start",
                metadata={"submitted_contacts": len(contacts)},
            ),
        )

    def results(self, routine_run_id: str) -> ClayResults:
        self.result_ids.append(routine_run_id)
        return ClayResults(
            status="complete",
            items=[{"id": self.contact_id, "work_email": " Alice.Owner@Acme.com "}],
            usage_event=coverage_helpers._event("clay", "work_email_routine_results"),
        )


class _PendingInstantly:
    """Create one pending verification, then resolve it only through GET."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.read: list[str] = []

    def create(self, email: str) -> VerificationResult:
        self.created.append(email)
        return VerificationResult(
            status="pending",
            credits_used=1.0,
            usage_event=coverage_helpers._event("instantly", "email_verification_create"),
        )

    def get(self, email: str) -> VerificationResult:
        self.read.append(email)
        return VerificationResult(
            status="verified",
            credits_used=0.0,
            usage_event=coverage_helpers._event("instantly", "email_verification_get"),
        )


class _ResumeOnlyInstantly(_PendingInstantly):
    """Fail if a fresh runner replaces the already-created verification."""

    def create(self, email: str) -> VerificationResult:
        raise AssertionError(f"Instantly create must not be replaced for {email}")


def test_nonempty_journal_tree_fails_closed_before_shadow_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing journal with tracked content is never accepted as operation authority."""
    _remote, work = _journal_workspace(tmp_path, monkeypatch, nonempty_tree=True)
    run_id = "nonempty-journal"
    run_dir = _normal_run_dir(work, run_id)
    exa = coverage_helpers._CoverageExa()

    with pytest.raises(RuntimeError, match="journal.*tree|tree.*empty"):
        run_provider_coverage(
            run_dir,
            run_id=run_id,
            exa=exa,
            clay=coverage_helpers._BombClay(),
            apollo=coverage_helpers._BombApollo(),
            instantly=coverage_helpers._BombInstantly(),
        )

    assert exa.companies == []


def test_crash_after_remote_pending_state_restores_same_clay_identity_without_replacement_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote durability is atomic with the pending identity before local checkpoint replacement."""
    remote, first_work = _initial_workspace(tmp_path, monkeypatch)
    run_id = "restart-crash-window"
    first_run_dir = _normal_run_dir(first_work, run_id)
    company = coverage_helpers._company("rejected")
    contact = select_contacts(company, [coverage_helpers._person_result()], limit=1)[0]
    first_clay = _RestartableClay(contact.contact_id)
    original_write_json_atomic = state_module.write_json_atomic

    def crash_before_private_checkpoint_replace(path: Path, payload: dict[str, object]) -> None:
        provider_state = payload.get("provider_state")
        operations = provider_state.get("operations") if isinstance(provider_state, dict) else None
        clay_state = operations.get("coverage:clay") if isinstance(operations, dict) else None
        if (
            path.name == "canary_paid_checkpoint.json"
            and isinstance(clay_state, dict)
            and clay_state.get("state") == "pending"
        ):
            raise RuntimeError("simulated runner loss before local checkpoint replacement")
        original_write_json_atomic(path, payload)

    with monkeypatch.context() as crash_patch:
        crash_patch.setattr(state_module, "write_json_atomic", crash_before_private_checkpoint_replace)
        with pytest.raises(RuntimeError, match="simulated runner loss"):
            run_provider_coverage(
                first_run_dir,
                run_id=run_id,
                exa=coverage_helpers._CoverageExa(),
                clay=first_clay,
                apollo=coverage_helpers._BombApollo(),
                instantly=coverage_helpers._BombInstantly(),
            )

    assert len(first_clay.starts) == 1

    second_work = _fresh_workspace(tmp_path, monkeypatch, remote, "runner-b")
    second_run_dir = _normal_run_dir(second_work, run_id)
    resumed_clay = _RestartableClay(contact.contact_id)
    instantly = coverage_helpers._CoverageInstantly()

    resumed = run_provider_coverage(
        second_run_dir,
        run_id=run_id,
        exa=coverage_helpers._BombExa(),
        clay=resumed_clay,
        apollo=coverage_helpers._BombApollo(),
        instantly=instantly,
    )

    assert resumed.status == "completed"
    assert resumed_clay.starts == []
    assert resumed_clay.result_ids == ["shadow-clay-run"]
    assert instantly.created == ["alice.owner@acme.com"]


def test_production_canary_fresh_runner_restores_before_normal_cli_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The top-level canary restores durable prerequisites before deciding to rerun normal work."""
    remote, first_work = _initial_workspace(tmp_path, monkeypatch)
    run_id = "restart-production-canary"
    first_run_dir = _normal_run_dir(first_work, run_id)
    company = coverage_helpers._company("rejected")
    contact = select_contacts(company, [coverage_helpers._person_result()], limit=1)[0]

    first = run_provider_coverage(
        first_run_dir,
        run_id=run_id,
        exa=coverage_helpers._CoverageExa(),
        clay=_RestartableClay(contact.contact_id),
        apollo=coverage_helpers._BombApollo(),
        instantly=coverage_helpers._BombInstantly(),
    )
    assert first.status == "pending"

    second_work = _fresh_workspace(tmp_path, monkeypatch, remote, "runner-b")
    data_root = second_work / "data"

    def forbid_normal_cli(_argv: object = None) -> int:
        raise AssertionError("normal run/enrich must not be re-dispatched from durable restart state")

    def fake_coverage(root: Path, *, run_id: str) -> SimpleNamespace:
        restored = root / run_id
        assert (restored / "checkpoint.json").is_file()
        assert (restored / "contact_checkpoint.json").is_file()
        assert (restored / "companies_evaluated.jsonl").is_file()
        assert (restored / "contacts.jsonl").is_file()
        assert (restored / "leads.csv").is_file()
        return SimpleNamespace(status="completed")

    def fake_report(_root: Path, *, run_id: str) -> SimpleNamespace:
        assert run_id == "restart-production-canary"
        return SimpleNamespace(overall_outcome="inconclusive")

    monkeypatch.setattr(production_canary, "cli_main", forbid_normal_cli)
    monkeypatch.setattr(production_canary, "run_live_provider_coverage", fake_coverage)
    monkeypatch.setattr(production_canary, "build_canary_coverage_report", fake_report)

    assert production_canary.main(["--run-id", run_id, "--data-root", str(data_root)]) == 2


def test_runner_loss_after_pending_shadow_clay_resumes_same_routine_without_new_paid_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh runner restores private Exa/Clay state and polls the original Clay run."""
    remote, first_work = _initial_workspace(tmp_path, monkeypatch)
    run_id = "restart-shadow-clay"
    first_run_dir = _normal_run_dir(first_work, run_id)
    company = coverage_helpers._company("rejected")
    contact = select_contacts(company, [coverage_helpers._person_result()], limit=1)[0]
    exa = coverage_helpers._CoverageExa()
    first_clay = _RestartableClay(contact.contact_id)

    first = run_provider_coverage(
        first_run_dir,
        run_id=run_id,
        exa=exa,
        clay=first_clay,
        apollo=coverage_helpers._BombApollo(),
        instantly=coverage_helpers._BombInstantly(),
    )
    assert first.status == "pending"
    assert len(exa.companies) == 1
    assert len(first_clay.starts) == 1

    journal_log = _git(
        first_work,
        "log",
        "--format=%B",
        f"refs/remotes/origin/{_JOURNAL_BRANCH}",
    )
    assert "leads-canary-state-v1" in journal_log
    assert "shadow-clay-run" not in journal_log
    assert contact.contact_id not in journal_log
    assert "alice.owner@acme.com" not in journal_log.lower()
    assert (
        _git(
            first_work,
            "ls-tree",
            "-r",
            "--name-only",
            f"refs/remotes/origin/{_JOURNAL_BRANCH}",
        )
        == ""
    )

    second_work = _fresh_workspace(tmp_path, monkeypatch, remote, "runner-b")
    second_run_dir = _normal_run_dir(second_work, run_id)
    resumed_clay = _RestartableClay(contact.contact_id)
    instantly = coverage_helpers._CoverageInstantly()

    resumed = run_provider_coverage(
        second_run_dir,
        run_id=run_id,
        exa=coverage_helpers._BombExa(),
        clay=resumed_clay,
        apollo=coverage_helpers._BombApollo(),
        instantly=instantly,
    )

    assert resumed.status == "completed"
    assert resumed_clay.starts == []
    assert resumed_clay.result_ids == ["shadow-clay-run"]
    assert instantly.created == ["alice.owner@acme.com"]


def test_runner_loss_after_pending_shadow_instantly_resumes_get_without_new_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh runner restores the private email lineage and resumes the original verification."""
    remote, first_work = _initial_workspace(tmp_path, monkeypatch)
    run_id = "restart-shadow-instantly"
    first_run_dir = _normal_run_dir(first_work, run_id)
    company = coverage_helpers._company("rejected")
    contact = select_contacts(company, [coverage_helpers._person_result()], limit=1)[0]
    clay = _RestartableClay(contact.contact_id)
    instantly = _PendingInstantly()

    first = run_provider_coverage(
        first_run_dir,
        run_id=run_id,
        exa=coverage_helpers._CoverageExa(),
        clay=clay,
        apollo=coverage_helpers._BombApollo(),
        instantly=instantly,
    )
    assert first.status == "pending"

    second = run_provider_coverage(
        first_run_dir,
        run_id=run_id,
        exa=coverage_helpers._BombExa(),
        clay=clay,
        apollo=coverage_helpers._BombApollo(),
        instantly=instantly,
    )
    assert second.status == "pending"
    assert len(clay.starts) == 1
    assert clay.result_ids == ["shadow-clay-run"]
    assert instantly.created == ["alice.owner@acme.com"]

    second_work = _fresh_workspace(tmp_path, monkeypatch, remote, "runner-b")
    second_run_dir = _normal_run_dir(second_work, run_id)
    resumed_instantly = _ResumeOnlyInstantly()

    resumed = run_provider_coverage(
        second_run_dir,
        run_id=run_id,
        exa=coverage_helpers._BombExa(),
        clay=coverage_helpers._BombClay(),
        apollo=coverage_helpers._BombApollo(),
        instantly=resumed_instantly,
    )

    assert resumed.status == "completed"
    assert resumed_instantly.read == ["alice.owner@acme.com"]


def test_workflow_rejects_existing_nonempty_journal_before_live_canary() -> None:
    """Workflow setup must validate an existing journal tree before releasing paid credentials."""
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/generate-leads.yml").read_text(encoding="utf-8")
    prepare = text.split("- name: Prepare durable Git operation journal", 1)[1].split(
        "- name: Run fixed one-company live canary", 1
    )[0]

    assert 'journal_tree="$(git rev-parse "refs/remotes/origin/canary-operation-journal^{tree}")"' in prepare
    assert '"$journal_tree" != "$empty_tree"' in prepare
    assert "journal branch tree must be empty" in prepare
