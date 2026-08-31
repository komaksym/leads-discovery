"""Red-team checks for safe public-repository handling of generated M4 contact data."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from m3_factories import accepted_facts, build_company
from m4_contract_fixtures import (
    WireStub,
    call_cli,
    install_mock_http,
    person_result,
    prepare_evaluated_run,
    set_m4_credentials,
)

_M4_ARTIFACTS: Final[frozenset[str]] = frozenset(
    {
        "contacts.jsonl",
        "leads.csv",
        "contact_usage_events.jsonl",
        "contact_usage.json",
        "contact_checkpoint.json",
    }
)
_PII_NAME: Final = "Public Leak Sentinel Owner"
_PII_EMAIL: Final = "public-leak-sentinel@example.invalid"
_PII_PROFILE: Final = "https://www.linkedin.com/in/public-leak-sentinel"
_RAW_PROVIDER_SENTINEL: Final = "raw-provider-payload-must-stay-private"


def _summary(capsys: pytest.CaptureFixture[str]) -> tuple[dict[str, Any], str]:
    """Parse the single CLI JSON line and return all captured process text."""
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload, captured.out + captured.err


def _exa_success(request: httpx.Request) -> httpx.Response:
    """Return one current person plus private-looking provider-only sentinel fields."""
    assert request.method == "POST"
    person = person_result(
        name=_PII_NAME,
        title="President and Owner",
        company="Public Safety Valve",
        domain="public-safety.example",
        profile_url=_PII_PROFILE,
    )
    person["email"] = _PII_EMAIL
    person["raw_provider_payload"] = _RAW_PROVIDER_SENTINEL
    return httpx.Response(
        200,
        json={
            "results": [person],
            "costDollars": {"total": 0.001},
            "provider_debug": _RAW_PROVIDER_SENTINEL,
        },
    )


def _exa_failure(request: httpx.Request) -> httpx.Response:
    """Return a provider error body containing values that must never reach CLI logs."""
    assert request.method == "POST"
    return httpx.Response(
        500,
        json={
            "error": _RAW_PROVIDER_SENTINEL,
            "name": _PII_NAME,
            "email": _PII_EMAIL,
            "profile_url": _PII_PROFILE,
        },
    )


def _assert_no_private_values(text: str) -> None:
    """Assert that contact and raw-provider sentinels are absent from process output."""
    for value in (_PII_NAME, _PII_EMAIL, _PII_PROFILE, _RAW_PROVIDER_SENTINEL):
        assert value not in text


def test_public_workflows_have_no_generated_data_publish_or_release_sink() -> None:
    """Public CI must not upload, cache, or release generated M4 contact artifacts."""
    root = Path(__file__).resolve().parents[1]
    workflows = root / ".github" / "workflows"
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(workflows.glob("*.yml"))
        if path.name != "generate-leads.yml"
    )
    lowered = text.casefold()

    for artifact in _M4_ARTIFACTS:
        assert artifact.casefold() not in lowered
    for forbidden in (
        "actions/upload-artifact@",
        "actions/cache@",
        "actions/upload-release-asset@",
        "softprops/action-gh-release@",
        "ncipollo/release-action@",
        "gh release",
        "data/",
    ):
        assert forbidden not in lowered

    assert "cache: pip" in lowered
    assert "contents: read" in lowered


def test_generated_m4_artifacts_are_ignored_and_not_repository_tracked() -> None:
    """A fresh public checkout must not contain tracked generated lead/contact output."""
    root = Path(__file__).resolve().parents[1]
    ignored = {
        line.strip()
        for line in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "data/" in ignored

    result = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    tracked = result.stdout.splitlines()
    assert not any(path == "data" or path.startswith("data/") for path in tracked)
    assert not any(Path(path).name in _M4_ARTIFACTS for path in tracked)


def test_successful_cli_output_does_not_log_contact_or_raw_provider_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Normal live CLI output is a count/status summary, never contact rows or raw payloads."""
    prepare_evaluated_run(
        tmp_path,
        "public-safe-success",
        [
            build_company(
                facts=accepted_facts(),
                name="Public Safety Valve",
                domain="public-safety.example",
            )
        ],
    )
    stub = WireStub({"exa": _exa_success})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    code = call_cli(
        [
            "enrich",
            "--run-id",
            "public-safe-success",
            "--data-root",
            str(tmp_path),
            "--max-paid-contacts-per-company",
            "0",
            "--exa-people-budget-usd",
            "1.0",
            "--execute-live",
        ]
    )
    payload, emitted = _summary(capsys)

    assert code == 0
    assert payload["status"] == "completed"
    assert payload["contact_count"] == 1
    assert set(payload["artifacts"]) == _M4_ARTIFACTS
    _assert_no_private_values(emitted)


def test_provider_failure_body_is_sanitized_from_cli_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Provider error bodies cannot leak contact-like or raw payload values to Actions logs."""
    prepare_evaluated_run(
        tmp_path,
        "public-safe-error",
        [
            build_company(
                facts=accepted_facts(),
                name="Public Safety Valve",
                domain="public-safety.example",
            )
        ],
    )
    stub = WireStub({"exa": _exa_failure})
    install_mock_http(monkeypatch, stub)
    set_m4_credentials(monkeypatch)

    code = call_cli(
        [
            "enrich",
            "--run-id",
            "public-safe-error",
            "--data-root",
            str(tmp_path),
            "--max-paid-contacts-per-company",
            "0",
            "--exa-people-budget-usd",
            "1.0",
            "--execute-live",
        ]
    )
    payload, emitted = _summary(capsys)

    assert code != 0
    assert payload["status"] in {"paused_unknown", "failed"}
    _assert_no_private_values(emitted)
