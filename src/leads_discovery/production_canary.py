"""Fixed production canary entry point for one explicitly authorized GitHub-hosted run."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
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
_STATUS_READS_ADMITTED_KEY = "status_reads_admitted"


@dataclass(frozen=True, slots=True)
class _PendingStatusRead:
    """Bind one persisted async operation to its exact status-read quota identity."""

    pause_reason: str
    provider: str
    event_operation: str
    identity_key: str
    identity_value: str
    admitted_reads: int


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


def _persisted_status_read_count(state: dict[str, object]) -> int | None:
    """Read the durable pre-dispatch status-read count, accepting old checkpoints as zero."""
    raw = state.get(_STATUS_READS_ADMITTED_KEY, 0)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw


def _normal_m4_pending_identity(
    data_root: Path,
    run_id: str,
) -> _PendingStatusRead | None:
    """Resolve one persisted pending operation to its durable exact read identity."""
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
    provider_state = payload.get("provider_state")
    if not isinstance(provider_state, dict):
        return None
    operations = provider_state.get("operations")
    if not isinstance(operations, dict):
        return None

    reason = payload.get("pause_reason")
    if reason == "clay_pending":
        state = operations.get("clay:batch")
        if not isinstance(state, dict) or state.get("state") != "pending":
            return None
        routine_run_id = state.get("routine_run_id")
        admitted_reads = _persisted_status_read_count(state)
        if (
            not isinstance(routine_run_id, str)
            or not routine_run_id.strip()
            or admitted_reads is None
        ):
            return None
        return _PendingStatusRead(
            pause_reason=reason,
            provider="clay",
            event_operation="work_email_routine_results",
            identity_key="routine_run_id",
            identity_value=routine_run_id,
            admitted_reads=admitted_reads,
        )

    if not isinstance(reason, str) or not reason.startswith("instantly:") or reason == "instantly:":
        return None
    state = operations.get(reason)
    if not isinstance(state, dict) or state.get("state") != "pending":
        return None
    email = state.get("email")
    admitted_reads = _persisted_status_read_count(state)
    if not isinstance(email, str) or not email.strip() or admitted_reads is None:
        return None
    return _PendingStatusRead(
        pause_reason=reason,
        provider="instantly",
        event_operation="email_verification_get",
        identity_key="email",
        identity_value=email,
        admitted_reads=admitted_reads,
    )


def _normal_m4_status_read_count(
    data_root: Path,
    run_id: str,
    operation: _PendingStatusRead,
) -> int | None:
    """Count reads using both durable admissions and backward-compatible usage evidence."""
    try:
        events = load_usage_events(data_root / run_id / "contact_usage_events.jsonl")
    except (OSError, UnicodeError, ValueError):
        return None

    reads = 0
    for event in events:
        if event.provider != operation.provider or event.operation != operation.event_operation:
            continue
        recorded_identity = event.metadata.get(operation.identity_key)
        if not isinstance(recorded_identity, str) or not recorded_identity.strip():
            return None
        if recorded_identity == operation.identity_value:
            reads += event.request_count
    return max(reads, operation.admitted_reads)


def _normal_m4_resume_allowed(data_root: Path, run_id: str) -> bool:
    """Admit only new or valid pending M4 work while its durable read quota remains."""
    checkpoint_path = data_root / run_id / "contact_checkpoint.json"
    try:
        payload = read_json(checkpoint_path)
    except (OSError, UnicodeError, ValueError):
        return False
    if payload is None:
        return True
    if not isinstance(payload, dict) or payload.get("run_id") != run_id:
        return False
    status = payload.get("status")
    if status == "completed":
        return True
    if status != "paused_pending":
        return False

    operation = _normal_m4_pending_identity(data_root, run_id)
    if operation is None:
        return False
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
        persisted_pending = _normal_m4_pending_identity(args.data_root, args.run_id)
        if _normal_m4_resume_allowed(args.data_root, args.run_id):
            if persisted_pending is not None:
                sleep(_ASYNC_POLL_DELAY_SECONDS)
            if _normal_m4_resume_allowed(args.data_root, args.run_id):
                enrich_code = cli_main(enrich_args)
            else:
                enrich_code = 2
                normal_pending = True
        else:
            enrich_code = 2
            normal_pending = True

        while enrich_code == 2:
            pending_operation = _normal_m4_pending_identity(
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
