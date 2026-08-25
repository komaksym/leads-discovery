"""Opaque Git-backed paid-operation barriers for ephemeral GitHub-hosted runners."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from leads_discovery.models import RunCheckpoint

_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_APIFY_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PREFIX = "leads-op-v1"


def _configured() -> tuple[Path, str, str] | None:
    """Return validated Git journal configuration, or None outside production Actions."""
    branch = os.getenv("LEADS_GIT_JOURNAL_BRANCH")
    if branch is None:
        return None
    if not _BRANCH.fullmatch(branch) or ".." in branch or "//" in branch:
        raise ValueError("LEADS_GIT_JOURNAL_BRANCH is invalid")
    remote = os.getenv("LEADS_GIT_JOURNAL_REMOTE", "origin")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", remote):
        raise ValueError("LEADS_GIT_JOURNAL_REMOTE is invalid")
    root = Path(os.getenv("GITHUB_WORKSPACE", ".")).resolve()
    if not (root / ".git").exists():
        raise RuntimeError("Git operation journal requires a repository checkout")
    return root, remote, branch


def _git(root: Path, *args: str, input_text: str | None = None) -> str:
    """Run one fixed Git command without exposing credentials or subprocess output."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Git operation journal command failed") from exc
    return completed.stdout.strip()


def _operation_hash(run_id: str, operation_id: str) -> str:
    """Hash run and operation identity so Git metadata does not expose company/contact IDs."""
    return hashlib.sha256(f"{run_id}\0{operation_id}".encode()).hexdigest()[:32]


def _previous_operations(previous: RunCheckpoint | None) -> dict[str, dict[str, Any]]:
    """Return the previous local operation map when it is structurally usable."""
    if previous is None:
        return {}
    raw = previous.provider_state.get("operations", {})
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _remote_subject(root: Path, ref: str, operation_hash: str) -> str | None:
    """Return the latest matching opaque operation subject from the journal ref."""
    output = _git(
        root,
        "log",
        "-1",
        "--format=%s",
        "--fixed-strings",
        f"--grep={_PREFIX} {operation_hash} ",
        ref,
    )
    return output or None


def _subject(operation_hash: str, state: str, entry: dict[str, Any]) -> str:
    """Build one bounded non-sensitive operation journal subject."""
    subject = f"{_PREFIX} {operation_hash} {state}"
    if entry.get("provider") == "apify":
        run_id = entry.get("run_id")
        if isinstance(run_id, str) and _APIFY_RUN_ID.fullmatch(run_id):
            subject += f" run={run_id}"
    return subject


def _remote_state(subject: str | None) -> str | None:
    """Parse only the coarse journal state from one trusted-format commit subject."""
    if subject is None:
        return None
    parts = subject.split()
    if len(parts) < 3 or parts[0] != _PREFIX:
        return None
    return parts[2]


def _append_subject(root: Path, remote: str, branch: str, subject: str) -> None:
    """Append one metadata-only commit while preserving the branch tree exactly."""
    ref = f"refs/remotes/{remote}/{branch}"
    parent = _git(root, "rev-parse", ref)
    tree = _git(root, "rev-parse", f"{parent}^{{tree}}")
    commit = _git(root, "commit-tree", tree, "-p", parent, "-m", subject)
    _git(root, "push", remote, f"{commit}:refs/heads/{branch}")
    _git(root, "update-ref", ref, commit)


def sync_checkpoint_barrier(
    checkpoint: RunCheckpoint,
    previous: RunCheckpoint | None,
) -> None:
    """Durably mirror paid-operation transitions before local checkpoint replacement.

    New in-flight transitions are rejected when the same deterministic operation already has
    an unresolved, completed, or failed remote barrier. Only an explicitly persisted ``pending``
    state may be retried. Existing local in-flight state may be re-persisted without creating a
    second barrier, which preserves normal same-workspace process resume behavior.
    """
    config = _configured()
    if config is None:
        return
    root, remote, branch = config
    if not _RUN_ID.fullmatch(checkpoint.run_id):
        raise ValueError("checkpoint run_id is invalid for Git operation journal")
    ref = f"refs/remotes/{remote}/{branch}"
    _git(root, "rev-parse", "--verify", ref)

    current_raw = checkpoint.provider_state.get("operations", {})
    if not isinstance(current_raw, dict):
        raise ValueError("checkpoint operations must be an object")
    prior = _previous_operations(previous)
    for operation_id, raw_entry in sorted(current_raw.items()):
        if not isinstance(operation_id, str) or not isinstance(raw_entry, dict):
            raise ValueError("checkpoint operation entries must be objects")
        entry = raw_entry
        state = entry.get("state")
        if state not in {"in_flight", "completed", "failed", "pending"}:
            raise ValueError("checkpoint operation has an invalid journal state")
        op_hash = _operation_hash(checkpoint.run_id, operation_id)
        latest = _remote_subject(root, ref, op_hash)
        latest_state = _remote_state(latest)
        desired = _subject(op_hash, state, entry)
        old_entry = prior.get(operation_id)
        old_state = old_entry.get("state") if old_entry is not None else None
        new_dispatch = state == "in_flight" and old_state != "in_flight"

        if new_dispatch and latest_state not in {None, "pending"}:
            raise RuntimeError("paid operation already has a durable non-retryable Git barrier")
        if latest == desired:
            continue
        if state == "in_flight" and old_state == "in_flight" and latest_state != "in_flight":
            raise RuntimeError("local in-flight operation disagrees with durable Git barrier")
        _append_subject(root, remote, branch, desired)
