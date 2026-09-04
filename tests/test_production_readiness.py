"""Focused production-readiness tests for paid recovery and bounded execution."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from leads_discovery import production_canary
from leads_discovery.contacts.providers import (
    ApolloResult,
    ClayResults,
    ClayStartResult,
    ExaPeopleResult,
    VerificationResult,
)
from leads_discovery.discovery.base import DiscoveryProviderError, safe_transport_call
from leads_discovery.models import (
    CompanyRecord,
    EvidenceBundle,
    EvidenceItem,
    ExtractedFact,
    ExtractionResult,
    RunCheckpoint,
    UsageEvent,
)
from leads_discovery.pipeline.contact_enrichment import (
    ContactEnrichmentConfig,
    run_contact_enrichment,
)
from leads_discovery.pipeline.git_journal import sync_checkpoint_barrier
from leads_discovery.pipeline.state import (
    append_jsonl,
    iter_jsonl,
    write_jsonl_atomic,
    write_text_atomic,
)
from leads_discovery.research.extract import (
    FACT_KEYS,
    DeepSeekExtractor,
    DeepSeekPriceSchedule,
    apply_extraction,
)

_EVIDENCE_ID = "ev_000000000000000000000001"


def _company() -> CompanyRecord:
    """Build one canonical company for readiness tests."""
    return CompanyRecord(
        company_id="cmp_acme",
        name="Acme Valve",
        normalized_name="acme valve",
        domain="acme.com",
        normalized_domain="acme.com",
        country="US",
    )


def _bundle(excerpt: str = "Acme distributes industrial valves and fittings.") -> EvidenceBundle:
    """Build one bounded evidence bundle."""
    return EvidenceBundle(
        company_id="cmp_acme",
        items=[
            EvidenceItem(
                evidence_id=_EVIDENCE_ID,
                url="https://acme.com/about",
                title="About Acme",
                excerpt=excerpt,
                provider="exa",
            )
        ],
        raw_records=[],
        usage_events=[],
    )


def _facts() -> dict[str, dict[str, Any]]:
    """Build the exact DeepSeek fact schema with one supported positive fact."""
    facts: dict[str, dict[str, Any]] = {
        key: {"value": None, "confidence": 0, "evidence_ids": []}
        for key in FACT_KEYS
    }
    facts["pvf_relevant"] = {
        "value": True,
        "confidence": 0.9,
        "evidence_ids": [_EVIDENCE_ID],
    }
    return facts


def _deepseek_response(content: str) -> dict[str, Any]:
    """Build one valid DeepSeek response envelope around supplied model content."""
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_cache_hit_tokens": 1,
            "prompt_cache_miss_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 3,
        },
    }


def test_ambiguous_paid_operation_cannot_redispatch_after_remote_restart_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote in-flight barrier blocks the same paid operation after local loss."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True
    )
    work.mkdir()
    subprocess.run(["git", "init"], cwd=work, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.name", "Test Bot"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    )
    (work / "seed").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed"], cwd=work, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=work, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "branch", "-M", "generated-leads"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "generated-leads"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    )
    monkeypatch.setenv("GITHUB_WORKSPACE", str(work))
    monkeypatch.setenv("LEADS_GIT_JOURNAL_BRANCH", "generated-leads")
    monkeypatch.setenv("LEADS_GIT_JOURNAL_REMOTE", "origin")
    checkpoint = RunCheckpoint(
        run_id="restart",
        provider_state={
            "operations": {
                "discovery:one": {
                    "provider": "exa",
                    "operation": "company_search",
                    "state": "in_flight",
                }
            }
        },
    )
    sync_checkpoint_barrier(checkpoint, None)
    with pytest.raises(RuntimeError, match="durable non-retryable"):
        sync_checkpoint_barrier(checkpoint, None)


def test_deepseek_malformed_2xx_is_terminal_without_replay() -> None:
    """Malformed paid model output must stop before a later response can be requested."""
    contents = ["", "{", json.dumps({"facts": _facts()})]
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        content = contents[calls]
        calls += 1
        return httpx.Response(200, json=_deepseek_response(content))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        extractor = DeepSeekExtractor(
            api_key="test",
            client=client,
            model="deepseek-v4-flash",
            prices=DeepSeekPriceSchedule(0.0, 0.0, 0.0),
        )
        with pytest.raises(DiscoveryProviderError) as captured:
            extractor.extract(_company(), _bundle())

    assert calls == 1
    assert captured.value.kind == "invalid_response"
    assert captured.value.retryable is False
    assert captured.value.usage_event.request_count == 1

def test_deepseek_schema_invalid_2xx_is_terminal() -> None:
    """Schema-invalid paid model output is terminal after exactly one received response."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_deepseek_response('{"facts": {}}'))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        extractor = DeepSeekExtractor(
            api_key="test",
            client=client,
            model="deepseek-v4-flash",
            prices=DeepSeekPriceSchedule(0.0, 0.0, 0.0),
        )
        with pytest.raises(DiscoveryProviderError) as captured:
            extractor.extract(_company(), _bundle())

    assert calls == 1
    assert captured.value.kind == "invalid_response"
    assert captured.value.retryable is False
    assert captured.value.usage_event.request_count == 1

def test_unsupported_hard_negative_becomes_unknown() -> None:
    """A cited ID without explicit negative support cannot reject PVF relevance."""
    facts = {key: ExtractedFact(None, 0.0, []) for key in FACT_KEYS}
    facts["pvf_relevant"] = ExtractedFact(False, 0.99, [_EVIDENCE_ID])
    result = ExtractionResult(
        company_id="cmp_acme",
        model="deepseek-v4-flash",
        facts=facts,
        usage_event=UsageEvent(provider="deepseek", operation="structured_extraction"),
    )
    updated = apply_extraction(_company(), _bundle(), result)
    assert updated.features["pvf_relevant"] is None
    assert updated.feature_confidence["pvf_relevant"] == {
        "confidence": 0.0,
        "evidence_ids": [],
    }


def test_oversized_provider_response_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declared oversized provider responses fail before JSON parsing or persistence."""
    monkeypatch.setenv("LEADS_MAX_HTTP_RESPONSE_BYTES", "10")
    with pytest.raises(DiscoveryProviderError):
        safe_transport_call(
            lambda: httpx.Response(
                200, content=b"x" * 11, headers={"content-length": "11"}
            ),
            provider="exa",
            request_id="one",
            operation="search",
            request_count=1,
        )


def test_replay_is_incremental_and_oversized_record_fails_at_that_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A good first replay row is yielded before an oversized later row is rejected."""
    monkeypatch.setenv("LEADS_MAX_RECORD_BYTES", "32")
    monkeypatch.setenv("LEADS_MAX_FILE_BYTES", "1024")
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"ok":1}\n' + b'{"x":"' + b"a" * 100 + b'"}\n')
    rows = iter_jsonl(path)
    assert next(rows) == {"ok": 1}
    with pytest.raises(ValueError, match="record exceeds"):
        next(rows)


def test_oversized_replay_file_is_rejected_before_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replay file over the configured file ceiling fails closed before rows are used."""
    monkeypatch.setenv("LEADS_MAX_RECORD_BYTES", "64")
    monkeypatch.setenv("LEADS_MAX_FILE_BYTES", "8")
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"ok":1}\n')
    with pytest.raises(ValueError, match="artifact exceeds"):
        list(iter_jsonl(path))


def test_replay_record_count_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay stops when the configured record-count ceiling is exceeded."""
    monkeypatch.setenv("LEADS_MAX_RECORD_BYTES", "64")
    monkeypatch.setenv("LEADS_MAX_FILE_BYTES", "128")
    monkeypatch.setenv("LEADS_MAX_RECORDS", "1")
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"a":1}\n{"b":2}\n')
    rows = iter_jsonl(path)
    assert next(rows) == {"a": 1}
    with pytest.raises(ValueError, match="LEADS_MAX_RECORDS"):
        next(rows)


def test_total_run_disk_limit_stops_before_second_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Aggregate persisted bytes are checked before another artifact is committed."""
    monkeypatch.setenv("LEADS_MAX_FILE_BYTES", "100")
    monkeypatch.setenv("LEADS_MAX_RUN_BYTES", "40")
    write_text_atomic(tmp_path / "one.txt", "a" * 30)
    with pytest.raises(ValueError, match="run would exceed"):
        write_text_atomic(tmp_path / "two.txt", "b" * 20)
    assert not (tmp_path / "two.txt").exists()


def test_m4_rejects_symlinked_data_root(tmp_path: Path) -> None:
    """The M4 writable root itself cannot be a symlink."""
    real = tmp_path / "real-data"
    real.mkdir()
    alias = tmp_path / "alias-data"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="data_root must not be a symlink"):
        run_contact_enrichment(
            ContactEnrichmentConfig(
                run_id="root-link",
                data_root=alias,
                exa_people_budget_usd=0.02,
                execute_live=True,
            ),
            exa=_BombExa(),
            clay=_BombClay(),
            apollo=_BombApollo(),
            instantly=_BombInstantly(),
        )


def test_atomic_write_rejects_symlink_target(tmp_path: Path) -> None:
    """An output symlink cannot redirect an atomic write outside the intended root."""
    outside = tmp_path / "outside.txt"
    outside.write_text("sentinel", encoding="utf-8")
    root = tmp_path / "run"
    root.mkdir()
    target = root / "leads.csv"
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        write_text_atomic(target, "changed")
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_jsonl_snapshot_honors_record_and_run_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomic JSONL snapshots enforce record and aggregate disk limits while streaming."""
    monkeypatch.setenv("LEADS_MAX_RECORD_BYTES", "64")
    monkeypatch.setenv("LEADS_MAX_FILE_BYTES", "128")
    monkeypatch.setenv("LEADS_MAX_RUN_BYTES", "128")
    with pytest.raises(ValueError, match="record exceeds"):
        write_jsonl_atomic(tmp_path / "rows.jsonl", ({"x": "a" * 100} for _ in range(1)))


class _BombExa:
    """Fail if M4 dispatches an Exa People call after its pre-call budget gate."""

    def search(self, _company: CompanyRecord) -> ExaPeopleResult:
        """Reject any dispatch."""
        raise AssertionError("Exa People budget gate must stop before dispatch")


class _BombClay:
    """Unused Clay fake for a pre-Exa budget stop."""

    def start(self, _contacts: list[Any]) -> ClayStartResult:
        """Reject unexpected Clay use."""
        raise AssertionError("Clay should not run")

    def results(self, _routine_run_id: str) -> ClayResults:
        """Reject unexpected Clay polling."""
        raise AssertionError("Clay should not run")


class _BombApollo:
    """Unused Apollo fake for a pre-Exa budget stop."""

    def enrich(self, _contact: Any) -> ApolloResult:
        """Reject unexpected Apollo use."""
        raise AssertionError("Apollo should not run")


class _BombInstantly:
    """Unused Instantly fake for a pre-Exa budget stop."""

    def create(self, _email: str) -> VerificationResult:
        """Reject unexpected Instantly use."""
        raise AssertionError("Instantly should not run")

    def get(self, _email: str) -> VerificationResult:
        """Reject unexpected Instantly polling."""
        raise AssertionError("Instantly should not run")


def test_m4_exa_people_budget_reserves_worst_case_before_dispatch(tmp_path: Path) -> None:
    """M4 must stop before a fixed People Search could cross its USD ceiling."""
    run_dir = tmp_path / "m4-budget"
    run_dir.mkdir()
    company = _company()
    company.final_decision = "accepted"
    company.stage_status["decision"] = "completed"
    write_jsonl_atomic(run_dir / "companies_evaluated.jsonl", [company.to_dict()])
    append_jsonl(
        run_dir / "contact_usage_events.jsonl",
        UsageEvent(
            provider="exa",
            operation="people_search",
            estimated_cost_usd=0.99,
        ).to_dict(),
    )
    summary = run_contact_enrichment(
        ContactEnrichmentConfig(
            run_id="m4-budget",
            data_root=tmp_path,
            exa_people_budget_usd=1.0,
            execute_live=True,
        ),
        exa=_BombExa(),
        clay=_BombClay(),
        apollo=_BombApollo(),
        instantly=_BombInstantly(),
    )
    assert summary.status == "paused_budget"


def test_canary_limits_are_not_cli_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The canary exposes only run identity/data root and always injects fixed tiny limits."""
    calls: list[list[str]] = []

    def fake_cli(argv: Sequence[str] | None = None) -> int:
        """Capture the immutable commands emitted by the canary wrapper."""
        assert argv is not None
        calls.append(list(argv))
        return 0

    def fake_coverage(_data_root: Path, *, run_id: str) -> SimpleNamespace:
        """Keep this wrapper contract focused on immutable CLI limits."""
        assert run_id == "canary-one"
        return SimpleNamespace(status="completed")

    monkeypatch.setattr(production_canary, "cli_main", fake_cli)
    monkeypatch.setattr(production_canary, "run_live_provider_coverage", fake_coverage)
    assert production_canary.main(["--run-id", "canary-one"]) == 0
    assert len(calls) == 2
    run, enrich = calls
    assert run[run.index("--max-candidates") + 1] == "1"
    assert run[run.index("--max-evaluated") + 1] == "1"
    assert run[run.index("--exa-budget-usd") + 1] == "0.15"
    assert run[run.index("--deepseek-budget-usd") + 1] == "0.01"
    assert enrich[enrich.index("--max-contacts-per-company") + 1] == "1"
    assert enrich[enrich.index("--max-paid-contacts-per-company") + 1] == "1"
    assert enrich[enrich.index("--exa-people-budget-usd") + 1] == "0.02"
    with pytest.raises(SystemExit):
        production_canary.main(["--run-id", "canary-one", "--max-candidates", "2"])


def test_paid_workflow_is_manual_only_and_publishes_only_approved_outputs() -> None:
    """Static production workflow controls cannot be widened by dispatch inputs."""
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/generate-leads.yml").read_text(encoding="utf-8")
    trigger = text.split("permissions:", 1)[0]
    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "pull_request:" not in trigger
    assert "schedule:" not in trigger
    assert "inputs:" not in trigger
    assert "runs-on: ubuntu-latest" in text
    assert "actions/upload-artifact" not in text
    assert "git rm -rf --ignore-unmatch ." in text
    assert 'cp -- "$run_dir/leads.csv"' in text
    assert 'cp -- "$run_dir/contacts.jsonl"' in text
    assert "git add -- leads.csv contacts.jsonl" in text
    assert "git add --all" not in text
    assert "contact_checkpoint.json" not in text
    assert "contact_usage_events.jsonl" not in text

    authorize = text.split("jobs:\n  authorize:", 1)[1].split("\n  canary:", 1)[0]
    canary = text.split("\n  canary:", 1)[1]
    assert "contents: read" in authorize
    assert "actions: read" in authorize
    assert "secrets." not in authorize
    assert "id: target" in authorize
    assert "ref: main" in authorize
    assert 'git rev-parse HEAD' in authorize
    assert 'echo "sha=$target_sha" >> "$GITHUB_OUTPUT"' in authorize
    assert "actions/workflows/ci.yml/runs" in authorize
    assert '.head_sha == $sha' in authorize
    assert '.head_branch == "main"' in authorize
    assert '.event == "push"' in authorize
    assert '.conclusion == "success"' in authorize
    assert "needs: authorize" in canary
    assert "ref: ${{ needs.authorize.outputs.target_sha }}" in canary