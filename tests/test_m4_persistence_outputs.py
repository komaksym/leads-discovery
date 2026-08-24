"""Independent persistence, output, resume, and safety contracts for M4."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from m3_factories import accepted_facts, build_company, exact_threshold_facts, write_run_inputs
from m4_contract_fixtures import (
    ClayRoutineScript,
    WireStub,
    call_cli,
    call_enrich_live,
    install_mock_http,
    json_body,
    person_result,
    prepare_evaluated_run,
    read_csv,
    read_jsonl,
    row_text,
    set_m4_credentials,
)

M4_ARTIFACTS = {
    "contacts.jsonl",
    "leads.csv",
    "contact_usage_events.jsonl",
    "contact_usage.json",
    "contact_checkpoint.json",
}
EMAIL = "pat.owner@acmevalve.com"
PROFILE = "https://www.linkedin.com/in/pat-owner"
PROVIDERS = ("exa", "clay", "apollo", "instantly")


def _exa_one(_request: httpx.Request) -> httpx.Response:
    """Return one current owner for a one-company persistence test."""
    result = person_result(
        name="Pat Owner",
        title="President and Owner",
        company="Acme Valve",
        domain="acmevalve.com",
        profile_url=PROFILE,
    )
    return httpx.Response(200, json={"results": [result], "costDollars": {"total": 0.001}})


def _apollo_miss(_request: httpx.Request) -> httpx.Response:
    """Complete one paid Apollo attempt without finding an email."""
    return httpx.Response(
        200,
        json={"status": "completed", "credits_used": 1, "person": {"email": None}},
    )


def _replace_first_operation_state(value: Any, replacement: str) -> bool:
    """Replace one persisted provider operation state without assuming its identifier."""
    if isinstance(value, dict):
        operations = value.get("operations")
        if isinstance(operations, dict):
            for operation in operations.values():
                if isinstance(operation, dict) and "state" in operation:
                    operation["state"] = replacement
                    return True
        return any(_replace_first_operation_state(nested, replacement) for nested in value.values())
    if isinstance(value, list):
        return any(_replace_first_operation_state(nested, replacement) for nested in value)
    return False


def _checkpoint_operations(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Return operations from the public persisted M4 provider-state container."""
    provider_state = checkpoint.get("provider_state")
    assert isinstance(provider_state, dict)
    operations = provider_state.get("operations")
    assert isinstance(operations, dict)
    return operations


def _operation_key_for_provider(operations: dict[str, Any], provider: str) -> str:
    """Locate one provider operation from observable persisted checkpoint text."""
    matches: list[str] = []
    for key, operation in operations.items():
        text = f"{key}\n{json.dumps(operation, sort_keys=True, default=str)}".casefold()
        if provider.casefold() in text:
            matches.append(key)
    assert len(matches) == 1, f"expected one {provider} operation, found {matches}"
    return matches[0]


def _resume_clay_until_complete(
    *,
    data_root: Path,
    run_dir: Path,
    run_id: str,
    clay: ClayRoutineScript,
    max_invocations: int = 10,
) -> None:
    """Drive each started Clay routine through durable state and a later GET."""
    code = call_enrich_live(data_root, run_id)
    assert code != 0
    for _ in range(max_invocations):
        latest = clay.latest_run_id
        assert latest is not None
        checkpoint = run_dir / "contact_checkpoint.json"
        assert checkpoint.exists()
        assert latest in checkpoint.read_text(encoding="utf-8")
        clay.release_started()
        code = call_enrich_live(data_root, run_id)
        if code == 0:
            assert len(clay.posts) == len(clay.gets)
            return
    raise AssertionError("Clay lifecycle did not complete within bounded resume attempts")


def test_m4_artifacts_are_separate_and_completed_rerun_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider misses retain contacts and completed resume replays neither rows nor spend."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "partial",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    m123_before = {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}
    clay = ClayRoutineScript([])
    stub = WireStub({"exa": _exa_one, "clay": clay, "apollo": _apollo_miss})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    _resume_clay_until_complete(
        data_root=tmp_path,
        run_dir=run_dir,
        run_id="partial",
        clay=clay,
    )

    names = {path.name for path in run_dir.iterdir() if path.is_file()}
    assert M4_ARTIFACTS <= names
    contacts = read_jsonl(run_dir / "contacts.jsonl")
    assert len(contacts) == 1
    assert "pat owner" in row_text(contacts[0]).casefold()
    assert EMAIL not in row_text(contacts[0])
    for name, payload in m123_before.items():
        assert (run_dir / name).read_bytes() == payload

    durable_before = {name: (run_dir / name).read_bytes() for name in M4_ARTIFACTS}
    request_count = len(stub.requests)
    assert call_enrich_live(tmp_path, "partial") == 0
    assert len(stub.requests) == request_count
    for name, payload in durable_before.items():
        assert (run_dir / name).read_bytes() == payload
    assert len(read_jsonl(run_dir / "contacts.jsonl")) == 1


def test_provider_budget_exhaustion_keeps_partial_contact_and_stops_downstream_spend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Clay budget stop preserves discovery and cannot consume Apollo or Instantly credits."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "budget-stop",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )

    def clay_budget(_request: httpx.Request) -> httpx.Response:
        """Simulate an explicit Clay budget exhaustion response."""
        return httpx.Response(402, json={"error": "budget exhausted", "credits_used": 0})

    stub = WireStub({"exa": _exa_one, "clay": clay_budget})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_enrich_live(tmp_path, "budget-stop") != 0
    contacts = read_jsonl(run_dir / "contacts.jsonl")
    assert len(contacts) == 1
    assert "pat owner" in row_text(contacts[0]).casefold()
    assert stub.for_provider("apollo") == []
    assert stub.for_provider("instantly") == []


def test_provider_usage_is_separated_for_independent_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M4 persists provider-specific accounting instead of pooling Exa, Clay, and Apollo usage."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "provider-ledgers",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    clay = ClayRoutineScript([])
    stub = WireStub({"exa": _exa_one, "clay": clay, "apollo": _apollo_miss})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)
    _resume_clay_until_complete(
        data_root=tmp_path,
        run_dir=run_dir,
        run_id="provider-ledgers",
        clay=clay,
    )

    usage_text = (run_dir / "contact_usage.json").read_text(encoding="utf-8").casefold()
    assert "exa" in usage_text
    assert "clay" in usage_text
    assert "apollo" in usage_text
    assert (run_dir / "contact_usage_events.jsonl").exists()


def test_leads_csv_is_deterministic_formula_safe_and_score_ordered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verified leads sort by score then rank/name and preserve safe Unicode CSV text."""
    malicious = "=SUM(1,2)"
    unicode_name = "Žaneta \"Zee\", O'Neil"
    low_name = "Aaron Threshold"
    run_dir = prepare_evaluated_run(
        tmp_path,
        "csv-output",
        [
            build_company(
                facts=accepted_facts(),
                company_id="cmp_formula",
                name="Formula Valve",
                domain="formula.example",
            ),
            build_company(
                facts=accepted_facts(),
                company_id="cmp_unicode",
                name="Unicode Valve",
                domain="unicode.example",
            ),
            build_company(
                facts=exact_threshold_facts(),
                company_id="cmp_threshold",
                name="Threshold Valve",
                domain="threshold.example",
            ),
        ],
    )
    identities = {
        "formula": (malicious, "formula.example", "formula.owner@formula.example"),
        "unicode": (unicode_name, "unicode.example", "unicode.owner@unicode.example"),
        "threshold": (low_name, "threshold.example", "threshold.owner@threshold.example"),
    }

    def exa(request: httpx.Request) -> httpx.Response:
        """Return one owner selected from the company-scoped People Search query."""
        query = str(json_body(request).get("query", "")).casefold()
        for needle, (name, domain, _email) in identities.items():
            if needle not in query:
                continue
            result = person_result(
                name=name,
                title="Owner",
                company=f"{needle.title()} Valve",
                domain=domain,
                profile_url=f"https://www.linkedin.com/in/{needle}-owner",
            )
            return httpx.Response(
                200,
                json={"results": [result], "costDollars": {"total": 0.001}},
            )
        raise AssertionError(f"unknown company query: {query}")

    def clay_rows(request: httpx.Request, _run_id: str) -> list[dict[str, Any]]:
        """Return the work email corresponding to the submitted contact."""
        body = request.content.decode("utf-8").casefold()
        rows: list[dict[str, Any]] = []
        for needle, (name, _domain, email) in identities.items():
            if needle in body:
                rows.append(
                    {
                        "name": name,
                        "profile_url": f"https://www.linkedin.com/in/{needle}-owner",
                        "work_email": email,
                        "email": email,
                    }
                )
        return rows

    def instantly(request: httpx.Request) -> httpx.Response:
        """Verify the existing work email without creating outreach state."""
        body = json_body(request) if request.method == "POST" else {}
        email = str(body.get("email") or request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            200,
            json={
                "email": email,
                "status": "completed",
                "verification_status": "verified",
                "credits_used": 1,
            },
        )

    clay = ClayRoutineScript(clay_rows)
    stub = WireStub({"exa": exa, "clay": clay, "instantly": instantly})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)
    _resume_clay_until_complete(
        data_root=tmp_path,
        run_dir=run_dir,
        run_id="csv-output",
        clay=clay,
    )

    first_bytes = (run_dir / "leads.csv").read_bytes()
    rows = read_csv(run_dir / "leads.csv")
    assert len(rows) == 3
    row_values = [list(row.values()) for row in rows]
    assert any("'" + malicious in values for values in row_values)
    assert any(unicode_name in values for values in row_values)
    assert any(low_name in values for values in row_values)
    flattened = ["\u241f".join(values) for values in row_values]
    formula_index = next(i for i, text in enumerate(flattened) if malicious in text)
    unicode_index = next(i for i, text in enumerate(flattened) if unicode_name in text)
    low_index = next(i for i, text in enumerate(flattened) if low_name in text)
    assert formula_index < unicode_index < low_index
    raw = first_bytes.decode("utf-8")
    assert '""Zee""' in raw
    assert malicious in raw

    request_count = len(stub.requests)
    assert call_enrich_live(tmp_path, "csv-output") == 0
    assert len(stub.requests) == request_count
    assert (run_dir / "leads.csv").read_bytes() == first_bytes


@pytest.mark.parametrize(
    "run_id",
    ["../escape", "..", "/absolute", "nested/run", "\\windows", "a" * 65],
)
def test_enrich_rejects_traversal_run_ids_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
) -> None:
    """The M4 CLI validates its run directory before any provider work."""
    stub = WireStub({})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_enrich_live(tmp_path, run_id) == 1
    assert stub.requests == []


def test_symlinked_m4_output_is_rejected_without_spend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing M4 output symlink fails closed before provider work."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "symlink",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    target = tmp_path / "outside-contacts.jsonl"
    target.write_text("sentinel\n", encoding="utf-8")
    os.symlink(target, run_dir / "contacts.jsonl")
    stub = WireStub({"exa": _exa_one})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_enrich_live(tmp_path, "symlink") != 0
    assert target.read_text(encoding="utf-8") == "sentinel\n"
    assert stub.requests == []


def test_run_score_and_calibrate_remain_m4_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing M1-M3 local/dry commands never read M4 credentials or use its providers."""
    env_type = type(os.environ)
    original_get = env_type.get
    original_getitem = env_type.__getitem__
    forbidden = {
        "CLAY_PUBLIC_API_KEY",
        "CLAY_CONTACT_ROUTINE_ID",
        "APOLLO_API_KEY",
        "INSTANTLY_API_KEY",
    }

    def guarded_get(env: Any, key: str, default: str | None = None) -> str | None:
        """Reject one M4 credential/config read from an existing command."""
        if key in forbidden:
            raise AssertionError(f"M4 credential read forbidden: {key}")
        return original_get(env, key, default)

    def guarded_getitem(env: Any, key: str) -> str:
        """Reject one indexed M4 credential/config read from an existing command."""
        if key in forbidden:
            raise AssertionError(f"M4 credential read forbidden: {key}")
        return original_getitem(env, key)

    monkeypatch.setattr(env_type, "get", guarded_get)
    monkeypatch.setattr(env_type, "__getitem__", guarded_getitem)
    stub = WireStub({})
    install_mock_http(monkeypatch, stub)

    assert call_cli(
        [
            "run",
            "--run-id",
            "dry-m4-free",
            "--data-root",
            str(tmp_path),
            "--deepseek-budget-usd",
            "1",
        ]
    ) == 0
    run_dir = write_run_inputs(
        tmp_path,
        "local-m4-free",
        [build_company(facts=accepted_facts())],
    )
    assert call_cli(["score", "--run-id", "local-m4-free", "--data-root", str(tmp_path)]) == 0
    labels = tmp_path / "labels.csv"
    labels.write_text("company_id,manual_label\ncmp_contract,A\n", encoding="utf-8")
    assert call_cli(
        [
            "calibrate",
            "--run-id",
            "local-m4-free",
            "--data-root",
            str(tmp_path),
            "--labels",
            str(labels),
        ]
    ) == 0
    assert stub.requests == []
    assert M4_ARTIFACTS.isdisjoint({path.name for path in run_dir.iterdir()})


def test_unknown_checkpoint_operation_state_fails_closed_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed persisted operation state cannot authorize replay or provider spend."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "bad-operation-state",
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    clay = ClayRoutineScript([])
    stub = WireStub({"exa": _exa_one, "clay": clay})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_enrich_live(tmp_path, "bad-operation-state") != 0
    assert len(clay.posts) == 1
    checkpoint_path = run_dir / "contact_checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert _replace_first_operation_state(checkpoint, "mystery_state")
    checkpoint_path.write_text(
        json.dumps(checkpoint, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    request_count = len(stub.requests)

    assert call_enrich_live(tmp_path, "bad-operation-state") != 0
    assert len(stub.requests) == request_count


@pytest.mark.parametrize("later_provider", ["clay", "apollo", "instantly"])
@pytest.mark.parametrize("evidence", ["missing", "torn"])
def test_paused_unknown_without_complete_operation_evidence_blocks_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    later_provider: str,
    evidence: str,
) -> None:
    """Later-provider unknown state cannot replay when earlier Exa evidence is incomplete."""
    run_id = f"paused-unknown-{later_provider}-{evidence}"
    run_dir = prepare_evaluated_run(
        tmp_path,
        run_id,
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )

    def clay_unknown(request: httpx.Request) -> httpx.Response:
        """Make the Clay paid POST outcome unknowable."""
        raise httpx.ReadTimeout("unknown Clay outcome", request=request)

    def apollo_unknown(request: httpx.Request) -> httpx.Response:
        """Make the Apollo paid POST outcome unknowable."""
        raise httpx.ReadTimeout("unknown Apollo outcome", request=request)

    def instantly_unknown(request: httpx.Request) -> httpx.Response:
        """Make the Instantly verification POST outcome unknowable."""
        raise httpx.ReadTimeout("unknown Instantly outcome", request=request)

    clay: ClayRoutineScript | None = None
    if later_provider == "clay":
        stub = WireStub({"exa": _exa_one, "clay": clay_unknown})
    elif later_provider == "apollo":
        clay = ClayRoutineScript([])
        stub = WireStub({"exa": _exa_one, "clay": clay, "apollo": apollo_unknown})
    else:
        clay = ClayRoutineScript(
            [{"profile_url": PROFILE, "work_email": EMAIL, "email": EMAIL}]
        )
        stub = WireStub({"exa": _exa_one, "clay": clay, "instantly": instantly_unknown})

    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_enrich_live(tmp_path, run_id) != 0
    if clay is not None:
        routine_run_id = clay.latest_run_id
        assert routine_run_id is not None
        checkpoint_path = run_dir / "contact_checkpoint.json"
        assert routine_run_id in checkpoint_path.read_text(encoding="utf-8")
        clay.release_started()
        assert call_enrich_live(tmp_path, run_id) != 0

    checkpoint_path = run_dir / "contact_checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert isinstance(checkpoint, dict)
    assert checkpoint.get("status") == "paused_unknown"
    operations = _checkpoint_operations(checkpoint)
    exa_key = _operation_key_for_provider(operations, "exa")
    later_key = _operation_key_for_provider(operations, later_provider)
    assert later_key != exa_key
    later_before = json.dumps(operations[later_key], sort_keys=True, default=str)

    if evidence == "missing":
        del operations[exa_key]
    else:
        operations[exa_key] = {}

    assert checkpoint.get("status") == "paused_unknown"
    assert json.dumps(operations[later_key], sort_keys=True, default=str) == later_before
    checkpoint_path.write_text(
        json.dumps(checkpoint, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    before = {provider: len(stub.for_provider(provider)) for provider in PROVIDERS}

    assert call_enrich_live(tmp_path, run_id) != 0

    after = {provider: len(stub.for_provider(provider)) for provider in PROVIDERS}
    assert after == before
    for provider in PROVIDERS:
        assert len(stub.for_provider(provider)) - before[provider] == 0


def test_corrupted_derived_usage_summary_is_rebuilt_from_authoritative_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corrupt derived quota summary regenerates from events without replaying providers."""
    run_id = "corrupt-derived-usage"
    run_dir = prepare_evaluated_run(
        tmp_path,
        run_id,
        [build_company(facts=accepted_facts(), name="Acme Valve", domain="acmevalve.com")],
    )
    clay = ClayRoutineScript(
        [{"profile_url": PROFILE, "work_email": EMAIL, "email": EMAIL}]
    )

    def instantly(_request: httpx.Request) -> httpx.Response:
        """Return one terminal verification with explicit one-credit usage."""
        return httpx.Response(
            200,
            json={
                "email": EMAIL,
                "status": "completed",
                "verification_status": "verified",
                "credits_used": 1,
            },
        )

    stub = WireStub({"exa": _exa_one, "clay": clay, "instantly": instantly})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)
    _resume_clay_until_complete(
        data_root=tmp_path,
        run_dir=run_dir,
        run_id=run_id,
        clay=clay,
    )

    usage_path = run_dir / "contact_usage.json"
    events_path = run_dir / "contact_usage_events.jsonl"
    events_before = events_path.read_bytes()
    contacts_before = (run_dir / "contacts.jsonl").read_bytes()
    leads_before = (run_dir / "leads.csv").read_bytes()
    request_count = len(stub.requests)
    corrupt_summary = b'{"derived_quota":{"clay":"corrupt"}}\n'
    usage_path.write_bytes(corrupt_summary)

    assert call_enrich_live(tmp_path, run_id) == 0
    assert len(stub.requests) == request_count
    assert events_path.read_bytes() == events_before
    assert usage_path.read_bytes() != corrupt_summary
    assert isinstance(json.loads(usage_path.read_text(encoding="utf-8")), dict)
    assert (run_dir / "contacts.jsonl").read_bytes() == contacts_before
    assert (run_dir / "leads.csv").read_bytes() == leads_before
