"""Fixed production canary entry point for one explicitly authorized GitHub-hosted run."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from time import sleep

from leads_discovery.cli import main as cli_main
from leads_discovery.pipeline.canary_outcomes import build_canary_coverage_report
from leads_discovery.pipeline.canary_provider_coverage import run_live_provider_coverage
from leads_discovery.pipeline.state import load_usage_events, read_json

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
_NORMAL_ASYNC_READ_LIMIT = 3
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


def _normal_m4_pending_operation(data_root: Path, run_id: str) -> str | None:
    """Return the explicit durable async operation that admits one same-run resume."""
    try:
        payload = read_json(data_root / run_id / "contact_checkpoint.json")
    except (OSError, UnicodeError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("run_id") != run_id
        or payload.get("status") != "paused_pending"
    ):
        return None
    reason = payload.get("pause_reason")
    if reason == "clay_pending":
        return "clay_pending"
    if isinstance(reason, str) and reason.startswith("instantly:") and reason != "instantly:":
        return reason
    return None


def _normal_m4_pending_identity(
    data_root: Path,
    run_id: str,
) -> tuple[str, str, str, str] | None:
    """Resolve one persisted pending operation to its authoritative usage identity."""
    reason = _normal_m4_pending_operation(data_root, run_id)
    if reason is None:
        return None
    try:
        payload = read_json(data_root / run_id / "contact_checkpoint.json")
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    provider_state = payload.get("provider_state")
    if not isinstance(provider_state, dict):
        return None
    operations = provider_state.get("operations")
    if not isinstance(operations, dict):
        return None

    if reason == "clay_pending":
        state = operations.get("clay:batch")
        if not isinstance(state, dict) or state.get("state") != "pending":
            return None
        routine_run_id = state.get("routine_run_id")
        if not isinstance(routine_run_id, str) or not routine_run_id.strip():
            return None
        return (
            "clay",
            "work_email_routine_results",
            "routine_run_id",
            routine_run_id,
        )

    state = operations.get(reason)
    if not isinstance(state, dict) or state.get("state") != "pending":
        return None
    email = state.get("email")
    if not isinstance(email, str) or not email.strip():
        return None
    return ("instantly", "email_verification_get", "email", email)


def _normal_m4_status_read_count(
    data_root: Path,
    run_id: str,
    operation: tuple[str, str, str, str],
) -> int | None:
    """Replay authoritative normal usage and count reads for exactly one async operation."""
    provider, event_operation, identity_key, identity_value = operation
    try:
        events = load_usage_events(data_root / run_id / "contact_usage_events.jsonl")
    except (OSError, UnicodeError, ValueError):
        return None

    reads = 0
    for event in events:
        if event.provider != provider or event.operation != event_operation:
            continue
        recorded_identity = event.metadata.get(identity_key)
        if not isinstance(recorded_identity, str) or not recorded_identity.strip():
            return None
        if recorded_identity == identity_value:
            reads += event.request_count
    return reads


def _normal_m4_resume_allowed(data_root: Path, run_id: str) -> bool:
    """Admit a real pending resume only while its durable status-read quota remains."""
    checkpoint_path = data_root / run_id / "contact_checkpoint.json"
    try:
        payload = read_json(checkpoint_path)
    except (OSError, UnicodeError, ValueError):
        return False
    if payload is None:
        return True
    if not isinstance(payload, dict) or payload.get("run_id") != run_id:
        return False
    if payload.get("status") != "paused_pending":
        return True

    operation = _normal_m4_pending_identity(data_root, run_id)
    if operation is None:
        # Keep synthetic/legacy orchestration seams on the normal M4 validator path.
        # Real provider state is validated there before any provider can be called.
        return True
    reads = _normal_m4_status_read_count(data_root, run_id, operation)
    return reads is not None and reads < _NORMAL_ASYNC_READ_LIMIT


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
    normal_pending = False
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
            "--async-status-read-cap",
            str(_NORMAL_ASYNC_READ_LIMIT),
            "--execute-live",
        ]
        if _normal_m4_resume_allowed(args.data_root, args.run_id):
            enrich_code = cli_main(enrich_args)
        else:
            enrich_code = 2
            normal_pending = True

        while enrich_code == 2:
            pending_operation = _normal_m4_pending_operation(
                args.data_root,
                args.run_id,
            )
            if pending_operation is None:
                break
            if not _normal_m4_resume_allowed(args.data_root, args.run_id):
                normal_pending = True
                break
            sleep(_ASYNC_POLL_DELAY_SECONDS)
            if not _normal_m4_resume_allowed(args.data_root, args.run_id):
                normal_pending = True
                break
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
    if (normal_pending or coverage_pending) and report.overall_outcome == "success":
        return 2
    return _outcome_code(report.overall_outcome)


if __name__ == "__main__":
    raise SystemExit(main())
