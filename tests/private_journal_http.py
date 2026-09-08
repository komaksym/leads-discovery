"""In-memory HTTP double for GitHub draft-release journal tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import AbstractContextManager
from typing import Any

import httpx
import pytest

_TEST_KEY = "test-canary-private-journal-key-32-bytes-minimum"
_API_URL = "https://api.github.test"
_REAL_HTTPX_CLIENT = httpx.Client


class DraftReleaseJournalServer(AbstractContextManager["DraftReleaseJournalServer"]):
    """One in-memory durable GitHub API double shared across fresh runner roots."""

    def __init__(self) -> None:
        self._releases: list[dict[str, Any]] = []
        self._assets: dict[int, tuple[int, str, bytes]] = {}
        self._next_release_id = 1
        self._next_asset_id = 1
        self._transport = httpx.MockTransport(self._handle)

    def __enter__(self) -> DraftReleaseJournalServer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        return None

    @property
    def url(self) -> str:
        return _API_URL

    @property
    def releases(self) -> list[dict[str, Any]]:
        return self._releases

    @property
    def assets(self) -> dict[int, tuple[str, bytes]]:
        return {
            asset_id: (name, data)
            for asset_id, (_release_id, name, data) in self._assets.items()
        }

    def _release_payload(self, release: dict[str, Any]) -> dict[str, Any]:
        release_id = int(release["id"])
        payload = dict(release)
        payload["upload_url"] = f"{self.url}/uploads/{release_id}/assets{{?name,label}}"
        payload["assets_url"] = f"{self.url}/repos/acme/leads/releases/{release_id}/assets"
        return payload

    def _asset_payload(self, asset_id: int, name: str, data: bytes) -> dict[str, Any]:
        return {
            "id": asset_id,
            "name": name,
            "size": len(data),
            "state": "uploaded",
            "url": f"{self.url}/repos/acme/leads/releases/assets/{asset_id}",
        }

    def _json_response(
        self,
        request: httpx.Request,
        status: int,
        payload: object,
    ) -> httpx.Response:
        return httpx.Response(status, json=payload, request=request)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.github.test":
            raise AssertionError("private journal test transport rejected an unexpected host")

        path = request.url.path
        if request.method == "GET" and path == "/repos/acme/leads/releases":
            page = int(request.url.params.get("page", "1"))
            per_page = int(request.url.params.get("per_page", "30"))
            start = (page - 1) * per_page
            selected = self._releases[start : start + per_page]
            return self._json_response(
                request,
                200,
                [self._release_payload(item) for item in selected],
            )

        release_prefix = "/repos/acme/leads/releases/"
        asset_suffix = "/assets"
        if (
            request.method == "GET"
            and path.startswith(release_prefix)
            and path.endswith(asset_suffix)
        ):
            middle = path[len(release_prefix) : -len(asset_suffix)]
            if middle.isdigit():
                release_id = int(middle)
                rows = [
                    self._asset_payload(asset_id, name, data)
                    for asset_id, (owner_release_id, name, data) in sorted(self._assets.items())
                    if owner_release_id == release_id
                ]
                page = int(request.url.params.get("page", "1"))
                per_page = int(request.url.params.get("per_page", "30"))
                start = (page - 1) * per_page
                return self._json_response(request, 200, rows[start : start + per_page])

        asset_prefix = "/repos/acme/leads/releases/assets/"
        if request.method == "GET" and path.startswith(asset_prefix):
            raw_id = path[len(asset_prefix) :]
            if raw_id.isdigit():
                stored = self._assets.get(int(raw_id))
                if stored is not None:
                    _release_id, _name, data = stored
                    return httpx.Response(
                        200,
                        content=data,
                        headers={"content-type": "application/octet-stream"},
                        request=request,
                    )

        if request.method == "POST" and path == "/repos/acme/leads/releases":
            payload = json.loads(request.content.decode("utf-8"))
            if not isinstance(payload, dict):
                return self._json_response(request, 422, {"message": "invalid"})
            release = {
                "id": self._next_release_id,
                "tag_name": payload.get("tag_name"),
                "draft": payload.get("draft"),
                "name": payload.get("name"),
            }
            self._next_release_id += 1
            self._releases.append(release)
            return self._json_response(request, 201, self._release_payload(release))

        upload_prefix = "/uploads/"
        upload_suffix = "/assets"
        if (
            request.method == "POST"
            and path.startswith(upload_prefix)
            and path.endswith(upload_suffix)
        ):
            middle = path[len(upload_prefix) : -len(upload_suffix)]
            name = request.url.params.get("name")
            if not middle.isdigit() or name is None:
                return self._json_response(request, 422, {"message": "invalid upload"})
            release_id = int(middle)
            if not any(int(release["id"]) == release_id for release in self._releases):
                return self._json_response(request, 404, {"message": "release not found"})
            asset_id = self._next_asset_id
            self._next_asset_id += 1
            data = request.content
            self._assets[asset_id] = (release_id, name, data)
            return self._json_response(
                request,
                201,
                self._asset_payload(asset_id, name, data),
            )

        return self._json_response(request, 404, {"message": "not found"})

    def configure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def client_factory(*args: Any, **kwargs: Any) -> httpx.Client:
            kwargs["transport"] = self._transport
            return _REAL_HTTPX_CLIENT(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", client_factory)
        monkeypatch.setenv("GITHUB_API_URL", self.url)
        monkeypatch.setenv("GITHUB_REPOSITORY", "acme/leads")
        monkeypatch.setenv("LEADS_PRIVATE_JOURNAL_TOKEN", "test-token")
        monkeypatch.setenv("LEADS_PRIVATE_JOURNAL_KEY", _TEST_KEY)
        monkeypatch.delenv("LEADS_GIT_JOURNAL_BRANCH", raising=False)
        monkeypatch.delenv("LEADS_GIT_JOURNAL_REMOTE", raising=False)
        monkeypatch.delenv("LEADS_GIT_JOURNAL_KEY", raising=False)


@pytest.fixture
def draft_release_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[DraftReleaseJournalServer]:
    with DraftReleaseJournalServer() as server:
        server.configure(monkeypatch)
        yield server
