"""Frozen-contract tests for local M3 evaluation persistence and path safety."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from m3_factories import (
    accepted_facts,
    build_company,
    low_score_facts,
    read_jsonl,
    write_run_inputs,
)

from leads_discovery.pipeline.evaluation import EvaluationConfig, EvaluationSummary, evaluate_run


def _evaluate(root: Path, run_id: str, max_evaluated: int = 20) -> EvaluationSummary:
    """Run the frozen local evaluation API."""
    return evaluate_run(
        EvaluationConfig(run_id=run_id, data_root=root, max_evaluated=max_evaluated)
    )


def _symlink_or_skip(link: Path, target: Path) -> None:
    """Create a symlink or skip on platforms that cannot."""
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")


def test_latest_completed_snapshots_are_selected_sorted_and_capped(tmp_path: Path) -> None:
    """Append-only M2 history is collapsed to latest completed snapshots and at most 20 IDs."""
    old = build_company(
        facts=low_score_facts(),
        company_id="cmp_00",
        extraction_completed=False,
    )
    new = build_company(facts=accepted_facts(), company_id="cmp_00")
    others = [
        build_company(facts=accepted_facts(), company_id=f"cmp_{i:02d}")
        for i in range(25, 0, -1)
    ]
    run_dir = write_run_inputs(tmp_path, "latest", [old, *others, new])
    summary = _evaluate(tmp_path, "latest")
    rows = read_jsonl(run_dir / "companies_evaluated.jsonl")

    assert summary.evaluated_count == 20
    assert [row["company_id"] for row in rows] == [f"cmp_{i:02d}" for i in range(20)]
    assert rows[0]["final_decision"] == "accepted"


@pytest.mark.parametrize("cap", [0, 21, True])
def test_invalid_cap_fails_before_any_m3_write(tmp_path: Path, cap: object) -> None:
    """The local cap is strict integer 1..20 and validated before mutation."""
    run_dir = write_run_inputs(tmp_path, "bad-cap", [build_company(facts=accepted_facts())])
    with pytest.raises((TypeError, ValueError)):
        _evaluate(tmp_path, "bad-cap", cap)  # type: ignore[arg-type]
    assert not (run_dir / "companies_evaluated.jsonl").exists()


@pytest.mark.parametrize("run_id", ["", "../escape", "a/b", "/absolute", "x" * 65])
def test_run_id_traversal_and_invalid_grammar_are_rejected(
    tmp_path: Path,
    run_id: str,
) -> None:
    """A run ID can never escape or add path components beneath data_root."""
    with pytest.raises(ValueError):
        _evaluate(tmp_path, run_id)
    assert not (tmp_path.parent / "escape" / "companies_evaluated.jsonl").exists()


def test_empty_partial_run_is_valid_but_empty_completed_run_is_error(tmp_path: Path) -> None:
    """No completed extraction is valid while paused/running, but corrupts completed M2 state."""
    paused = write_run_inputs(
        tmp_path,
        "paused-empty",
        [],
        checkpoint_status="paused_budget",
        pause_reason="deepseek_budget_exhausted",
    )
    summary = _evaluate(tmp_path, "paused-empty")
    assert summary.evaluated_count == 0
    assert (paused / "companies_evaluated.jsonl").read_text(encoding="utf-8") == ""

    completed = write_run_inputs(tmp_path, "completed-empty", [], checkpoint_status="completed")
    with pytest.raises(ValueError):
        _evaluate(tmp_path, "completed-empty")
    assert not (completed / "companies_evaluated.jsonl").exists()


def test_atomic_recomputation_replaces_m3_and_never_mutates_m2(tmp_path: Path) -> None:
    """Rerun replaces stale derived snapshots while every M2 artifact remains byte-identical."""
    first = build_company(facts=accepted_facts(), company_id="cmp_atomic")
    run_dir = write_run_inputs(tmp_path, "atomic", [first])
    for name, content in {
        "companies_raw.jsonl": '{"raw":1}\n',
        "companies_deduped.jsonl": '{"dedup":1}\n',
        "research_raw.jsonl": '{"research":1}\n',
    }.items():
        (run_dir / name).write_text(content, encoding="utf-8")

    _evaluate(tmp_path, "atomic")
    assert read_jsonl(run_dir / "companies_evaluated.jsonl")[0]["final_decision"] == "accepted"

    second = build_company(facts=low_score_facts(), company_id="cmp_atomic")
    with (run_dir / "companies_extracted.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(second.to_dict(), sort_keys=True) + "\n")

    m2_names = [
        "companies_raw.jsonl",
        "companies_deduped.jsonl",
        "research_raw.jsonl",
        "companies_extracted.jsonl",
        "usage_events.jsonl",
        "usage.json",
    ]
    before = {name: (run_dir / name).read_bytes() for name in m2_names}
    _evaluate(tmp_path, "atomic")
    after = {name: (run_dir / name).read_bytes() for name in m2_names}

    rows = read_jsonl(run_dir / "companies_evaluated.jsonl")
    assert len(rows) == 1
    assert rows[0]["final_decision"] == "uncertain"
    assert before == after


def test_one_malformed_fact_does_not_erase_successful_peer(tmp_path: Path) -> None:
    """Fact-level invalidity yields a reviewable company instead of aborting the run."""
    good = build_company(facts=accepted_facts(), company_id="cmp_good")
    bad = build_company(facts={"employee_count": (50, .90)}, company_id="cmp_bad")
    bad.features["employee_count"] = "fifty"
    run_dir = write_run_inputs(tmp_path, "bad-fact", [good, bad])

    summary = _evaluate(tmp_path, "bad-fact")
    rows = {row["company_id"]: row for row in read_jsonl(run_dir / "companies_evaluated.jsonl")}
    assert summary.evaluated_count == 2
    assert rows["cmp_good"]["final_decision"] == "accepted"
    assert "invalid_fact:employee_count" in rows["cmp_bad"]["review_reasons"]


@pytest.mark.parametrize(
    ("literal", "run_id"),
    [(b"NaN", "nan"), (b"Infinity", "inf"), (b"-Infinity", "neg-inf")],
)
def test_raw_nonfinite_persisted_fact_is_rejected_before_m3_output_mutation(
    tmp_path: Path,
    literal: bytes,
    run_id: str,
) -> None:
    """Non-finite persisted fact numbers fail closed before derived artifacts are written."""
    company = build_company(facts={"employee_count": (50, .90)})
    run_dir = write_run_inputs(tmp_path, run_id, [company])
    path = run_dir / "companies_extracted.jsonl"
    raw = path.read_bytes()
    assert b'"employee_count": 50' in raw
    path.write_bytes(raw.replace(b'"employee_count": 50', b'"employee_count": ' + literal, 1))

    with pytest.raises(ValueError):
        _evaluate(tmp_path, run_id)

    for name in (
        "companies_evaluated.jsonl",
        "companies_ranked.csv",
        "companies_rejected.csv",
        "companies_uncertain.csv",
        "calibration_template.csv",
        "run_summary.json",
    ):
        assert not (run_dir / name).exists()


def test_torn_final_append_is_ignored_but_nonfinal_corruption_fails_atomically(
    tmp_path: Path,
) -> None:
    """Only a torn final M2 append is tolerable; prior complete M3 output survives corruption."""
    company = build_company(facts=accepted_facts())
    run_dir = write_run_inputs(tmp_path, "torn", [company])
    _evaluate(tmp_path, "torn")

    extracted = run_dir / "companies_extracted.jsonl"
    with extracted.open("ab") as handle:
        handle.write(b'{"company_id":"torn"')
    _evaluate(tmp_path, "torn")
    assert read_jsonl(run_dir / "companies_evaluated.jsonl")[0]["company_id"] == "cmp_contract"
    evaluated_before = (run_dir / "companies_evaluated.jsonl").read_bytes()
    ranked_before = (run_dir / "companies_ranked.csv").read_bytes()

    extracted.write_bytes(
        json.dumps(company.to_dict(), sort_keys=True).encode()
        + b"\n{not-json}\n"
        + json.dumps(company.to_dict(), sort_keys=True).encode()
        + b"\n"
    )
    with pytest.raises((ValueError, json.JSONDecodeError)):
        _evaluate(tmp_path, "torn")
    assert (run_dir / "companies_evaluated.jsonl").read_bytes() == evaluated_before
    assert (run_dir / "companies_ranked.csv").read_bytes() == ranked_before


def test_nonobject_and_malformed_canonical_rows_fail_run_level(tmp_path: Path) -> None:
    """Run-level JSONL/model corruption is never downgraded to an uncertain lead."""
    for run_id, payload in [
        ("nonobject", "[]\n"),
        ("malformed", '{"company_id":"only-id"}\n'),
    ]:
        run_dir = write_run_inputs(tmp_path, run_id, [build_company(facts=accepted_facts())])
        (run_dir / "companies_extracted.jsonl").write_text(payload, encoding="utf-8")
        with pytest.raises((TypeError, ValueError)):
            _evaluate(tmp_path, run_id)
        assert not (run_dir / "companies_evaluated.jsonl").exists()


def test_symlinked_run_directory_and_output_are_rejected(tmp_path: Path) -> None:
    """Evaluation never follows a run-directory or output symlink."""
    outside = tmp_path / "outside"
    outside.mkdir()
    run_link = tmp_path / "linked-run"
    _symlink_or_skip(run_link, outside)
    with pytest.raises(ValueError):
        _evaluate(tmp_path, "linked-run")

    run_dir = write_run_inputs(tmp_path, "output-link", [build_company(facts=accepted_facts())])
    target = tmp_path / "outside.txt"
    target.write_text("outside\n", encoding="utf-8")
    _symlink_or_skip(run_dir / "companies_evaluated.jsonl", target)
    with pytest.raises(ValueError):
        _evaluate(tmp_path, "output-link")
    assert target.read_text(encoding="utf-8") == "outside\n"
    assert not (run_dir / "companies_ranked.csv").exists()


def test_summary_paths_are_run_local_and_counts_are_exact(tmp_path: Path) -> None:
    """Public summary reports exact counts and detached artifact paths within the run."""
    companies = [
        build_company(facts=accepted_facts(), company_id="cmp_a"),
        build_company(facts=low_score_facts(), company_id="cmp_u"),
        build_company(facts=accepted_facts(), company_id="cmp_r", status="inactive"),
    ]
    run_dir = write_run_inputs(tmp_path, "summary", companies)
    summary = _evaluate(tmp_path, "summary")
    assert (summary.evaluated_count, summary.accepted_count) == (3, 1)
    assert (summary.rejected_count, summary.uncertain_count) == (1, 1)
    assert summary.policy_version == "m3-v1"
    assert all(path.parent == run_dir for path in summary.artifact_paths)
