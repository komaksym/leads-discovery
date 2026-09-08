"""Static security contract for the paid credentialed production canary workflow."""

# ruff: noqa: F401

from __future__ import annotations

from credentialed_canary_workflow_core import (
    _CANARY_STATE_KEY_MARKER,
    _REQUIRED_PROVIDERS,
    _canary_job,
    _workflow_text,
    test_clay_managed_function_id_is_non_secret_environment_config,
    test_exactly_one_paid_credentialed_canary_workflow_exists,
    test_paid_canary_is_manual_immutable_and_ci_authorized,
    test_secret_bearing_canary_job_rejects_non_main_dispatch_refs,
)


def test_canary_private_durability_is_wired_without_public_ref() -> None:
    """The live canary receives private journal authority without an operational Git branch."""
    canary = _canary_job(_workflow_text())

    assert "canary-operation-journal" not in canary
    assert "LEADS_GIT_JOURNAL_" not in canary
    assert "actions/upload-artifact" not in canary
    assert _CANARY_STATE_KEY_MARKER in canary
    assert "${{ github.token }}" in canary


def test_paid_canary_gates_publication_on_decisive_private_coverage() -> None:
    """Only decisive successful private coverage may cross the publication boundary."""
    text = _workflow_text()
    canary = _canary_job(text)
    private_phase, publish = canary.split("- name: Publish approved public outputs", 1)
    run_step = private_phase.split("- name: Run fixed one-company live canary", 1)[1]

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

    assert "generated-leads" not in private_phase
    assert "canary-operation-journal" not in private_phase
    assert "git push origin" not in private_phase
    assert "actions/upload-artifact" not in private_phase
    assert "if: ${{ success() }}" in publish
    assert "canary_coverage_report.json" not in publish
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
