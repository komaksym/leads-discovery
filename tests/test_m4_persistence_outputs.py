"""Independent persistence, output, resume, and safety contracts for M4."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from m3_factories import (
    accepted_facts,
    build_company,
    exact_threshold_facts,
    write_run_inputs,
)
from m4_contract_fixtures import (
    WireStub,
    call_cli,
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


def _exa_one(_request: httpx.Request) -> httpx.Response:
    """Return one current owner for a one-company persistence test."""
    result = person_result(
        name="Pat Owner",
        title="President and Owner",
        company="Acme Valve",
        domain="acmevalve.com",
        profile_url=PROFILE,
    )
    return httpx.Response(
        200,
        json={"results": [result], "costDollars": {"total": 0.001}},
    )


def _clay_miss(_request: httpx.Request) -> httpx.Response:
    """Complete Clay with no work email."""
    return httpx.Response(
        200,
        json={
            "id": "clay-miss",
            "run_id": "clay-miss",
            "routine_run_id": "clay-miss",
            "status": "completed",
            "credits_used": 1,
            "results": [],
            "data": {"results": [], "credits_used": 1},
        },
    )


def _apollo_miss(_request: httpx.Request) -> httpx.Response:
    """Complete one paid Apollo attempt without finding an email."""
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "credits_used": 1,
            "person": {"email": None},
        },
    )


def _first_exact_key(value: Any, target: str) -> bool:
    """Return whether a nested JSON-like structure contains one exact key."""
    if isinstance(value, dict):
        if target in value:
            return True
        return any(_first_exact_key(nested, target) for nested in value.values())
    if isinstance(value, list):
        return any(_first_exact_key(nested, target) for nested in value)
    return False


def _replace_exact_key(value: Any, target: str, replacement: Any) -> int:
    """Replace every occurrence of one exact nested key and return the mutation count."""
    count = 0
    if isinstance(value, dict):
        for key in list(value):
            if key == target:
                value[key] = replacement
                count += 1
            else:
                count += _replace_exact_key(value[key], target, replacement)
    elif isinstance(value, list):
        for nested in value:
            count += _replace_exact_key(nested, target, replacement)
    return count


def test_m4_artifacts_are_separate_and_completed_rerun_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider misses retain contacts and completed resume replays neither rows nor spend."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "partial",
        [
            build_company(
                facts=accepted_facts(),
                name="Acme Valve",
                domain="acmevalve.com",
            )
        ],
    )
    m123_before = {
        path.name: path.read_bytes()
        for path in run_dir.iterdir()
        if path.is_file()
    }
    stub = WireStub(
        {"exa": _exa_one, "clay": _clay_miss, "apollo": _apollo_miss}
    )
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_cli(
        ["enrich", "--run-id", "partial", "--data-root", str(tmp_path)]
    ) == 0

    names = {path.name for path in run_dir.iterdir() if path.is_file()}
    assert M4_ARTIFACTS <= names
    contacts = read_jsonl(run_dir / "contacts.jsonl")
    assert len(contacts) == 1
    assert "pat owner" in row_text(contacts[0]).casefold()
    assert EMAIL not in row_text(contacts[0])
    for name, payload in m123_before.items():
        assert (run_dir / name).read_bytes() == payload

    durable_before = {
        name: (run_dir / name).read_bytes()
        for name in M4_ARTIFACTS
    }
    request_count = len(stub.requests)
    assert call_cli(
        ["enrich", "--run-id", "partial", "--data-root", str(tmp_path)]
    ) == 0
    assert len(stub.requests) == request_count
    for name, payload in durable_before.items():
        assert (run_dir / name).read_bytes() == payload
    assert len(read_jsonl(run_dir / "contacts.jsonl")) == 1


def test_provider_budget_exhaustion_keeps_partial_contact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A paid-provider budget stop preserves discovery and blocks downstream spend."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "budget-stop",
        [
            build_company(
                facts=accepted_facts(),
                name="Acme Valve",
                domain="acmevalve.com",
            )
        ],
    )

    def clay_budget(_request: httpx.Request) -> httpx.Response:
        """Simulate an explicit Clay budget exhaustion response."""
        return httpx.Response(
            402,
            json={"error": "budget exhausted", "credits_used": 0},
        )

    stub = WireStub({"exa": _exa_one, "clay": clay_budget})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_cli(
        ["enrich", "--run-id", "budget-stop", "--data-root", str(tmp_path)]
    ) != 0
    contacts = read_jsonl(run_dir / "contacts.jsonl")
    assert len(contacts) == 1
    assert "pat owner" in row_text(contacts[0]).casefold()
    assert stub.for_provider("apollo") == []
    assert stub.for_provider("instantly") == []


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

    def clay(request: httpx.Request) -> httpx.Response:
        """Return the work email corresponding to the submitted contact."""
        body = request.content.decode("utf-8").casefold()
        results: list[dict[str, str]] = []
        for needle, (name, _domain, email) in identities.items():
            if needle in body:
                results.append(
                    {
                        "name": name,
                        "profile_url": (
                            f"https://www.linkedin.com/in/{needle}-owner"
                        ),
                        "work_email": email,
                        "email": email,
                    }
                )
        return httpx.Response(
            200,
            json={
                "id": "clay-csv",
                "run_id": "clay-csv",
                "routine_run_id": "clay-csv",
                "status": "completed",
                "credits_used": 1,
                "results": results,
                "data": {"results": results, "credits_used": 1},
            },
        )

    def instantly(request: httpx.Request) -> httpx.Response:
        """Verify the existing work email without creating outreach state."""
        body = json_body(request) if request.method == "POST" else {}
        email = str(body.get("email") or request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            200,
            json={"email": email, "status": "verified", "credits_used": 1},
        )

    stub = WireStub({"exa": exa, "clay": clay, "instantly": instantly})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_cli(
        ["enrich", "--run-id", "csv-output", "--data-root", str(tmp_path)]
    ) == 0

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

    assert call_cli(
        ["enrich", "--run-id", "csv-output", "--data-root", str(tmp_path)]
    ) == 0
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

    assert call_cli(
        ["enrich", "--run-id", run_id, "--data-root", str(tmp_path)]
    ) == 1
    assert stub.requests == []


def test_symlinked_m4_output_is_rejected_without_spend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing M4 output symlink fails closed before provider work."""
    run_dir = prepare_evaluated_run(
        tmp_path,
        "symlink",
        [
            build_company(
                facts=accepted_facts(),
                name="Acme Valve",
                domain="acmevalve.com",
            )
        ],
    )
    target = tmp_path / "outside-contacts.jsonl"
    target.write_text("sentinel\n", encoding="utf-8")
    os.symlink(target, run_dir / "contacts.jsonl")
    stub = WireStub({"exa": _exa_one})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    assert call_cli(
        ["enrich", "--run-id", "symlink", "--data-root", str(tmp_path)]
    ) != 0
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
    forbidden = {"CLAY_API_KEY", "APOLLO_API_KEY", "INSTANTLY_API_KEY"}

    def guarded_get(
        env: Any,
        key: str,
        default: str | None = None,
    ) -> str | None:
        """Reject one M4 credential read from an existing command."""
        if key in forbidden:
            raise AssertionError(f"M4 credential read forbidden: {key}")
        return original_get(env, key, default)

    def guarded_getitem(env: Any, key: str) -> str:
        """Reject one indexed M4 credential read from an existing command."""
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
    assert call_cli(
        ["score", "--run-id", "local-m4-free", "--data-root", str(tmp_path)]
    ) == 0
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "company_id,manual_label\ncmp_contract,A\n",
        encoding="utf-8",
    )
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


@pytest.mark.parametrize(
    "bad_usage",
    [-1, float("nan"), float("inf"), float("-inf"), "one"],
)
def test_corrupted_persisted_m4_credits_used_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_usage: Any,
) -> None:
    """Persisted credits_used is validated even after all contact work completed."""
    run_id = "corrupt-usage"
    run_dir = prepare_evaluated_run(
        tmp_path,
        run_id,
        [
            build_company(
                facts=accepted_facts(),
                name="Acme Valve",
                domain="acmevalve.com",
            )
        ],
    )

    def clay(_request: httpx.Request) -> httpx.Response:
        """Return one work email with explicit one-credit usage."""
        result = {"profile_url": PROFILE, "work_email": EMAIL, "email": EMAIL}
        return httpx.Response(
            200,
            json={
                "id": "clay-corrupt",
                "run_id": "clay-corrupt",
                "routine_run_id": "clay-corrupt",
                "status": "completed",
                "credits_used": 1,
                "results": [result],
                "data": {"results": [result], "credits_used": 1},
            },
        )

    def instantly(_request: httpx.Request) -> httpx.Response:
        """Return one terminal verification with explicit one-credit usage."""
        return httpx.Response(
            200,
            json={"email": EMAIL, "status": "verified", "credits_used": 1},
        )

    stub = WireStub({"exa": _exa_one, "clay": clay, "instantly": instantly})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)
    assert call_cli(
        ["enrich", "--run-id", run_id, "--data-root", str(tmp_path)]
    ) == 0

    usage_path = run_dir / "contact_usage.json"
    usage = json.loads(usage_path.read_text(encoding="utf-8"))
    assert _first_exact_key(usage, "credits_used")
    assert _replace_exact_key(usage, "credits_used", bad_usage) > 0
    usage_path.write_text(
        json.dumps(usage, sort_keys=True, allow_nan=True) + "\n",
        encoding="utf-8",
    )
    contacts_before = (run_dir / "contacts.jsonl").read_bytes()
    leads_before = (run_dir / "leads.csv").read_bytes()
    request_count = len(stub.requests)

    assert call_cli(
        ["enrich", "--run-id", run_id, "--data-root", str(tmp_path)]
    ) != 0
    assert len(stub.requests) == request_count
    assert (run_dir / "contacts.jsonl").read_bytes() == contacts_before
    assert (run_dir / "leads.csv").read_bytes() == leads_before
