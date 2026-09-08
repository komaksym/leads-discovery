"""Private durable canary journal backed by encrypted unpublished draft-release assets."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Final, Literal, cast
from urllib.parse import urlparse

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from leads_discovery.models import RunCheckpoint

JournalKind = Literal["transition", "restart"]

_RUN_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REPO: Final = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PREFIX: Final = "leads-canary-private-v2"
_MAX_STATE: Final = 256 * 1024
_MAX_ASSETS: Final = 96
_MAX_RELEASE_PAGES: Final = 10
_MAX_API_BYTES: Final = 1024 * 1024
_NONCE_BYTES: Final = 12
_TAG_BYTES: Final = 16
_MAX_ASSET_BYTES: Final = _MAX_STATE + _NONCE_BYTES + _TAG_BYTES
_TIMEOUT: Final = 15.0
_ASSET_NAME: Final = re.compile(
    rf"^{re.escape(_PREFIX)}-(?P<revision>[0-9]{{3}})-(?P<digest>[0-9a-f]{{32}})\.bin$"
)


@dataclass(frozen=True, slots=True)
class _Config:
    token: str
    repository: str
    api_url: str
    key: bytes


@dataclass(frozen=True, slots=True)
class _JournalHead:
    revision: int
    transition_revision: int | None
    restart_revision: int | None

    def latest(self, kind: JournalKind) -> int | None:
        return (
            self.transition_revision
            if kind == "transition"
            else self.restart_revision
        )

    def advance(self, kind: JournalKind) -> _JournalHead:
        revision = self.revision + 1
        if revision > _MAX_ASSETS:
            raise RuntimeError("canary private journal asset bound reached")
        return _JournalHead(
            revision=revision,
            transition_revision=(
                revision if kind == "transition" else self.transition_revision
            ),
            restart_revision=revision if kind == "restart" else self.restart_revision,
        )


_EMPTY_HEAD: Final = _JournalHead(0, None, None)


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


def _headers(config: _Config, *, binary: bool = False) -> dict[str, str]:
    return {
        "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
        "Authorization": f"Bearer {config.token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _request(
    config: _Config,
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    content: bytes | None = None,
    binary: bool = False,
) -> httpx.Response:
    """Send one bounded GitHub request without buffering an untrusted response first."""
    headers = _headers(config, binary=binary)
    if content is not None:
        headers["Content-Type"] = "application/octet-stream"
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client, client.stream(
            method,
            url,
            headers=headers,
            json=body,
            content=content,
        ) as response:
            raw_length = response.headers.get("content-length")
            if raw_length is not None:
                try:
                    declared = int(raw_length)
                except ValueError as exc:
                    raise RuntimeError(
                        "canary private journal API Content-Length is invalid"
                    ) from exc
                if declared < 0 or declared > _MAX_API_BYTES:
                    raise RuntimeError(
                        "canary private journal API response exceeds its fixed bound"
                    )
            data = bytearray()
            for chunk in response.iter_bytes():
                if len(data) + len(chunk) > _MAX_API_BYTES:
                    raise RuntimeError(
                        "canary private journal API response exceeds its fixed bound"
                    )
                data.extend(chunk)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=bytes(data),
                request=response.request,
            )
    except httpx.HTTPError as exc:
        raise RuntimeError("canary private journal request failed") from exc


def _download_bytes(config: _Config, url: str) -> bytes:
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client, client.stream(
            "GET", url, headers=_headers(config, binary=True)
        ) as response:
            if response.status_code != 200:
                raise RuntimeError("canary private journal asset download failed")
            raw_length = response.headers.get("content-length")
            if raw_length is not None:
                try:
                    declared = int(raw_length)
                except ValueError as exc:
                    raise RuntimeError(
                        "canary private journal asset Content-Length is invalid"
                    ) from exc
                if declared < 0 or declared > _MAX_ASSET_BYTES:
                    raise RuntimeError(
                        "canary private journal asset size exceeds its fixed bound"
                    )
            data = bytearray()
            for chunk in response.iter_bytes():
                if len(data) + len(chunk) > _MAX_ASSET_BYTES:
                    raise RuntimeError(
                        "canary private journal asset size exceeds its fixed bound"
                    )
                data.extend(chunk)
    except httpx.HTTPError as exc:
        raise RuntimeError("canary private journal request failed") from exc
    if len(data) <= _NONCE_BYTES + _TAG_BYTES:
        raise ValueError("canary private journal asset size is invalid")
    return bytes(data)


def _json(response: httpx.Response) -> Any:
    if len(response.content) > _MAX_API_BYTES:
        raise RuntimeError("canary private journal API response exceeds its fixed bound")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("canary private journal API returned invalid JSON") from exc


def _tag(config: _Config, run_id: str) -> str:
    """Return a stable release identity that does not change when encryption keys rotate."""
    del config
    digest = hashlib.sha256(f"{_PREFIX}\0release\0{run_id}".encode()).hexdigest()[:32]
    return f"{_PREFIX}-{digest}"


def _head_payload(head: _JournalHead) -> dict[str, Any]:
    return {
        "version": 2,
        "revision": head.revision,
        "transition_revision": head.transition_revision,
        "restart_revision": head.restart_revision,
    }


def _head_mac(config: _Config, run_id: str, head: _JournalHead) -> str:
    encoded = json.dumps(
        _head_payload(head),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    return hmac.new(
        config.key,
        f"{_PREFIX}\0head\0{run_id}\0".encode() + encoded,
        hashlib.sha256,
    ).hexdigest()


def _head_body(config: _Config, run_id: str, head: _JournalHead) -> str:
    payload = _head_payload(head)
    payload["mac"] = _head_mac(config, run_id, head)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _parse_head(config: _Config, run_id: str, body: object) -> _JournalHead:
    if not isinstance(body, str):
        raise RuntimeError("canary private journal head is invalid")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("canary private journal head is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "revision",
        "transition_revision",
        "restart_revision",
        "mac",
    }:
        raise RuntimeError("canary private journal head is invalid")
    revision = payload.get("revision")
    transition_revision = payload.get("transition_revision")
    restart_revision = payload.get("restart_revision")
    mac = payload.get("mac")
    if (
        payload.get("version") != 2
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or revision > _MAX_ASSETS
        or (
            transition_revision is not None
            and (
                isinstance(transition_revision, bool)
                or not isinstance(transition_revision, int)
                or transition_revision < 1
                or transition_revision > revision
            )
        )
        or (
            restart_revision is not None
            and (
                isinstance(restart_revision, bool)
                or not isinstance(restart_revision, int)
                or restart_revision < 1
                or restart_revision > revision
            )
        )
        or not isinstance(mac, str)
    ):
        raise RuntimeError("canary private journal head is invalid")
    if revision == 0 and (transition_revision is not None or restart_revision is not None):
        raise RuntimeError("canary private journal head is invalid")
    if revision > 0 and transition_revision is None and restart_revision is None:
        raise RuntimeError("canary private journal head is invalid")
    head = _JournalHead(revision, transition_revision, restart_revision)
    if not hmac.compare_digest(mac, _head_mac(config, run_id, head)):
        raise RuntimeError("canary private journal head authentication failed")
    return head


def _validate_release(
    raw: Any,
    tag: str,
    *,
    config: _Config,
    run_id: str,
) -> tuple[dict[str, Any], _JournalHead]:
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
    head = _parse_head(config, run_id, raw.get("body"))
    return cast(dict[str, Any], raw), head


def _release(
    config: _Config, run_id: str, *, create: bool
) -> tuple[dict[str, Any], _JournalHead] | None:
    tag = _tag(config, run_id)
    base = f"{config.api_url}/repos/{config.repository}/releases"
    matches: list[dict[str, Any]] = []
    for page in range(1, _MAX_RELEASE_PAGES + 1):
        response = _request(config, "GET", f"{base}?per_page=100&page={page}")
        if response.status_code != 200:
            raise RuntimeError("canary private journal release lookup failed")
        payload = _json(response)
        if not isinstance(payload, list):
            raise RuntimeError("canary private journal release listing is invalid")
        for raw in payload:
            if isinstance(raw, dict) and raw.get("tag_name") == tag:
                matches.append(cast(dict[str, Any], raw))
        if len(payload) < 100:
            break
    else:
        raise RuntimeError("canary private journal release replay bound exceeded")
    if len(matches) > 1:
        raise RuntimeError("canary private journal contains conflicting releases")
    if matches:
        return _validate_release(matches[0], tag, config=config, run_id=run_id)
    if not create:
        return None

    response = _request(
        config,
        "POST",
        base,
        body={
            "tag_name": tag,
            "name": "Private production canary restart journal",
            "body": _head_body(config, run_id, _EMPTY_HEAD),
            "draft": True,
            "prerelease": False,
        },
    )
    if response.status_code != 201:
        if response.status_code == 422:
            raced = _release(config, run_id, create=False)
            if raced is not None:
                return raced
        raise RuntimeError("canary private journal draft release creation failed")
    created, head = _validate_release(
        _json(response), tag, config=config, run_id=run_id
    )
    confirmed = _release(config, run_id, create=False)
    if confirmed is None or confirmed[0]["id"] != created["id"] or confirmed[1] != head:
        raise RuntimeError("canary private journal release creation was not stable")
    return confirmed


def _asset_name(config: _Config, run_id: str, revision: int) -> str:
    if revision < 1 or revision > _MAX_ASSETS:
        raise ValueError("canary private journal revision is outside its fixed bound")
    material = f"{_PREFIX}\0asset\0{run_id}\0{revision}".encode()
    digest = hmac.new(config.key, material, hashlib.sha256).hexdigest()[:32]
    return f"{_PREFIX}-{revision:03d}-{digest}.bin"


def _assets(
    config: _Config,
    release: dict[str, Any],
    head: _JournalHead,
    run_id: str,
) -> dict[int, dict[str, Any]]:
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
    if len(payload) > _MAX_ASSETS:
        raise RuntimeError("canary private journal asset bound exceeded")

    selected: dict[int, dict[str, Any]] = {}
    for raw in payload:
        if not isinstance(raw, dict):
            raise RuntimeError("canary private journal asset entry is invalid")
        asset_id, name, size, state = (
            raw.get("id"),
            raw.get("name"),
            raw.get("size"),
            raw.get("state"),
        )
        if (
            isinstance(asset_id, bool)
            or not isinstance(asset_id, int)
            or asset_id <= 0
            or not isinstance(name, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or state != "uploaded"
        ):
            raise RuntimeError("canary private journal asset entry is invalid")
        match = _ASSET_NAME.fullmatch(name)
        if match is None:
            raise RuntimeError("canary private journal asset name is invalid")
        revision = int(match.group("revision"))
        if revision < 1 or revision > _MAX_ASSETS:
            raise RuntimeError("canary private journal asset revision is invalid")
        if name != _asset_name(config, run_id, revision):
            raise RuntimeError("canary private journal asset name is invalid")
        if size > _MAX_ASSET_BYTES:
            raise RuntimeError("canary private journal asset size exceeds its fixed bound")
        if size <= _NONCE_BYTES + _TAG_BYTES:
            raise RuntimeError("canary private journal asset size is invalid")
        if revision in selected:
            raise RuntimeError("canary private journal contains duplicate revisions")
        selected[revision] = cast(dict[str, Any], raw)

    revisions = sorted(selected)
    expected = list(range(1, head.revision + 1))
    if revisions != expected:
        raise RuntimeError("canary private journal head disagrees with durable assets")
    return selected


def _plain(
    kind: JournalKind,
    run_id: str,
    state: dict[str, Any],
    revision: int,
) -> bytes:
    data = json.dumps(
        {
            "version": 2,
            "revision": revision,
            "kind": kind,
            "run_id": run_id,
            "state": state,
        },
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    if len(data) > _MAX_STATE:
        raise ValueError("canary private restart state exceeds its fixed bound")
    return data


def _aad(run_id: str, revision: int) -> bytes:
    return f"{_PREFIX}\0{run_id}\0{revision}".encode()


def _encrypt(
    config: _Config,
    kind: JournalKind,
    run_id: str,
    state: dict[str, Any],
    revision: int,
) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(config.key).encrypt(
        nonce,
        _plain(kind, run_id, state, revision),
        _aad(run_id, revision),
    )


def _download(
    config: _Config,
    asset: dict[str, Any],
    *,
    run_id: str,
    revision: int,
) -> tuple[JournalKind, dict[str, Any]]:
    asset_id = cast(int, asset["id"])
    data = _download_bytes(
        config,
        f"{config.api_url}/repos/{config.repository}/releases/assets/{asset_id}",
    )
    if len(data) != asset["size"]:
        raise RuntimeError("canary private journal asset size disagrees with metadata")
    try:
        plain = AESGCM(config.key).decrypt(
            data[:_NONCE_BYTES],
            data[_NONCE_BYTES:],
            _aad(run_id, revision),
        )
        envelope = json.loads(plain.decode())
    except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("canary private journal state is invalid") from exc
    kind = envelope.get("kind") if isinstance(envelope, dict) else None
    if (
        not isinstance(envelope, dict)
        or envelope.get("version") != 2
        or envelope.get("revision") != revision
        or kind not in {"transition", "restart"}
        or envelope.get("run_id") != run_id
        or not isinstance(envelope.get("state"), dict)
    ):
        raise ValueError("canary private journal state envelope is invalid")
    return cast(JournalKind, kind), cast(dict[str, Any], envelope["state"])


def _load_with_head(
    kind: JournalKind,
    run_id: str,
) -> tuple[dict[str, Any] | None, _JournalHead]:
    config = _config()
    if config is None:
        return None, _EMPTY_HEAD
    if _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("canary private journal state identity is invalid")
    found = _release(config, run_id, create=False)
    if found is None:
        return None, _EMPTY_HEAD
    release, head = found
    assets = _assets(config, release, head, run_id)
    revision = head.latest(kind)
    if revision is None:
        return None, head
    stored_kind, state = _download(
        config,
        assets[revision],
        run_id=run_id,
        revision=revision,
    )
    if stored_kind != kind:
        raise RuntimeError("canary private journal head points to the wrong state kind")
    return state, head


def _commit_head(
    config: _Config,
    release: dict[str, Any],
    run_id: str,
    head: _JournalHead,
) -> None:
    release_id = cast(int, release["id"])
    response = _request(
        config,
        "PATCH",
        f"{config.api_url}/repos/{config.repository}/releases/{release_id}",
        body={"body": _head_body(config, run_id, head)},
    )
    if response.status_code != 200:
        raise RuntimeError("canary private journal head update failed")
    updated, updated_head = _validate_release(
        _json(response),
        _tag(config, run_id),
        config=config,
        run_id=run_id,
    )
    if updated["id"] != release_id or updated_head != head:
        raise RuntimeError("canary private journal head update is invalid")
    confirmed = _release(config, run_id, create=False)
    if confirmed is None or confirmed[0]["id"] != release_id or confirmed[1] != head:
        raise RuntimeError("canary private journal head update was not durable")


def _persist(
    kind: JournalKind,
    run_id: str,
    state: dict[str, Any],
    *,
    expected_head: _JournalHead,
) -> None:
    config = _config()
    if config is None:
        return
    if _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("canary private journal state identity is invalid")
    found = _release(config, run_id, create=True)
    assert found is not None
    release, head = found
    assets = _assets(config, release, head, run_id)
    if head != expected_head:
        raise RuntimeError("canary private journal changed concurrently")
    latest_revision = head.latest(kind)
    if latest_revision is not None:
        stored_kind, durable = _download(
            config,
            assets[latest_revision],
            run_id=run_id,
            revision=latest_revision,
        )
        if stored_kind != kind:
            raise RuntimeError("canary private journal head points to the wrong state kind")
        if durable == state:
            return

    next_head = head.advance(kind)
    revision = next_head.revision
    if len(assets) >= _MAX_ASSETS:
        raise RuntimeError("canary private journal asset bound reached")
    name = _asset_name(config, run_id, revision)
    encrypted = _encrypt(config, kind, run_id, state, revision)
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
        config,
        "POST",
        f"{upload_url}?name={name}",
        content=encrypted,
    )
    if response.status_code == 422:
        raise RuntimeError("canary private journal concurrent revision conflict")
    if response.status_code != 201:
        raise RuntimeError("canary private journal asset upload failed")
    uploaded = _json(response)
    if not isinstance(uploaded, dict):
        raise RuntimeError("canary private journal upload response is invalid")
    asset_id, uploaded_name, size, upload_state = (
        uploaded.get("id"),
        uploaded.get("name"),
        uploaded.get("size"),
        uploaded.get("state"),
    )
    if (
        isinstance(asset_id, bool)
        or not isinstance(asset_id, int)
        or asset_id <= 0
        or uploaded_name != name
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size != len(encrypted)
        or upload_state != "uploaded"
    ):
        raise RuntimeError("canary private journal upload response is invalid")
    uploaded_asset = {
        "id": asset_id,
        "name": name,
        "size": size,
        "state": "uploaded",
    }
    uploaded_kind, uploaded_state = _download(
        config,
        uploaded_asset,
        run_id=run_id,
        revision=revision,
    )
    if uploaded_kind != kind or uploaded_state != state:
        raise RuntimeError("canary private journal uploaded state disagrees")
    _commit_head(config, release, run_id, next_head)
    committed = _assets(config, release, next_head, run_id)
    if revision not in committed:
        raise RuntimeError("canary private journal committed asset is missing")


def _transition_with_head(run_id: str) -> tuple[dict[str, Any], _JournalHead]:
    state, head = _load_with_head("transition", run_id)
    if state is None:
        return {"barriers": {}, "private_state": None}, head
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
    }, head


def _transition(run_id: str) -> dict[str, Any]:
    return _transition_with_head(run_id)[0]


def load_canary_private_state(run_id: str) -> dict[str, Any] | None:
    """Load the latest private canary checkpoint/usage authority."""
    value = _transition(run_id)["private_state"]
    return None if value is None else cast(dict[str, Any], value)


def persist_canary_private_state(run_id: str, payload: dict[str, Any]) -> None:
    """Persist private canary checkpoint/usage authority without changing barriers."""
    if not git_journal_configured():
        return
    transition, head = _transition_with_head(run_id)
    transition["private_state"] = payload
    _persist("transition", run_id, transition, expected_head=head)


def persist_canary_restart_state(run_id: str, payload: dict[str, Any]) -> None:
    """Persist completed normal prerequisites in private durable storage."""
    if not git_journal_configured():
        return
    _state, head = _load_with_head("restart", run_id)
    _persist("restart", run_id, payload, expected_head=head)


def load_canary_restart_state(run_id: str) -> dict[str, Any] | None:
    """Load completed normal prerequisites from private durable storage."""
    return _load_with_head("restart", run_id)[0]


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
    checkpoint: RunCheckpoint,
    previous: RunCheckpoint | None,
    remote: dict[str, Any] | None,
    private_usage: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    if not _is_private(checkpoint):
        return None
    usage: list[dict[str, Any]] = []
    if private_usage is not None:
        if any(not isinstance(row, dict) for row in private_usage):
            raise ValueError("canary private journal usage state is invalid")
        usage = [dict(row) for row in private_usage]
    elif remote is not None:
        raw = remote.get("usage_events")
        if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
            raise ValueError("canary private journal usage state is invalid")
        usage = [cast(dict[str, Any], row) for row in raw]
    elif previous is not None and _is_private(previous):
        raise RuntimeError("canary private local state lacks durable private authority")
    return {"checkpoint": checkpoint.to_dict(), "usage_events": usage}


def sync_checkpoint_barrier(
    checkpoint: RunCheckpoint,
    previous: RunCheckpoint | None,
    *,
    private_usage: list[dict[str, Any]] | None = None,
) -> None:
    """Persist encrypted remote intent and its usage snapshot before local replacement."""
    if not git_journal_configured():
        return
    if _RUN_ID.fullmatch(checkpoint.run_id) is None:
        raise ValueError("checkpoint run_id is invalid for canary private journal")
    current = _operations(checkpoint)
    if not current:
        return
    transition, head = _transition_with_head(checkpoint.run_id)
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
    remote_private = cast(dict[str, Any] | None, transition["private_state"])
    private = _private_snapshot(
        checkpoint,
        previous,
        remote_private,
        private_usage,
    )
    if private is not None:
        transition["private_state"] = private
        changed = True
    if changed:
        _persist("transition", checkpoint.run_id, transition, expected_head=head)


__all__ = [
    "git_journal_configured",
    "load_canary_private_state",
    "load_canary_restart_state",
    "persist_canary_private_state",
    "persist_canary_restart_state",
    "sync_checkpoint_barrier",
]
