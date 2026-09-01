"""Top-level CLI for M2/M3 company evaluation and explicit M4 contact enrichment."""

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
from leads_discovery.discovery import normalize_discovery_configuration
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
    """Build the run, score, calibrate, and explicit enrich command surface."""
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
    run.add_argument("--market", default="PVF")
    run.add_argument(
        "--search-term",
        "--search-query",
        "--search-terms",
        dest="search_terms",
        action="append",
        default=None,
    )
    run.add_argument(
        "--target-geography",
        "--target-country",
        "--target-geographies",
        "--target-countries",
        dest="target_geographies",
        action="append",
        default=None,
    )
    run.add_argument("--execute-live", action="store_true")

    score = commands.add_parser("score")
    score.add_argument("--run-id", required=True)
    score.add_argument("--data-root", type=Path, default=Path("data"))
    score.add_argument("--max-evaluated", type=int, default=20)

    calibrate = commands.add_parser("calibrate")
    calibrate.add_argument("--run-id", required=True)
    calibrate.add_argument("--labels", type=Path, required=True)
    calibrate.add_argument("--data-root", type=Path, default=Path("data"))

    enrich = commands.add_parser("enrich")
    enrich.add_argument("--run-id", required=True)
    enrich.add_argument("--data-root", type=Path, default=Path("data"))
    enrich.add_argument("--max-contacts-per-company", type=int, default=3)
    enrich.add_argument("--max-paid-contacts-per-company", type=int, default=2)
    enrich.add_argument("--exa-people-budget-usd", type=float)
    enrich.add_argument("--clay-max-contacts", type=int, default=10)
    enrich.add_argument("--apollo-credit-cap", type=float, default=5.0)
    enrich.add_argument("--instantly-verification-call-cap", type=int, default=5)
    enrich.add_argument("--execute-live", action="store_true")
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


def _validate_run_id(run_id: object) -> str:
    """Validate and return one safe run identifier."""
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("run_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return run_id


def _validate_run_inputs(args: argparse.Namespace) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Validate dry/live M2/M3 run controls without provider imports or credentials."""
    _validate_run_id(args.run_id)
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
    market, terms, geographies = normalize_discovery_configuration(
        market=args.market,
        search_terms=tuple(args.search_terms or ()),
        target_geographies=tuple(args.target_geographies or ("US", "CA")),
    )
    return market, terms, geographies


def _validate_enrich_inputs(args: argparse.Namespace) -> None:
    """Validate M4 scalar controls without filesystem, environment, or provider access."""
    _validate_run_id(args.run_id)
    if (
        isinstance(args.max_contacts_per_company, bool)
        or not isinstance(args.max_contacts_per_company, int)
        or not 1 <= args.max_contacts_per_company <= 3
    ):
        raise ValueError("max_contacts_per_company must be an integer in 1..3")
    if (
        isinstance(args.max_paid_contacts_per_company, bool)
        or not isinstance(args.max_paid_contacts_per_company, int)
        or not 0 <= args.max_paid_contacts_per_company <= 2
        or args.max_paid_contacts_per_company > args.max_contacts_per_company
    ):
        raise ValueError("max_paid_contacts_per_company must be in 0..2 and <= max contacts")
    if (
        isinstance(args.clay_max_contacts, bool)
        or not isinstance(args.clay_max_contacts, int)
        or args.clay_max_contacts < 0
    ):
        raise ValueError("clay_max_contacts must be a nonnegative integer")
    if (
        isinstance(args.instantly_verification_call_cap, bool)
        or not isinstance(args.instantly_verification_call_cap, int)
        or args.instantly_verification_call_cap < 0
    ):
        raise ValueError("instantly_verification_call_cap must be a nonnegative integer")
    _validate_number("apollo_credit_cap", args.apollo_credit_cap)
    if args.exa_people_budget_usd is not None:
        _validate_number("exa_people_budget_usd", args.exa_people_budget_usd)


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
    """Return an M2/M3 dry-run summary without filesystem, credentials, or provider imports."""
    market, search_terms, target_geographies = _validate_run_inputs(args)
    return (
        {
            "command": "run",
            "run_id": args.run_id,
            "status": "dry_run",
            "reason": "live_execution_not_authorized",
            "max_candidates": args.max_candidates,
            "max_evaluated": args.max_evaluated,
            "market": market,
            "search_terms": list(search_terms),
            "target_geographies": list(target_geographies),
        },
        0,
    )


def _run_live(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Compose existing M2 providers only after explicit live authorization."""
    market, search_terms, target_geographies = _validate_run_inputs(args)
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
        market=market,
        search_terms=search_terms,
        target_geographies=target_geographies,
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

    with httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
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


def _enrich_dry(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Return an M4 dry-run summary without reading credentials or touching run artifacts."""
    _validate_enrich_inputs(args)
    return (
        {
            "command": "enrich",
            "run_id": args.run_id,
            "status": "dry_run",
            "reason": "live_execution_not_authorized",
            "max_contacts_per_company": args.max_contacts_per_company,
            "max_paid_contacts_per_company": args.max_paid_contacts_per_company,
            "clay_max_contacts": args.clay_max_contacts,
            "apollo_credit_cap": args.apollo_credit_cap,
            "instantly_verification_call_cap": args.instantly_verification_call_cap,
        },
        0,
    )


def _enrich_live(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Preflight durable state before constructing any explicitly authorized live provider."""
    _validate_enrich_inputs(args)
    if args.exa_people_budget_usd is None:
        raise ValueError("live enrichment requires an explicit Exa People budget")

    from leads_discovery.pipeline.contact_enrichment import (
        ContactEnrichmentConfig,
        run_contact_enrichment,
        validate_contact_enrichment_state,
    )

    config = ContactEnrichmentConfig(
        run_id=args.run_id,
        data_root=args.data_root,
        max_contacts_per_company=args.max_contacts_per_company,
        max_paid_contacts_per_company=args.max_paid_contacts_per_company,
        exa_people_budget_usd=args.exa_people_budget_usd,
        clay_max_contacts=args.clay_max_contacts,
        apollo_credit_cap=args.apollo_credit_cap,
        instantly_verification_call_cap=args.instantly_verification_call_cap,
        execute_live=True,
    )
    validate_contact_enrichment_state(config)

    import os

    import httpx

    from leads_discovery.contacts.providers import (
        ApolloContactProvider,
        ClayContactProvider,
        ExaPeopleProvider,
        InstantlyVerificationProvider,
    )

    names = (
        "EXA_API_KEY",
        "CLAY_PUBLIC_API_KEY",
        "CLAY_CONTACT_ROUTINE_ID",
        "APOLLO_API_KEY",
        "INSTANTLY_API_KEY",
    )
    credentials = {name: os.environ.get(name, "") for name in names}
    if any(not credentials[name] for name in names):
        return (
            {
                "command": "enrich",
                "run_id": args.run_id,
                "status": "failed",
                "reason": "required_provider_credentials_missing",
            },
            1,
        )
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
        summary = run_contact_enrichment(
            config,
            exa=ExaPeopleProvider(api_key=credentials["EXA_API_KEY"], client=client),
            clay=ClayContactProvider(
                api_key=credentials["CLAY_PUBLIC_API_KEY"],
                routine_id=credentials["CLAY_CONTACT_ROUTINE_ID"],
                client=client,
            ),
            apollo=ApolloContactProvider(api_key=credentials["APOLLO_API_KEY"], client=client),
            instantly=InstantlyVerificationProvider(
                api_key=credentials["INSTANTLY_API_KEY"], client=client
            ),
        )
    payload = {
        "command": "enrich",
        "run_id": summary.run_id,
        "status": summary.status,
        "accepted_company_count": summary.accepted_company_count,
        "contact_count": summary.contact_count,
        "paid_candidate_count": summary.paid_candidate_count,
        "verified_email_count": summary.verified_email_count,
        "artifacts": [path.name for path in summary.artifact_paths],
    }
    if summary.status in {"paused_budget", "paused_unknown", "paused_pending"}:
        return payload, 2
    return payload, 0 if summary.status == "completed" else 1


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
    """Execute one CLI command and print exactly one sanitized JSON result."""
    try:
        args = _parser().parse_args(argv)
        if args.command == "run":
            payload, code = _run_live(args) if args.execute_live else _run_dry(args)
        elif args.command == "enrich":
            payload, code = _enrich_live(args) if args.execute_live else _enrich_dry(args)
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
