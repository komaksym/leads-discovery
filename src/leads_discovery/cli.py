"""Top-level CLI composing paid M2 work with local M3 evaluation and calibration."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Never, cast

from leads_discovery.calibration import CalibrationSummary, calibrate_run
from leads_discovery.models import RunCheckpoint
from leads_discovery.pipeline.evaluation import (
    EvaluationConfig,
    EvaluationSummary,
    evaluate_run,
)
from leads_discovery.pipeline.m2_batch import (
    M2BatchConfig,
    MissingProviderCredentials,
    resolve_m2_paths,
    run_live_m2,
)
from leads_discovery.pipeline.state import load_checkpoint, write_checkpoint


class _ArgumentParser(argparse.ArgumentParser):
    """Convert parser failures into sanitized application failures."""

    def error(self, message: str) -> Never:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="python -m leads_discovery")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run")
    run.add_argument("--run-id", required=True)
    run.add_argument("--data-root", type=Path, default=Path("data"))
    run.add_argument("--max-candidates", type=int, default=100)
    run.add_argument("--max-evaluated", type=int, default=20)
    run.add_argument("--include-apify", action="store_true")
    run.add_argument("--apify-budget-usd", type=float, default=0.25)
    run.add_argument("--deepseek-budget-usd", type=float, required=True)
    run.add_argument("--exa-budget-usd", type=float)
    run.add_argument("--exa-request-reservation-usd", type=float)
    run.add_argument("--execute-live", action="store_true")

    score = commands.add_parser("score")
    score.add_argument("--run-id", required=True)
    score.add_argument("--data-root", type=Path, default=Path("data"))
    score.add_argument("--max-evaluated", type=int, default=20)

    calibrate = commands.add_parser("calibrate")
    calibrate.add_argument("--run-id", required=True)
    calibrate.add_argument("--labels", type=Path, required=True)
    calibrate.add_argument("--data-root", type=Path, default=Path("data"))
    return parser


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False))


def _m2_config(args: argparse.Namespace, *, execute_live: bool) -> M2BatchConfig:
    """Translate CLI names into the single M2 configuration object."""
    return M2BatchConfig(
        run_id=args.run_id,
        data_root=args.data_root,
        max_candidates=args.max_candidates,
        max_extracted=args.max_evaluated,
        include_apify=args.include_apify,
        apify_budget_usd=args.apify_budget_usd,
        deepseek_budget_usd=args.deepseek_budget_usd,
        exa_budget_usd=args.exa_budget_usd,
        exa_request_reservation_usd=args.exa_request_reservation_usd,
        execute_live=execute_live,
    )


def _evaluation_json(summary: EvaluationSummary) -> dict[str, Any]:
    return {
        "run_id": summary.run_id,
        "policy_version": summary.policy_version,
        "evaluated_count": summary.evaluated_count,
        "accepted_count": summary.accepted_count,
        "rejected_count": summary.rejected_count,
        "uncertain_count": summary.uncertain_count,
        "artifacts": [path.name for path in summary.artifact_paths],
    }


def _calibration_json(summary: CalibrationSummary) -> dict[str, Any]:
    return {
        "run_id": summary.run_id,
        "policy_version": summary.policy_version,
        "evaluated_count": summary.evaluated_count,
        "labeled_count": summary.labeled_count,
        "unlabeled_count": summary.unlabeled_count,
        "critical_disagreement_count": summary.critical_disagreement_count,
        "report_path": summary.report_path.name,
        "joined_csv_path": summary.joined_csv_path.name,
    }


def _mark_m3_completed(path: Path, checkpoint: RunCheckpoint) -> RunCheckpoint:
    durable = load_checkpoint(path) if path.exists() else None
    target = checkpoint if durable is None else durable
    raw_stages = target.provider_state.setdefault("stages", {})
    if not isinstance(raw_stages, dict):
        raise ValueError("checkpoint stages must be an object")
    stages = cast(dict[str, Any], raw_stages)
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in stages.items()):
        raise ValueError("checkpoint stages must be a string map")
    stages["evaluation"] = "completed"
    stages["m3_pipeline"] = "completed"
    target.status = "completed"
    target.pending_company_id = None
    target.pending_stage = None
    target.pause_reason = None
    target.updated_at = datetime.now(UTC).isoformat()
    if path.exists():
        write_checkpoint(path, target)
    return target


def _run_dry(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    config = _m2_config(args, execute_live=False)
    resolve_m2_paths(config)
    return (
        {
            "command": "run",
            "run_id": config.run_id,
            "status": "dry_run",
            "reason": "live_execution_not_authorized",
            "max_candidates": config.max_candidates,
            "max_evaluated": config.max_extracted,
        },
        0,
    )


def _run_live(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    config = _m2_config(args, execute_live=True)
    paths = resolve_m2_paths(config)
    try:
        live = run_live_m2(config)
    except MissingProviderCredentials:
        return (
            {
                "command": "run",
                "run_id": config.run_id,
                "status": "failed",
                "reason": "required_provider_credentials_missing",
            },
            1,
        )

    checkpoint = live.checkpoint
    evaluation: EvaluationSummary | None = None
    if checkpoint.status in {"completed", "paused_budget", "paused_unknown"}:
        evaluation = evaluate_run(
            EvaluationConfig(
                run_id=config.run_id,
                data_root=config.data_root,
                max_evaluated=config.max_extracted,
            )
        )
    if checkpoint.status == "completed" and evaluation is not None:
        checkpoint = _mark_m3_completed(paths.checkpoint, checkpoint)

    payload: dict[str, Any] = {
        "command": "run",
        "run_id": config.run_id,
        "status": checkpoint.status,
        "pending_company_id": checkpoint.pending_company_id,
        "pending_stage": checkpoint.pending_stage,
        "pause_reason": checkpoint.pause_reason,
        "apify_enabled": live.apify_enabled,
    }
    if evaluation is not None:
        payload["evaluation"] = _evaluation_json(evaluation)
    if checkpoint.status in {"paused_budget", "paused_unknown"}:
        return payload, 2
    return payload, 0 if checkpoint.status == "completed" else 1


def _score(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    summary = evaluate_run(
        EvaluationConfig(
            run_id=args.run_id,
            data_root=args.data_root,
            max_evaluated=args.max_evaluated,
        )
    )
    return {"command": "score", "status": "completed", **_evaluation_json(summary)}, 0


def _calibrate(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    summary = calibrate_run(
        EvaluationConfig(run_id=args.run_id, data_root=args.data_root),
        labels_path=args.labels,
    )
    return {"command": "calibrate", "status": "completed", **_calibration_json(summary)}, 0


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one CLI command and print exactly one sanitized JSON result."""
    try:
        args = _parser().parse_args(argv)
        if args.command == "run":
            payload, code = _run_live(args) if args.execute_live else _run_dry(args)
        elif args.command == "score":
            payload, code = _score(args)
        elif args.command == "calibrate":
            payload, code = _calibrate(args)
        else:
            raise ValueError("unsupported command")
    except ValueError:
        payload, code = {"status": "failed", "error": "invalid_input_or_state"}, 1
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload, code = {"status": "failed", "error": "filesystem_or_state_error"}, 1
    _print(payload)
    return code
