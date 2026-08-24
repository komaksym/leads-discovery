"""Shared M3 test safety fixtures."""

from __future__ import annotations

import socket
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def zero_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block real DNS and socket access while preserving in-memory HTTP transports."""

    def blocked(*_args: Any, **_kwargs: Any) -> None:
        """Reject one accidental real network operation."""
        raise AssertionError("network access is forbidden in the M3 contract suite")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
