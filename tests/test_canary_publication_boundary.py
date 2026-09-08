"""Publication-boundary regressions for the credentialed production canary."""

from __future__ import annotations

from pathlib import Path


def test_private_canary_durability_does_not_use_public_repository_refs() -> None:
    """Every Git push in the canary remains confined to the approved output branch."""
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/generate-leads.yml").read_text(encoding="utf-8")
    canary = workflow.split("\n  canary:", 1)[1]

    assert "canary-operation-journal" not in canary
    assert "LEADS_GIT_JOURNAL_" not in canary
    assert "actions/upload-artifact" not in canary
    push_lines = [line.strip() for line in canary.splitlines() if "git push origin" in line]
    assert push_lines
    assert all("generated-leads" in line for line in push_lines)
