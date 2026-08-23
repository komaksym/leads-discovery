"""Shared M3 test safety fixtures."""

from __future__ import annotations

import socket
from typing import Any

import httpx
import pytest


@pytest.fixture(autouse=True)
def zero_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail immediately if any test accidentally attempts HTTP, DNS, or a socket connection."""

    def blocked(*_args: Any, **_kwargs: Any) -> None:
        """Reject one accidental network operation."""
        raise AssertionError("network access is forbidden in the M3 contract suite")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(
        httpx.Client,
        "request",
        blocked,
    )
    monkeypatch.setattr(
        httpx.AsyncClient,
        "request",
        blocked,
    )
