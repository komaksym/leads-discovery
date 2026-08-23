"""Top-level M3 CLI composing paid M2 work with zero-cost local operations."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Never, cast

from leads_discovery.calibration import CalibrationSummary, calibrate_run
from leads_discovery.models import RunCheckpoint
from leads_discovery.pipeline.evaluation import (
    EvaluationConfig,
    EvaluationSummary,
    evaluate_run,
)
from leads_discovery.pipeline.state import load_checkpoint, write_checkpoint

_RUN_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class _ArgumentParser(argparse.ArgumentParser):
    """Convert CLI parse failures into sanitized application-level failures."""

    def error(self, message: str) -> Never:
        """Raise instead of printing argparse's unsanitized error and exiting with code two."""
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    """Build the exact run, score, and calibrate command surface."""
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
    """Print exactly one deterministic sanitized JSON summary."""
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False))


def _validate_number(name: str, value: object, *, maximum: float | None = None) -> float:
    """Validate one nonnegative finite CLI budget without coercing booleans."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or (maximum is not None and value > maximum)
    ):
        suffix = "" if maximum is None else f" in 0..{maximum:g}"
        raise ValueError(f"{name} must be a nonnegative finite number{suffix}")
    return float(value)


def _validate_run_inputs(args: argparse.Namespace) -> None:
    """Validate dry/live run controls without importing provider modules or reading credentials."""
    if not isinstance(args.run_id, str) or not _RUN_ID.fullmatch(args.run_id):
        raise ValueError("run_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    if (
        isinstance(args.max_candidates, bool)
        or not isinstance(args.max_candidates, int)
        or not 1 <= args.max_candidates <= 100
    ):
        raise ValueError("max_candidates must be an integer in 1..100")
    if (
        isinstance(args.max_evaluated, bool)
        or not isinstance(args.max_evaluated, int)
        or not 1 <= args.max_evaluated <= 20
    ):
        raise ValueError("max_evaluated must be an integer in 1..20")
    _validate_number("apify_budget_usd", args.apify_budget_usd, maximum=1.0)
    _validate_number("deepseek_budget_usd", args.deepseek_budget_usd)
    if args.exa_budget_usd is not None:
        _validate_number("exa_budget_usd", args.exa_budget_usd)


def _evaluation_json(summary: EvaluationSummary) -> dict[str, Any]:
    """Serialize an evaluation summary without exposing absolute filesystem paths."""
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
    """Serialize a calibration summary without exposing absolute filesystem paths."""
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
    """Persist only M3 completion stage markers after a fully completed paid M2 run."""
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
    """Return an authorized dry-run summary without filesystem, credentials, or provider imports."""
    _validate_run_inputs(args)
    return (
        {
            "command": "run",
            "run_id": args.run_id,
            "status": "dry_run",
            "reason": "live_execution_not_authorized",
            "max_candidates": args.max_candidates,
            "max_evaluated": args.max_evaluated,
        },
        0,
    )


def _run_live(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Compose existing M2 providers only after explicit live authorization."""
    _validate_run_inputs(args)
    if args.deepseek_budget_usd <= 0:
        raise ValueError("live extraction requires a positive explicit DeepSeek budget")

    import os

    import httpx

    from leads_discovery.discovery import (
        ApifyDiscoveryProvider,
        DiscoveryProvider,
        ExaDiscoveryProvider,
    )
    from leads_discovery.pipeline import m2_batch
    from leads_discovery.research import DeepSeekExtractor, ExaEvidenceResearcher

    config = m2_batch.M2BatchConfig(
        run_id=args.run_id,
        data_root=args.data_root,
        max_candidates=args.max_candidates,
        max_extracted=args.max_evaluated,
        include_apify=args.include_apify,
        apify_budget_usd=args.apify_budget_usd,
        deepseek_budget_usd=args.deepseek_budget_usd,
        exa_budget_usd=args.exa_budget_usd,
        execute_live=True,
    )
    paths = m2_batch._validate_config(config)
    exa_key = os.environ.get("EXA_API_KEY", "")
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    apify_token = os.environ.get("APIFY_TOKEN", "")
    if not exa_key or not deepseek_key:
        return (
            {
                "command": "run",
                "run_id": args.run_id,
                "status": "failed",
                "reason": "required_provider_credentials_missing",
            },
            1,
        )
    if config.include_apify and not apify_token:
        config = replace(config, include_apify=False)

    with httpx.Client() as client:
        discovery: dict[str, DiscoveryProvider] = {
            "exa": ExaDiscoveryProvider(api_key=exa_key, client=client),
        }
        if config.include_apify and apify_token:
            discovery["apify"] = ApifyDiscoveryProvider(
                api_token=apify_token,
                client=client,
                on_run_started=m2_batch._ApifyRunRecorder(paths.checkpoint),
            )
        researcher = ExaEvidenceResearcher(api_key=exa_key, client=client)
        extractor = DeepSeekExtractor(
            api_key=deepseek_key,
            client=client,
            model=m2_batch._DEEPSEEK_MODEL,
            prices=m2_batch._DEFAULT_PRICES,
        )
        checkpoint = m2_batch.run_m2_batch(
            config,
            discovery=discovery,
            researcher=researcher,
            extractor=extractor,
        )

    evaluation: EvaluationSummary | None = None
    if checkpoint.status in {"completed", "paused_budget", "paused_unknown"}:
        evaluation = evaluate_run(
            EvaluationConfig(
                run_id=args.run_id,
                data_root=args.data_root,
                max_evaluated=args.max_evaluated,
            )
        )
    if checkpoint.status == "completed" and evaluation is not None:
        checkpoint = _mark_m3_completed(paths.checkpoint, checkpoint)

    payload: dict[str, Any] = {
        "command": "run",
        "run_id": args.run_id,
        "status": checkpoint.status,
        "pending_company_id": checkpoint.pending_company_id,
        "pending_stage": checkpoint.pending_stage,
        "pause_reason": checkpoint.pause_reason,
        "apify_enabled": config.include_apify,
    }
    if evaluation is not None:
        payload["evaluation"] = _evaluation_json(evaluation)
    if checkpoint.status in {"paused_budget", "paused_unknown"}:
        return payload, 2
    return payload, 0 if checkpoint.status == "completed" else 1


def _score(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Run local evaluation without importing provider composition code or reading credentials."""
    summary = evaluate_run(
        EvaluationConfig(
            run_id=args.run_id,
            data_root=args.data_root,
            max_evaluated=args.max_evaluated,
        )
    )
    return {"command": "score", "status": "completed", **_evaluation_json(summary)}, 0


def _calibrate(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Run local manual-label calibration without provider composition or credentials."""
    summary = calibrate_run(
        EvaluationConfig(run_id=args.run_id, data_root=args.data_root),
        labels_path=args.labels,
    )
    return {"command": "calibrate", "status": "completed", **_calibration_json(summary)}, 0


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one M3 CLI command and print exactly one sanitized JSON result."""
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
    except Exception:
        payload, code = {"status": "failed", "error": "operation_failed"}, 1
    _print(payload)
    return code
