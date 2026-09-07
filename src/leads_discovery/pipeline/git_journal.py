"""Opaque Git-backed paid-operation barriers for ephemeral GitHub-hosted runners."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from leads_discovery.models import RunCheckpoint

_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_APIFY_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PREFIX = "leads-op-v1"
_STATE_PREFIX = "leads-canary-state-v1"
_STATE_VERSION = 1
_STATE_MAX_BYTES = 256 * 1024
_STATE_NONCE_BYTES = 12


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


def git_journal_configured() -> bool:
    """Return whether the durable Git journal is enabled for this process."""
    return _configured() is not None


def _git(root: Path, *args: str, input_text: str | None = None) -> str:
    """Run one fixed Git command without exposing credentials or subprocess output."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            input=input_text,
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Git operation journal command failed") from exc
    return completed.stdout.strip()


def _operation_hash(run_id: str, operation_id: str) -> str:
    """Hash run and operation identity so Git metadata does not expose company/contact IDs."""
    return hashlib.sha256(f"{run_id}\0{operation_id}".encode()).hexdigest()[:32]


def _run_hash(run_id: str) -> str:
    """Hash the canary run identity for non-sensitive capsule lookup metadata."""
    return hashlib.sha256(run_id.encode()).hexdigest()[:32]


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


def _append_message(
    root: Path,
    remote: str,
    branch: str,
    subject: str,
    body: str | None = None,
) -> None:
    """Append one metadata-only commit while preserving the journal branch tree exactly."""
    ref = f"refs/remotes/{remote}/{branch}"
    parent = _git(root, "rev-parse", ref)
    tree = _git(root, "rev-parse", f"{parent}^{{tree}}")
    args = ["commit-tree", tree, "-p", parent, "-m", subject]
    if body is not None:
        args.extend(["-m", body])
    commit = _git(root, *args)
    _git(root, "push", remote, f"{commit}:refs/heads/{branch}")
    _git(root, "update-ref", ref, commit)


def _append_subject(root: Path, remote: str, branch: str, subject: str) -> None:
    """Append one operation barrier without changing the journal branch tree."""
    _append_message(root, remote, branch, subject)


def _state_subject(run_id: str) -> str:
    """Return the opaque lookup subject for one encrypted private-state capsule."""
    return f"{_STATE_PREFIX} {_run_hash(run_id)}"


def _state_key() -> bytes:
    """Derive one fixed AES-256 key from the environment-scoped canary state secret."""
    raw = os.getenv("LEADS_GIT_JOURNAL_KEY")
    if raw is None:
        raise RuntimeError("LEADS_GIT_JOURNAL_KEY is required for canary restart state")
    encoded = raw.encode("utf-8")
    if len(encoded) < 32:
        raise ValueError("LEADS_GIT_JOURNAL_KEY must contain at least 32 UTF-8 bytes")
    return hashlib.sha256(encoded).digest()


def _state_aad(run_id: str) -> bytes:
    """Bind an encrypted capsule to its exact logical run identity."""
    return f"{_STATE_PREFIX}\0{run_id}".encode("utf-8")


def persist_canary_private_state(run_id: str, payload: dict[str, Any]) -> None:
    """Encrypt and durably mirror the authoritative canary-private replay state."""
    config = _configured()
    if config is None:
        return
    root, remote, branch = config
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("canary state run_id is invalid for Git operation journal")
    envelope = {"version": _STATE_VERSION, "state": payload}
    plaintext = json.dumps(
        envelope,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(plaintext) > _STATE_MAX_BYTES:
        raise ValueError("canary private restart state exceeds its fixed Git journal bound")
    nonce = os.urandom(_STATE_NONCE_BYTES)
    ciphertext = AESGCM(_state_key()).encrypt(nonce, plaintext, _state_aad(run_id))
    body = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
    _append_message(root, remote, branch, _state_subject(run_id), body)


def load_canary_private_state(run_id: str) -> dict[str, Any] | None:
    """Decrypt the latest bounded private-state capsule for one canary run, if present."""
    config = _configured()
    if config is None:
        return None
    root, remote, branch = config
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("canary state run_id is invalid for Git operation journal")
    ref = f"refs/remotes/{remote}/{branch}"
    subject = _state_subject(run_id)
    message = _git(
        root,
        "log",
        "-1",
        "--format=%B",
        "--fixed-strings",
        f"--grep={subject}",
        ref,
    )
    if not message:
        return None
    lines = message.splitlines()
    if not lines or lines[0] != subject:
        raise ValueError("canary private Git journal capsule subject is invalid")
    body_lines = [line.strip() for line in lines[1:] if line.strip()]
    if len(body_lines) != 1:
        raise ValueError("canary private Git journal capsule body is invalid")
    try:
        encoded = body_lines[0].encode("ascii")
        encrypted = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(encrypted) <= _STATE_NONCE_BYTES + 16:
            raise ValueError("encrypted capsule is too short")
        plaintext = AESGCM(_state_key()).decrypt(
            encrypted[:_STATE_NONCE_BYTES],
            encrypted[_STATE_NONCE_BYTES:],
            _state_aad(run_id),
        )
        if len(plaintext) > _STATE_MAX_BYTES:
            raise ValueError("decrypted capsule is too large")
        envelope = json.loads(plaintext.decode("utf-8"))
    except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("canary private Git journal capsule is invalid") from exc
    if not isinstance(envelope, dict) or envelope.get("version") != _STATE_VERSION:
        raise ValueError("canary private Git journal capsule version is invalid")
    payload = envelope.get("state")
    if not isinstance(payload, dict):
        raise ValueError("canary private Git journal state must be an object")
    return payload


def sync_checkpoint_barrier(
    checkpoint: RunCheckpoint,
    previous: RunCheckpoint | None,
) -> None:
    """Durably mirror paid-operation transitions before local checkpoint replacement.

    A fresh local state may dispatch only when no durable barrier exists. A status/read retry is
    allowed only when the same local operation was already persisted as ``pending`` and the remote
    barrier agrees. Existing local in-flight state may be re-persisted without creating a second
    barrier, preserving same-workspace crash recovery while runner loss stays fail-closed.
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

        if new_dispatch:
            retrying_pending = old_state == "pending" and latest_state == "pending"
            if (old_state == "pending" and not retrying_pending) or (
                old_state != "pending" and latest_state is not None
            ):
                raise RuntimeError(
                    "paid operation already has a durable non-retryable Git barrier"
                )
        if latest == desired:
            continue
        if state == "in_flight" and old_state == "in_flight" and latest_state != "in_flight":
            raise RuntimeError("local in-flight operation disagrees with durable Git barrier")
        _append_subject(root, remote, branch, desired)
