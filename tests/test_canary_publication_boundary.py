"""Publication-boundary regressions for the credentialed production canary."""

from __future__ import annotations

from pathlib import Path


def test_private_canary_durability_does_not_use_public_repository_refs() -> None:
    """Private restart/barrier authority must not cross a public Git ref before publication."""
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/generate-leads.yml").read_text(encoding="utf-8")
    canary = workflow.split("\n  canary:", 1)[1]
    private_phase = canary.split("- name: Publish approved public outputs", 1)[0]

    assert "canary-operation-journal" not in private_phase
    assert "git push origin" not in private_phase
    assert "actions/upload-artifact" not in private_phase
    assert "generated-leads" not in private_phase
