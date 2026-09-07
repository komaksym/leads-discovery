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
_RESTART_PREFIX = "leads-canary-restart-v1"
_STATE_VERSION = 1
_STATE_MAX_BYTES = 256 * 1024
_STATE_NONCE_BYTES = 12
_CAPSULE_PREFIX = "capsule "


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


def _journal_ref(remote: str, branch: str) -> str:
    return f"refs/remotes/{remote}/{branch}"


def _empty_tree(root: Path) -> str:
    """Return Git's canonical empty-tree object for metadata-only journal commits."""
    return _git(root, "mktree", input_text="")


def _require_empty_journal_tree(root: Path, ref: str) -> str:
    """Fail closed unless the existing journal ref points at the canonical empty tree."""
    parent = _git(root, "rev-parse", "--verify", ref)
    tree = _git(root, "rev-parse", f"{parent}^{{tree}}")
    empty = _empty_tree(root)
    if tree != empty:
        raise RuntimeError("Git operation journal branch tree must be empty")
    return parent


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
    """Return the latest matching opaque operation line from journal commit metadata."""
    prefix = f"{_PREFIX} {operation_hash} "
    message = _git(
        root,
        "log",
        "-1",
        "--format=%B",
        "--fixed-strings",
        f"--grep={prefix}",
        ref,
    )
    for line in message.splitlines():
        if line.startswith(prefix):
            return line
    return None


def _subject(operation_hash: str, state: str, entry: dict[str, Any]) -> str:
    """Build one bounded non-sensitive operation journal line."""
    subject = f"{_PREFIX} {operation_hash} {state}"
    if entry.get("provider") == "apify":
        run_id = entry.get("run_id")
        if isinstance(run_id, str) and _APIFY_RUN_ID.fullmatch(run_id):
            subject += f" run={run_id}"
    return subject


def _remote_state(subject: str | None) -> str | None:
    """Parse only the coarse journal state from one trusted-format metadata line."""
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
    """Append one metadata-only commit on the canonical empty journal tree."""
    ref = _journal_ref(remote, branch)
    parent = _require_empty_journal_tree(root, ref)
    args = ["commit-tree", _empty_tree(root), "-p", parent, "-m", subject]
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


def _restart_subject(run_id: str) -> str:
    """Return the opaque lookup subject for bounded normal restart prerequisites."""
    return f"{_RESTART_PREFIX} {_run_hash(run_id)}"


def _state_key() -> bytes:
    """Derive one fixed AES-256 key from the environment-scoped canary state secret."""
    raw = os.getenv("LEADS_GIT_JOURNAL_KEY")
    if raw is None:
        raise RuntimeError("LEADS_GIT_JOURNAL_KEY is required for canary restart state")
    encoded = raw.encode("utf-8")
    if len(encoded) < 32:
        raise ValueError("LEADS_GIT_JOURNAL_KEY must contain at least 32 UTF-8 bytes")
    return hashlib.sha256(encoded).digest()


def _state_aad(prefix: str, run_id: str) -> bytes:
    """Bind an encrypted capsule to its exact logical kind and run identity."""
    return f"{prefix}\0{run_id}".encode()


def _encode_state(prefix: str, run_id: str, payload: dict[str, Any]) -> str:
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
    ciphertext = AESGCM(_state_key()).encrypt(
        nonce,
        plaintext,
        _state_aad(prefix, run_id),
    )
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def _decode_state(prefix: str, run_id: str, encoded_text: str) -> dict[str, Any]:
    try:
        encoded = encoded_text.encode("ascii")
        encrypted = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(encrypted) <= _STATE_NONCE_BYTES + 16:
            raise ValueError("encrypted capsule is too short")
        plaintext = AESGCM(_state_key()).decrypt(
            encrypted[:_STATE_NONCE_BYTES],
            encrypted[_STATE_NONCE_BYTES:],
            _state_aad(prefix, run_id),
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


def _load_capsule(run_id: str, *, prefix: str, subject: str) -> dict[str, Any] | None:
    config = _configured()
    if config is None:
        return None
    root, remote, branch = config
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("canary state run_id is invalid for Git operation journal")
    ref = _journal_ref(remote, branch)
    _require_empty_journal_tree(root, ref)
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
    capsule_lines = [
        line[len(_CAPSULE_PREFIX) :] if line.startswith(_CAPSULE_PREFIX) else line
        for line in body_lines
        if line.startswith(_CAPSULE_PREFIX) or not line.startswith(f"{_PREFIX} ")
    ]
    if len(capsule_lines) != 1:
        raise ValueError("canary private Git journal capsule body is invalid")
    return _decode_state(prefix, run_id, capsule_lines[0])


def persist_canary_private_state(run_id: str, payload: dict[str, Any]) -> None:
    """Encrypt and durably mirror the authoritative canary-private replay state."""
    config = _configured()
    if config is None:
        return
    root, remote, branch = config
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("canary state run_id is invalid for Git operation journal")
    body = _CAPSULE_PREFIX + _encode_state(_STATE_PREFIX, run_id, payload)
    _append_message(root, remote, branch, _state_subject(run_id), body)


def load_canary_private_state(run_id: str) -> dict[str, Any] | None:
    """Decrypt the latest bounded private-state capsule for one canary run, if present."""
    return _load_capsule(
        run_id,
        prefix=_STATE_PREFIX,
        subject=_state_subject(run_id),
    )


def persist_canary_restart_state(run_id: str, payload: dict[str, Any]) -> None:
    """Persist bounded production-derived normal prerequisites for fresh-runner resume."""
    config = _configured()
    if config is None:
        return
    root, remote, branch = config
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("canary state run_id is invalid for Git operation journal")
    body = _CAPSULE_PREFIX + _encode_state(_RESTART_PREFIX, run_id, payload)
    _append_message(root, remote, branch, _restart_subject(run_id), body)


def load_canary_restart_state(run_id: str) -> dict[str, Any] | None:
    """Load bounded production-derived normal prerequisites for fresh-runner resume."""
    return _load_capsule(
        run_id,
        prefix=_RESTART_PREFIX,
        subject=_restart_subject(run_id),
    )


def _planned_barriers(
    root: Path,
    ref: str,
    checkpoint: RunCheckpoint,
    previous: RunCheckpoint | None,
) -> list[str]:
    """Validate one checkpoint transition and return metadata lines that must become durable."""
    current_raw = checkpoint.provider_state.get("operations", {})
    if not isinstance(current_raw, dict):
        raise ValueError("checkpoint operations must be an object")
    prior = _previous_operations(previous)
    barriers: list[str] = []
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
        barriers.append(desired)
    return barriers


def _looks_like_canary_private(checkpoint: RunCheckpoint) -> bool:
    raw = checkpoint.provider_state.get("operations", {})
    if not isinstance(raw, dict) or not raw:
        return False
    return all(
        isinstance(value, dict)
        and "dispatch_id" in value
        and "input_fingerprint" in value
        and "dispatch_usage_recorded" in value
        for value in raw.values()
    )


def _atomic_private_state(
    checkpoint: RunCheckpoint,
    previous: RunCheckpoint | None,
) -> dict[str, Any] | None:
    """Compose a checkpoint transition with the last remotely durable authoritative usage."""
    if not _looks_like_canary_private(checkpoint):
        return None
    remote = load_canary_private_state(checkpoint.run_id)
    usage_events: list[dict[str, Any]] = []
    if remote is not None:
        raw_usage = remote.get("usage_events")
        if not isinstance(raw_usage, list) or any(not isinstance(row, dict) for row in raw_usage):
            raise ValueError("canary private Git journal usage state is invalid")
        usage_events = raw_usage
    elif previous is not None and _looks_like_canary_private(previous):
        raise RuntimeError("canary private local state lacks a durable Git journal capsule")
    return {"checkpoint": checkpoint.to_dict(), "usage_events": usage_events}


def sync_checkpoint_barrier(
    checkpoint: RunCheckpoint,
    previous: RunCheckpoint | None,
) -> None:
    """Durably mirror paid-operation transitions before local checkpoint replacement.

    For canary-private transitions, the coarse barrier and encrypted checkpoint identity become
    remotely visible in the same Git commit/ref update. The following local write may therefore
    be lost without losing the pending provider identity required for safe resume.
    """
    config = _configured()
    if config is None:
        return
    root, remote, branch = config
    if not _RUN_ID.fullmatch(checkpoint.run_id):
        raise ValueError("checkpoint run_id is invalid for Git operation journal")
    ref = _journal_ref(remote, branch)
    _require_empty_journal_tree(root, ref)
    barriers = _planned_barriers(root, ref, checkpoint, previous)
    private_state = _atomic_private_state(checkpoint, previous)
    if private_state is not None:
        body_lines = [*barriers, _CAPSULE_PREFIX + _encode_state(_STATE_PREFIX, checkpoint.run_id, private_state)]
        _append_message(
            root,
            remote,
            branch,
            _state_subject(checkpoint.run_id),
            "\n".join(body_lines),
        )
        return
    for desired in barriers:
        _append_subject(root, remote, branch, desired)
