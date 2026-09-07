"""Shared M3 test safety fixtures."""

from __future__ import annotations

import socket
from typing import Any

import pytest

from leads_discovery import production_canary


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


@pytest.fixture(autouse=True)
def zero_canary_poll_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep offline canary tests deterministic without changing production poll cadence."""
    monkeypatch.setattr(production_canary, "sleep", lambda _delay: None)
