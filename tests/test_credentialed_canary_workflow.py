"""Static security contract for the paid credentialed production canary workflow."""

from __future__ import annotations

import re
from pathlib import Path

_WORKFLOW = ".github/workflows/generate-leads.yml"
_CANARY_ENVIRONMENT = "production-canary"
_CLAY_FUNCTION_ID_MARKER = "vars.CLAY_WORK_EMAIL_FUNCTION_ID"
_EXISTING_SECRET_MARKERS = (
    "secrets.EXA_API_KEY",
    "secrets.DEEPSEEK_API_KEY",
    "secrets.CLAY_PUBLIC_API_KEY",
    "secrets.APOLLO_API_KEY",
    "secrets.INSTANTLY_API_KEY",
)
_RENAMED_SECRET_MARKERS = (
    "secrets.CANARY_EXA_API_KEY",
    "secrets.CANARY_DEEPSEEK_API_KEY",
    "secrets.CANARY_CLAY_PUBLIC_API_KEY",
    "secrets.CANARY_APOLLO_API_KEY",
    "secrets.CANARY_INSTANTLY_API_KEY",
)
_PAID_SECRET_MARKERS = _EXISTING_SECRET_MARKERS + _RENAMED_SECRET_MARKERS
_REQUIRED_PROVIDERS = (
    "apollo",
    "clay",
    "deepseek",
    "exa_discovery",
    "exa_people",
    "exa_research",
    "instantly",
)
_JOB_IF = re.compile(r"^\s*if:\s*\$\{\{\s*(?P<expression>.+?)\s*\}\}\s*$", re.MULTILINE)


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _workflow_text() -> str:
    return (_root() / _WORKFLOW).read_text(encoding="utf-8")


def _canary_job(text: str) -> str:
    return text.split("\n  canary:", 1)[1]


def _job_if_expression(job: str) -> str:
    match = _JOB_IF.search(job)
    assert match is not None, "secret-bearing canary job must have an explicit job-level guard"
    return match.group("expression")


def _guard_allows(expression: str, *, event_name: str, ref: str) -> bool:
    """Evaluate the tiny equality/AND subset used by the canary admission guard."""
    context = {
        "github.event_name": event_name,
        "github.ref": ref,
    }
    terms = [term.strip() for term in expression.split("&&")]
    assert terms
    results: list[bool] = []
    for term in terms:
        left, separator, right = term.partition("==")
        assert separator == "==", f"unsupported canary guard term: {term}"
        key = left.strip()
        literal = right.strip()
        assert key in context, f"unsupported canary guard context: {key}"
        assert len(literal) >= 2 and literal[0] == literal[-1] and literal[0] in {"'", '"'}
        results.append(context[key] == literal[1:-1])
    return all(results)


def test_exactly_one_paid_credentialed_canary_workflow_exists() -> None:
    """Only the dedicated manual canary workflow may receive paid-provider secrets."""
    workflows = _root() / ".github" / "workflows"
    credentialed = sorted(
        path.name
        for path in workflows.glob("*.y*ml")
        if any(
            marker in path.read_text(encoding="utf-8")
            for marker in _PAID_SECRET_MARKERS
        )
    )
    assert credentialed == ["generate-leads.yml"]


def test_paid_canary_is_manual_immutable_and_ci_authorized() -> None:
    """Dispatch cannot widen spend, and exact main CI must authorize execution."""
    text = _workflow_text()
    trigger = text.split("permissions:", 1)[0]
    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "pull_request:" not in trigger
    assert "schedule:" not in trigger
    assert "inputs:" not in trigger
    assert "github.event.inputs" not in text
    assert "continue-on-error" not in text

    authorize = text.split("jobs:\n  authorize:", 1)[1].split("\n  canary:", 1)[0]
    canary = _canary_job(text)
    assert "contents: read" in authorize
    assert "actions: read" in authorize
    assert "secrets." not in authorize
    assert "ref: main" in authorize
    assert "git rev-parse HEAD" in authorize
    assert "actions/workflows/ci.yml/runs" in authorize
    assert ".head_sha == $sha" in authorize
    assert '.head_branch == "main"' in authorize
    assert '.event == "push"' in authorize
    assert '.conclusion == "success"' in authorize
    assert "needs: authorize" in canary
    assert "ref: ${{ needs.authorize.outputs.target_sha }}" in canary
    assert "python -m leads_discovery.production_canary" in canary


def test_secret_bearing_canary_job_rejects_non_main_dispatch_refs() -> None:
    """Secret release keeps credential names and exact-main environment admission."""
    canary = _canary_job(_workflow_text())
    expression = _job_if_expression(canary)

    assert _guard_allows(
        expression,
        event_name="workflow_dispatch",
        ref="refs/heads/main",
    )
    for ref in (
        "refs/heads/feature/credential-exfiltration",
        "refs/heads/main-like",
        "refs/tags/main",
    ):
        assert not _guard_allows(expression, event_name="workflow_dispatch", ref=ref)
    assert not _guard_allows(expression, event_name="push", ref="refs/heads/main")

    assert f"environment: {_CANARY_ENVIRONMENT}" in canary
    for marker in _EXISTING_SECRET_MARKERS:
        assert marker in canary
    for marker in _RENAMED_SECRET_MARKERS:
        assert marker not in canary


def test_clay_managed_function_id_is_non_secret_environment_config() -> None:
    """Clay's workspace function identifier is configuration, not a paid credential."""
    canary = _canary_job(_workflow_text())

    assert _CLAY_FUNCTION_ID_MARKER in canary
    assert "secrets.CLAY_CONTACT_ROUTINE_ID" not in canary
    assert "secrets.CANARY_CLAY_CONTACT_ROUTINE_ID" not in canary


def test_canary_private_state_never_crosses_runner_boundary() -> None:
    """The private canary phase must not configure or perform repository publication."""
    canary = _canary_job(_workflow_text())
    private_phase = canary.split("- name: Publish approved public outputs", 1)[0]

    assert "LEADS_GIT_JOURNAL_BRANCH" not in private_phase
    assert "LEADS_GIT_JOURNAL_REMOTE" not in private_phase
    assert "Prepare durable Git operation journal" not in private_phase
    assert "git push" not in private_phase
    assert "git add" not in private_phase


def test_paid_canary_gates_publication_on_decisive_private_coverage() -> None:
    """Only a decisive successful private report may reach the public-output step."""
    text = _workflow_text()
    canary = _canary_job(text)
    run_step = canary.split("- name: Run fixed one-company live canary", 1)[1].split(
        "- name: Publish approved public outputs", 1
    )[0]
    publish = canary.split("- name: Publish approved public outputs", 1)[1]

    assert "canary_coverage_report.json" in run_step
    assert ".overall_outcome" in run_step
    assert ".pipeline_outcome" in run_step
    assert '.integration_outcome == "success"' in run_step
    for provider in _REQUIRED_PROVIDERS:
        assert f'"{provider}"' in run_step
    assert '"$overall" == "success"' in run_step
    assert '"$pipeline" == "success"' in run_step
    assert '"$canary_code" -eq 0' in run_step
    assert '"$canary_code" -eq 2' in run_step
    assert "Production canary readiness: inconclusive" in run_step
    assert "Production canary readiness: failure" in run_step
    assert "exit 2" in run_step
    assert "exit 1" in run_step

    assert "if: ${{ success() }}" in publish
    assert "canary_coverage_report.json" not in publish
    assert "actions/upload-artifact" not in text
    assert 'cp -- "$run_dir/leads.csv"' in publish
    assert 'cp -- "$run_dir/contacts.jsonl"' in publish
    assert "git add -- leads.csv contacts.jsonl" in publish
    assert "git add --all" not in publish
    for private_name in (
        "checkpoint.json",
        "usage_events.jsonl",
        "canary_paid_checkpoint.json",
        "canary_paid_usage_events.jsonl",
    ):
        assert private_name not in publish