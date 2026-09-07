"""Private durable canary journal backed by encrypted unpublished draft-release assets."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Final, cast
from urllib.parse import urlparse

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from leads_discovery.models import RunCheckpoint

_RUN_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REPO: Final = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PREFIX: Final = "leads-canary-private-v1"
_MAX_STATE: Final = 256 * 1024
_MAX_ASSETS: Final = 96
_MAX_RELEASE_PAGES: Final = 10
_MAX_API_BYTES: Final = 1024 * 1024
_NONCE_BYTES: Final = 12
_TIMEOUT: Final = 15.0


@dataclass(frozen=True, slots=True)
class _Config:
    token: str
    repository: str
    api_url: str
    key: bytes


def _config() -> _Config | None:
    token = os.getenv("LEADS_PRIVATE_JOURNAL_TOKEN")
    if token is None:
        return None
    if not token.strip():
        raise ValueError("LEADS_PRIVATE_JOURNAL_TOKEN must be nonempty")
    repository = os.getenv("GITHUB_REPOSITORY")
    if repository is None or _REPO.fullmatch(repository) is None:
        raise ValueError("GITHUB_REPOSITORY is invalid for canary private journal")
    api_url = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    parsed = urlparse(api_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("GITHUB_API_URL is invalid for canary private journal")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("GITHUB_API_URL must use HTTPS outside loopback tests")
    raw_key = os.getenv("LEADS_PRIVATE_JOURNAL_KEY")
    if raw_key is None:
        raise RuntimeError("LEADS_PRIVATE_JOURNAL_KEY is required for canary restart state")
    encoded = raw_key.encode()
    if len(encoded) < 32:
        raise ValueError("LEADS_PRIVATE_JOURNAL_KEY must contain at least 32 UTF-8 bytes")
    return _Config(token, repository, api_url, hashlib.sha256(encoded).digest())


def git_journal_configured() -> bool:
    """Compatibility name: return whether the private durable journal is configured."""
    return _config() is not None


def _request(
    config: _Config,
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    content: bytes | None = None,
    binary: bool = False,
) -> httpx.Response:
    headers = {
        "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
        "Authorization": f"Bearer {config.token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if content is not None:
        headers["Content-Type"] = "application/octet-stream"
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            return client.request(method, url, headers=headers, json=body, content=content)
    except httpx.HTTPError as exc:
        raise RuntimeError("canary private journal request failed") from exc


def _json(response: httpx.Response) -> Any:
    if len(response.content) > _MAX_API_BYTES:
        raise RuntimeError("canary private journal API response exceeds its fixed bound")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("canary private journal API returned invalid JSON") from exc


def _tag(config: _Config, run_id: str) -> str:
    digest = hmac.new(config.key, run_id.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{_PREFIX}-{digest}"


def _validate_release(raw: Any, tag: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("tag_name") != tag:
        raise RuntimeError("canary private journal release is invalid")
    if raw.get("draft") is not True:
        raise RuntimeError("canary private journal release must remain an unpublished draft")
    release_id = raw.get("id")
    upload_url = raw.get("upload_url")
    if (
        isinstance(release_id, bool)
        or not isinstance(release_id, int)
        or release_id <= 0
        or not isinstance(upload_url, str)
        or not upload_url
    ):
        raise RuntimeError("canary private journal release is invalid")
    return cast(dict[str, Any], raw)


def _release(config: _Config, run_id: str, *, create: bool) -> dict[str, Any] | None:
    tag = _tag(config, run_id)
    base = f"{config.api_url}/repos/{config.repository}/releases"
    for page in range(1, _MAX_RELEASE_PAGES + 1):
        response = _request(config, "GET", f"{base}?per_page=100&page={page}")
        if response.status_code != 200:
            raise RuntimeError("canary private journal release lookup failed")
        payload = _json(response)
        if not isinstance(payload, list):
            raise RuntimeError("canary private journal release listing is invalid")
        for raw in payload:
            if isinstance(raw, dict) and raw.get("tag_name") == tag:
                return _validate_release(raw, tag)
        if len(payload) < 100:
            break
    else:
        raise RuntimeError("canary private journal release replay bound exceeded")
    if not create:
        return None
    response = _request(
        config,
        "POST",
        base,
        body={
            "tag_name": tag,
            "name": "Private production canary restart journal",
            "body": "Encrypted private canary state. This draft must never be published.",
            "draft": True,
            "prerelease": False,
        },
    )
    if response.status_code != 201:
        raise RuntimeError("canary private journal draft release creation failed")
    return _validate_release(_json(response), tag)


def _assets(config: _Config, release: dict[str, Any]) -> list[dict[str, Any]]:
    release_id = cast(int, release["id"])
    url = (
        f"{config.api_url}/repos/{config.repository}/releases/"
        f"{release_id}/assets?per_page=100&page=1"
    )
    response = _request(config, "GET", url)
    if response.status_code != 200:
        raise RuntimeError("canary private journal asset listing failed")
    payload = _json(response)
    if not isinstance(payload, list):
        raise RuntimeError("canary private journal asset listing is invalid")
    assets: list[dict[str, Any]] = []
    for raw in payload:
        if not isinstance(raw, dict):
            raise RuntimeError("canary private journal asset entry is invalid")
        asset_id, name, size = raw.get("id"), raw.get("name"), raw.get("size")
        if (
            isinstance(asset_id, bool)
            or not isinstance(asset_id, int)
            or asset_id <= 0
            or not isinstance(name, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise RuntimeError("canary private journal asset entry is invalid")
        assets.append(cast(dict[str, Any], raw))
    if len(assets) > _MAX_ASSETS:
        raise RuntimeError("canary private journal asset bound exceeded")
    return assets


def _plain(kind: str, run_id: str, state: dict[str, Any]) -> bytes:
    data = json.dumps(
        {"version": 1, "kind": kind, "run_id": run_id, "state": state},
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    if len(data) > _MAX_STATE:
        raise ValueError("canary private restart state exceeds its fixed bound")
    return data


def _aad(kind: str, run_id: str) -> bytes:
    return f"{_PREFIX}\0{kind}\0{run_id}".encode()


def _name(config: _Config, kind: str, run_id: str, state: dict[str, Any]) -> str:
    digest = hmac.new(config.key, _plain(kind, run_id, state), hashlib.sha256).hexdigest()
    return f"{_PREFIX}-{kind}-{digest[:32]}.bin"


def _encrypt(config: _Config, kind: str, run_id: str, state: dict[str, Any]) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(config.key).encrypt(
        nonce, _plain(kind, run_id, state), _aad(kind, run_id)
    )


def _download(
    config: _Config, asset_id: int, *, kind: str, run_id: str
) -> dict[str, Any]:
    response = _request(
        config,
        "GET",
        f"{config.api_url}/repos/{config.repository}/releases/assets/{asset_id}",
        binary=True,
    )
    if response.status_code != 200:
        raise RuntimeError("canary private journal asset download failed")
    data = response.content
    if len(data) > _MAX_STATE + 1024 or len(data) <= _NONCE_BYTES + 16:
        raise ValueError("canary private journal asset size is invalid")
    try:
        plain = AESGCM(config.key).decrypt(
            data[:_NONCE_BYTES], data[_NONCE_BYTES:], _aad(kind, run_id)
        )
        envelope = json.loads(plain.decode())
    except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("canary private journal state is invalid") from exc
    if (
        not isinstance(envelope, dict)
        or envelope.get("version") != 1
        or envelope.get("kind") != kind
        or envelope.get("run_id") != run_id
        or not isinstance(envelope.get("state"), dict)
    ):
        raise ValueError("canary private journal state envelope is invalid")
    return cast(dict[str, Any], envelope["state"])


def _persist(kind: str, run_id: str, state: dict[str, Any]) -> None:
    config = _config()
    if config is None:
        return
    if kind not in {"transition", "restart"} or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("canary private journal state identity is invalid")
    release = _release(config, run_id, create=True)
    assert release is not None
    assets = _assets(config, release)
    name = _name(config, kind, run_id, state)
    matches = [asset for asset in assets if asset["name"] == name]
    if len(matches) > 1:
        raise RuntimeError("canary private journal contains duplicate state assets")
    if matches:
        durable = _download(
            config, cast(int, matches[0]["id"]), kind=kind, run_id=run_id
        )
        if durable != state:
            raise RuntimeError("canary private journal durable state disagrees")
        return
    if len(assets) >= _MAX_ASSETS:
        raise RuntimeError("canary private journal asset bound reached")
    encrypted = _encrypt(config, kind, run_id, state)
    upload_url = cast(str, release["upload_url"]).split("{", 1)[0]
    upload, api = urlparse(upload_url), urlparse(config.api_url)
    valid_host = upload.scheme == api.scheme and upload.netloc == api.netloc
    valid_github_host = (
        api.hostname == "api.github.com"
        and upload.scheme == "https"
        and upload.hostname == "uploads.github.com"
    )
    if not (valid_host or valid_github_host):
        raise RuntimeError("canary private journal upload URL host is invalid")
    response = _request(
        config, "POST", f"{upload_url}?name={name}", content=encrypted
    )
    if response.status_code != 201:
        raise RuntimeError("canary private journal asset upload failed")
    uploaded = _json(response)
    if not isinstance(uploaded, dict):
        raise RuntimeError("canary private journal upload response is invalid")
    asset_id, uploaded_name, size = (
        uploaded.get("id"),
        uploaded.get("name"),
        uploaded.get("size"),
    )
    if (
        isinstance(asset_id, bool)
        or not isinstance(asset_id, int)
        or asset_id <= 0
        or uploaded_name != name
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size != len(encrypted)
    ):
        raise RuntimeError("canary private journal upload response is invalid")
    if _download(config, asset_id, kind=kind, run_id=run_id) != state:
        raise RuntimeError("canary private journal uploaded state disagrees")


def _load(kind: str, run_id: str) -> dict[str, Any] | None:
    config = _config()
    if config is None:
        return None
    if kind not in {"transition", "restart"} or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("canary private journal state identity is invalid")
    release = _release(config, run_id, create=False)
    if release is None:
        return None
    prefix = f"{_PREFIX}-{kind}-"
    candidates = [
        asset
        for asset in _assets(config, release)
        if cast(str, asset["name"]).startswith(prefix)
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda item: cast(int, item["id"]))
    return _download(
        config, cast(int, latest["id"]), kind=kind, run_id=run_id
    )


def _transition(run_id: str) -> dict[str, Any]:
    state = _load("transition", run_id)
    if state is None:
        return {"barriers": {}, "private_state": None}
    if set(state) != {"barriers", "private_state"}:
        raise ValueError("canary private journal transition shape is invalid")
    barriers, private_state = state["barriers"], state["private_state"]
    if not isinstance(barriers, dict) or (
        private_state is not None and not isinstance(private_state, dict)
    ):
        raise ValueError("canary private journal transition is invalid")
    for key, value in barriers.items():
        if (
            not isinstance(key, str)
            or value not in {"in_flight", "completed", "failed", "pending"}
        ):
            raise ValueError("canary private journal barrier entry is invalid")
    return {
        "barriers": cast(dict[str, str], dict(barriers)),
        "private_state": (
            None if private_state is None else cast(dict[str, Any], dict(private_state))
        ),
    }


def load_canary_private_state(run_id: str) -> dict[str, Any] | None:
    """Load the latest private canary checkpoint/usage authority."""
    value = _transition(run_id)["private_state"]
    return None if value is None else cast(dict[str, Any], value)


def persist_canary_private_state(run_id: str, payload: dict[str, Any]) -> None:
    """Persist private canary checkpoint/usage authority without changing barriers."""
    if not git_journal_configured():
        return
    transition = _transition(run_id)
    transition["private_state"] = payload
    _persist("transition", run_id, transition)


def persist_canary_restart_state(run_id: str, payload: dict[str, Any]) -> None:
    """Persist completed normal prerequisites in private durable storage."""
    _persist("restart", run_id, payload)


def load_canary_restart_state(run_id: str) -> dict[str, Any] | None:
    """Load completed normal prerequisites from private durable storage."""
    return _load("restart", run_id)


def _operations(checkpoint: RunCheckpoint | None) -> dict[str, dict[str, Any]]:
    if checkpoint is None:
        return {}
    raw = checkpoint.provider_state.get("operations", {})
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _is_private(checkpoint: RunCheckpoint) -> bool:
    operations = _operations(checkpoint)
    return bool(operations) and all(
        "dispatch_id" in value
        and "input_fingerprint" in value
        and "dispatch_usage_recorded" in value
        for value in operations.values()
    )


def _private_snapshot(
    checkpoint: RunCheckpoint, previous: RunCheckpoint | None
) -> dict[str, Any] | None:
    if not _is_private(checkpoint):
        return None
    remote = load_canary_private_state(checkpoint.run_id)
    usage: list[dict[str, Any]] = []
    if remote is not None:
        raw = remote.get("usage_events")
        if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
            raise ValueError("canary private journal usage state is invalid")
        usage = [cast(dict[str, Any], row) for row in raw]
    elif previous is not None and _is_private(previous):
        raise RuntimeError("canary private local state lacks durable private authority")
    return {"checkpoint": checkpoint.to_dict(), "usage_events": usage}


def sync_checkpoint_barrier(
    checkpoint: RunCheckpoint, previous: RunCheckpoint | None
) -> None:
    """Persist encrypted remote intent before local paid checkpoint replacement."""
    if not git_journal_configured():
        return
    if _RUN_ID.fullmatch(checkpoint.run_id) is None:
        raise ValueError("checkpoint run_id is invalid for canary private journal")
    current = _operations(checkpoint)
    if not current:
        return
    transition = _transition(checkpoint.run_id)
    barriers = cast(dict[str, str], transition["barriers"])
    prior = _operations(previous)
    changed = False
    for operation_id, entry in sorted(current.items()):
        state = entry.get("state")
        if state not in {"in_flight", "completed", "failed", "pending"}:
            raise ValueError("checkpoint operation has an invalid journal state")
        state = cast(str, state)
        op_hash = hashlib.sha256(
            f"{checkpoint.run_id}\0{operation_id}".encode()
        ).hexdigest()[:32]
        remote_state = barriers.get(op_hash)
        old = prior.get(operation_id)
        old_state = old.get("state") if old is not None else None
        if state == "in_flight" and old_state != "in_flight":
            retrying_pending = old_state == "pending" and remote_state == "pending"
            if (old_state == "pending" and not retrying_pending) or (
                old_state != "pending" and remote_state is not None
            ):
                raise RuntimeError(
                    "paid operation already has a durable non-retryable private barrier"
                )
        if (
            state == "in_flight"
            and old_state == "in_flight"
            and remote_state != "in_flight"
        ):
            raise RuntimeError(
                "local in-flight operation disagrees with durable private barrier"
            )
        if remote_state != state:
            barriers[op_hash] = state
            changed = True
    private = _private_snapshot(checkpoint, previous)
    if private is not None:
        transition["private_state"] = private
        changed = True
    if changed:
        _persist("transition", checkpoint.run_id, transition)


__all__ = [
    "git_journal_configured",
    "load_canary_private_state",
    "load_canary_restart_state",
    "persist_canary_private_state",
    "persist_canary_restart_state",
    "sync_checkpoint_barrier",
]
