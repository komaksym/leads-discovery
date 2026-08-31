"""Fixed production canary entry point for one explicitly authorized GitHub-hosted run."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from leads_discovery.cli import main as cli_main

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
    parser.add_argument("--data-root", default="data")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the existing pipeline and enrichment CLI under immutable canary ceilings."""
    args = _parser().parse_args(argv)
    run_code = cli_main(
        [
            "run",
            "--run-id",
            args.run_id,
            "--data-root",
            args.data_root,
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
    if run_code != 0:
        return run_code
    return cli_main(
        [
            "enrich",
            "--run-id",
            args.run_id,
            "--data-root",
            args.data_root,
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


if __name__ == "__main__":
    raise SystemExit(main())
