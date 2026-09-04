"""Fixed production canary entry point for one explicitly authorized GitHub-hosted run."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from leads_discovery.cli import main as cli_main
from leads_discovery.pipeline.canary_outcomes import build_canary_coverage_report

_MAX_CANDIDATES = "1"
_MAX_EVALUATED = "1"
_EXA_BUDGET_USD = "0.15"
_DEEPSEEK_BUDGET_USD = "0.01"
_EXA_PEOPLE_BUDGET_USD = "0.02"
_MAX_CONTACTS = "1"
_MAX_PAID_CONTACTS = "1"
_CLAY_MAX_CONTACTS = "1"
_APOLLO_CREDIT_CAP = "1"
_INSTANTLY_CALL_CAP = "1"


def _parser() -> argparse.ArgumentParser:
    """Expose only run identity/data location; safety ceilings are intentionally not arguments."""
    parser = argparse.ArgumentParser(prog="python -m leads_discovery.production_canary")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    return parser


def _outcome_code(outcome: str) -> int:
    """Make the workflow green only for decisive overall canary success."""
    if outcome == "success":
        return 0
    if outcome == "inconclusive":
        return 2
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run normal product work first, then derive private readiness evidence from state."""
    args = _parser().parse_args(argv)
    data_root = str(args.data_root)
    run_code = cli_main(
        [
            "run",
            "--run-id",
            args.run_id,
            "--data-root",
            data_root,
            "--max-candidates",
            _MAX_CANDIDATES,
            "--max-evaluated",
            _MAX_EVALUATED,
            "--exa-budget-usd",
            _EXA_BUDGET_USD,
            "--deepseek-budget-usd",
            _DEEPSEEK_BUDGET_USD,
            "--execute-live",
        ]
    )
    if run_code == 0:
        cli_main(
            [
                "enrich",
                "--run-id",
                args.run_id,
                "--data-root",
                data_root,
                "--max-contacts-per-company",
                _MAX_CONTACTS,
                "--max-paid-contacts-per-company",
                _MAX_PAID_CONTACTS,
                "--exa-people-budget-usd",
                _EXA_PEOPLE_BUDGET_USD,
                "--clay-max-contacts",
                _CLAY_MAX_CONTACTS,
                "--apollo-credit-cap",
                _APOLLO_CREDIT_CAP,
                "--instantly-verification-call-cap",
                _INSTANTLY_CALL_CAP,
                "--execute-live",
            ]
        )
    try:
        report = build_canary_coverage_report(args.data_root, run_id=args.run_id)
    except (OSError, UnicodeError, ValueError):
        return 1
    return _outcome_code(report.overall_outcome)


if __name__ == "__main__":
    raise SystemExit(main())
