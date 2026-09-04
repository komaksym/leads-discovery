"""Static security contract for the paid credentialed production canary workflow."""

from pathlib import Path

_WORKFLOW = ".github/workflows/generate-leads.yml"
_PAID_SECRET_MARKERS = (
    "secrets.EXA_API_KEY",
    "secrets.DEEPSEEK_API_KEY",
    "secrets.CLAY_PUBLIC_API_KEY",
    "secrets.CLAY_CONTACT_ROUTINE_ID",
    "secrets.APOLLO_API_KEY",
    "secrets.INSTANTLY_API_KEY",
)
_REQUIRED_PROVIDERS = (
    "apollo",
    "clay",
    "deepseek",
    "exa_discovery",
    "exa_people",
    "exa_research",
    "instantly",
)


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _workflow_text() -> str:
    return (_root() / _WORKFLOW).read_text(encoding="utf-8")


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
    """Dispatch cannot select a ref or widen spend, and exact main CI must authorize execution."""
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
    canary = text.split("\n  canary:", 1)[1]
    assert "contents: read" in authorize
    assert "actions: read" in authorize
    assert "secrets." not in authorize
    assert "ref: main" in authorize
    assert 'git rev-parse HEAD' in authorize
    assert "actions/workflows/ci.yml/runs" in authorize
    assert '.head_sha == $sha' in authorize
    assert '.head_branch == "main"' in authorize
    assert '.event == "push"' in authorize
    assert '.conclusion == "success"' in authorize
    assert "needs: authorize" in canary
    assert "ref: ${{ needs.authorize.outputs.target_sha }}" in canary
    assert "python -m leads_discovery.production_canary" in canary


def test_paid_canary_gates_publication_on_decisive_private_coverage() -> None:
    """Only a decisive successful private report may reach the public-output step."""
    text = _workflow_text()
    canary = text.split("\n  canary:", 1)[1]
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
