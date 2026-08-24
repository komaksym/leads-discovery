"""Focused proofs for the global zero-network test boundary."""

from __future__ import annotations

import socket

import httpx
import pytest


def test_dns_resolution_is_blocked() -> None:
    """The global guard rejects real DNS resolution."""
    with pytest.raises(AssertionError, match="network access is forbidden"):
        socket.getaddrinfo("example.com", 443)


def test_socket_connect_is_blocked() -> None:
    """The global guard rejects direct socket connections."""
    with socket.socket() as sock:
        with pytest.raises(AssertionError, match="network access is forbidden"):
            sock.connect(("93.184.216.34", 443))


def test_default_httpx_network_request_is_blocked() -> None:
    """Default HTTPX transport cannot escape the socket-level network guard."""
    with pytest.raises(AssertionError, match="network access is forbidden"):
        httpx.get("https://example.com", timeout=0.1)


def test_httpx_mock_transport_remains_usable() -> None:
    """In-memory HTTPX MockTransport remains available to offline provider tests."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Return one deterministic in-memory provider response."""
        assert request.url == httpx.URL("https://example.com/mock")
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = client.get("https://example.com/mock")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
