"""Connection-construction tests: TLS (AR-0003) and sentinel parsing (AR-0024).

These assert on the parameters handed to the redis client rather than on a live
connection -- what matters is that a TLS-configured entry cannot silently
produce a plaintext socket.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.cluster_state_sync.backend import RedisBackend
from custom_components.cluster_state_sync.util import parse_sentinel_hosts


async def captured_direct_kwargs(**backend_kwargs: Any) -> dict[str, Any]:
    """Build a direct-mode backend and return the redis client kwargs.

    `connect()` deliberately pings to fail fast on bad config, so the stand-in
    client has to be awaitable.
    """
    backend = RedisBackend(namespace="testns", host="valkey.invalid", **backend_kwargs)
    with patch(
        "custom_components.cluster_state_sync.backend.aioredis.Redis",
        return_value=AsyncMock(),
    ) as redis_cls:
        await backend.connect()
    return redis_cls.call_args.kwargs


# -- AR-0003: TLS -----------------------------------------------------------


async def test_ar_0003_tls_is_off_when_not_configured() -> None:
    """The default stays plaintext; enabling TLS is an explicit choice."""
    kwargs = await captured_direct_kwargs()
    assert kwargs.get("ssl") is False


async def test_ar_0003_tls_reaches_the_client_when_enabled() -> None:
    """AR-0003 — a TLS-configured entry must not produce a plaintext socket.

    Production change that would make this fail: dropping the ssl kwargs from
    the client constructor, which is exactly the v0.1 state -- both Direct and
    Sentinel clients were built with no `ssl=` at all, so alarm and location
    state crossed a flat LAN in the clear.
    """
    kwargs = await captured_direct_kwargs(use_tls=True, tls_ca_certs="/etc/ssl/valkey-ca.pem")

    assert kwargs["ssl"] is True
    assert kwargs["ssl_ca_certs"] == "/etc/ssl/valkey-ca.pem"
    assert kwargs["ssl_cert_reqs"] == "required"


async def test_tls_without_a_ca_still_verifies_against_the_system_store() -> None:
    """Omitting a CA path must not silently disable verification."""
    kwargs = await captured_direct_kwargs(use_tls=True)

    assert kwargs["ssl"] is True
    assert kwargs["ssl_cert_reqs"] == "required"
    assert kwargs["ssl_ca_certs"] is None


async def test_ar_0003_tls_reaches_the_sentinel_client() -> None:
    """Sentinel mode must get the same treatment as direct mode.

    Both the sentinel connections themselves and the resolved master
    connection need TLS; securing only one leaves the other in the clear.
    """
    backend = RedisBackend(
        namespace="testns",
        use_sentinel=True,
        sentinel_hosts=[("valkey-1.invalid", 26379)],
        sentinel_service="valkey-primary",
        use_tls=True,
        tls_ca_certs="/etc/ssl/valkey-ca.pem",
    )
    sentinel = MagicMock()
    sentinel.master_for.return_value = AsyncMock()
    with patch(
        "custom_components.cluster_state_sync.backend.Sentinel", return_value=sentinel
    ) as sentinel_cls:
        await backend.connect()

    sentinel_kwargs = sentinel_cls.call_args.kwargs
    assert sentinel_kwargs["ssl"] is True
    assert sentinel_kwargs["ssl_ca_certs"] == "/etc/ssl/valkey-ca.pem"

    master_kwargs = sentinel.master_for.call_args.kwargs
    assert master_kwargs["ssl"] is True
    assert master_kwargs["ssl_ca_certs"] == "/etc/ssl/valkey-ca.pem"


# -- AR-0024: sentinel host parsing ----------------------------------------


def test_sentinel_hosts_parse_with_explicit_ports() -> None:
    assert parse_sentinel_hosts("a.lan:26379, b.lan:26380") == [
        ("a.lan", 26379),
        ("b.lan", 26380),
    ]


def test_sentinel_hosts_default_the_port() -> None:
    assert parse_sentinel_hosts("a.lan") == [("a.lan", 26379)]


def test_sentinel_hosts_empty_string_is_empty_list() -> None:
    assert parse_sentinel_hosts("") == []


@pytest.mark.parametrize("raw", ["a.lan:notaport", "a.lan:", "a.lan:70000", "a.lan:-1"])
def test_ar_0024_bad_sentinel_port_raises_a_clear_error(raw: str) -> None:
    """AR-0024 — `int(port)` was unvalidated.

    Production change that would make this fail: going back to a bare
    `int(port)`. A typo raised an opaque ValueError from inside setup rather
    than telling the operator which host was wrong, and an out-of-range port
    was accepted silently and failed later at connect time.
    """
    with pytest.raises(ValueError, match="sentinel host"):
        parse_sentinel_hosts(raw)


def test_ipv6_sentinel_host_with_port_parses() -> None:
    """rsplit on ':' must not mangle a bracketed IPv6 literal."""
    assert parse_sentinel_hosts("[fd00::1]:26379") == [("fd00::1", 26379)]
