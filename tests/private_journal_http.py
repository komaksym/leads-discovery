"""Local HTTP double for GitHub draft-release journal tests."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import pytest

_TEST_KEY = "test-canary-private-journal-key-32-bytes-minimum"


class _JournalHTTPServer(ThreadingHTTPServer):
    releases: list[dict[str, Any]]
    assets: dict[int, tuple[str, bytes]]
    next_release_id: int
    next_asset_id: int

    def __init__(self, server_address: tuple[str, int]) -> None:
        super().__init__(server_address, _Handler)
        self.releases = []
        self.assets = {}
        self.next_release_id = 1
        self.next_asset_id = 1

    @property
    def origin(self) -> str:
        host, port = cast(tuple[str, int], self.server_address)
        return f"http://{host}:{port}"

    def release_payload(self, release: dict[str, Any]) -> dict[str, Any]:
        release_id = int(release["id"])
        payload = dict(release)
        payload["upload_url"] = (
            f"{self.origin}/uploads/{release_id}/assets{{?name,label}}"
        )
        payload["assets_url"] = (
            f"{self.origin}/repos/acme/leads/releases/{release_id}/assets"
        )
        return payload

    def asset_payload(self, asset_id: int, name: str, data: bytes) -> dict[str, Any]:
        return {
            "id": asset_id,
            "name": name,
            "size": len(data),
            "state": "uploaded",
            "url": f"{self.origin}/repos/acme/leads/releases/assets/{asset_id}",
        }


class _Handler(BaseHTTPRequestHandler):
    server: _JournalHTTPServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _body(self) -> bytes:
        length = int(self.headers.get("content-length", "0"))
        return self.rfile.read(length)

    def _json(self, status: int, payload: object) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, status: int, data: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", "application/octet-stream")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/repos/acme/leads/releases":
            query = parse_qs(parsed.query)
            page = int(query.get("page", ["1"])[0])
            per_page = int(query.get("per_page", ["30"])[0])
            start = (page - 1) * per_page
            selected = self.server.releases[start : start + per_page]
            self._json(
                200,
                [self.server.release_payload(item) for item in selected],
            )
            return

        prefix = "/repos/acme/leads/releases/"
        suffix = "/assets"
        if parsed.path.startswith(prefix) and parsed.path.endswith(suffix):
            middle = parsed.path[len(prefix) : -len(suffix)]
            if middle.isdigit():
                release_id = int(middle)
                rows = [
                    self.server.asset_payload(asset_id, name, data)
                    for asset_id, (name, data) in sorted(self.server.assets.items())
                    if any(
                        int(release["id"]) == release_id
                        for release in self.server.releases
                    )
                ]
                query = parse_qs(parsed.query)
                page = int(query.get("page", ["1"])[0])
                per_page = int(query.get("per_page", ["30"])[0])
                start = (page - 1) * per_page
                self._json(200, rows[start : start + per_page])
                return

        asset_prefix = "/repos/acme/leads/releases/assets/"
        if parsed.path.startswith(asset_prefix):
            raw_id = parsed.path[len(asset_prefix) :]
            if raw_id.isdigit() and int(raw_id) in self.server.assets:
                _name, data = self.server.assets[int(raw_id)]
                self._bytes(200, data)
                return

        self._json(404, {"message": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/repos/acme/leads/releases":
            payload = json.loads(self._body().decode("utf-8"))
            if not isinstance(payload, dict):
                self._json(422, {"message": "invalid"})
                return
            release = {
                "id": self.server.next_release_id,
                "tag_name": payload.get("tag_name"),
                "draft": payload.get("draft"),
                "name": payload.get("name"),
            }
            self.server.next_release_id += 1
            self.server.releases.append(release)
            self._json(201, self.server.release_payload(release))
            return

        upload_prefix = "/uploads/"
        upload_suffix = "/assets"
        if parsed.path.startswith(upload_prefix) and parsed.path.endswith(upload_suffix):
            middle = parsed.path[len(upload_prefix) : -len(upload_suffix)]
            if middle.isdigit():
                query = parse_qs(parsed.query)
                names = query.get("name", [])
                if len(names) != 1:
                    self._json(422, {"message": "missing name"})
                    return
                asset_id = self.server.next_asset_id
                self.server.next_asset_id += 1
                data = self._body()
                self.server.assets[asset_id] = (names[0], data)
                self._json(
                    201,
                    self.server.asset_payload(asset_id, names[0], data),
                )
                return

        self._json(404, {"message": "not found"})


class DraftReleaseJournalServer(AbstractContextManager["DraftReleaseJournalServer"]):
    """One in-memory durable GitHub API double shared across fresh runner roots."""

    def __init__(self) -> None:
        self._server = _JournalHTTPServer(("127.0.0.1", 0))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> DraftReleaseJournalServer:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        return self._server.origin

    @property
    def releases(self) -> list[dict[str, Any]]:
        return self._server.releases

    @property
    def assets(self) -> dict[int, tuple[str, bytes]]:
        return self._server.assets

    def configure(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
