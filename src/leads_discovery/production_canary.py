"""Fixed production canary entry point for one explicitly authorized GitHub-hosted run."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from time import sleep

from leads_discovery.cli import main as cli_main
from leads_discovery.pipeline.canary_outcomes import build_canary_coverage_report
from leads_discovery.pipeline.canary_provider_coverage import run_live_provider_coverage
from leads_discovery.pipeline.state import read_json

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
_ASYNC_POLL_DELAY_SECONDS = 10.0
_NORMAL_PENDING_RESUME_LIMIT = 3
_COVERAGE_MAX_PASSES = 7


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m leads_discovery.production_canary")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    return parser


def _outcome_code(outcome: str) -> int:
    if outcome == "success":
        return 0
    if outcome == "inconclusive":
        return 2
    return 1


def _normal_m4_is_paused_pending(data_root: Path, run_id: str) -> bool:
    """Admit a same-run M4 resume only from its explicit durable pending checkpoint."""
    try:
        payload = read_json(data_root / run_id / "contact_checkpoint.json")
    except (OSError, UnicodeError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("run_id") == run_id
        and payload.get("status") == "paused_pending"
    )


def main(argv: Sequence[str] | None = None) -> int:
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

    coverage_failed = False
    coverage_pending = False
    if run_code == 0:
        enrich_args = [
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
        enrich_code = cli_main(enrich_args)
        for _ in range(_NORMAL_PENDING_RESUME_LIMIT):
            if enrich_code != 2 or not _normal_m4_is_paused_pending(
                args.data_root, args.run_id
            ):
                break
            sleep(_ASYNC_POLL_DELAY_SECONDS)
            enrich_code = cli_main(enrich_args)

        if enrich_code == 0:
            try:
                coverage = run_live_provider_coverage(args.data_root, run_id=args.run_id)
                passes = 1
                while coverage.status == "pending" and passes < _COVERAGE_MAX_PASSES:
                    sleep(_ASYNC_POLL_DELAY_SECONDS)
                    coverage = run_live_provider_coverage(
                        args.data_root,
                        run_id=args.run_id,
                    )
                    passes += 1
                coverage_pending = coverage.status == "pending"
            except Exception:
                coverage_failed = True

    try:
        report = build_canary_coverage_report(args.data_root, run_id=args.run_id)
    except (OSError, UnicodeError, ValueError):
        return 1
    if coverage_failed:
        return 1
    if coverage_pending and report.overall_outcome == "success":
        return 2
    return _outcome_code(report.overall_outcome)


if __name__ == "__main__":
    raise SystemExit(main())
