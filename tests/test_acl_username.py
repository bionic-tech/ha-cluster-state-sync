"""ACL username support — the missing half of I4 / H6.

[ADD 03 §2.3](../docs/designs/03-security-architecture.md) documents the
recipe for a dedicated, least-privilege Valkey user:

    ACL SETUSER ha-cluster-sync on >SECRET ~ha:cluster_state_sync:* ...

Redis authenticates a named ACL user with ``AUTH <username> <password>``. Until
this module, `RedisBackend` passed only ``password`` to the client, so redis-py
sent the one-argument form and authenticated as **default**. The integration
could not log in as the very user its own security architecture prescribes, and
the failure would have presented as a bad password.

These tests run against a real Valkey carrying a real ACL, because the point is
whether the *server* accepts us — which no double can answer.
"""

from __future__ import annotations

import subprocess
import uuid

import pytest

from custom_components.cluster_state_sync.backend import RedisBackend
from custom_components.cluster_state_sync.const import CONF_REDIS_PASSWORD, CONF_REDIS_USERNAME

ACL_USER = "ha-cluster-sync"
ACL_PASSWORD = "an-acl-password"


def _valkey_cli(container_host: tuple[str, int], *args: str) -> str:
    """Run valkey-cli against the session server via its container."""
    host, port = container_host
    return subprocess.run(
        ["docker", "exec", "-i", _container_for(port), "valkey-cli", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _container_for(port: int) -> str:
    """Find the container publishing this port — the fixture started it."""
    out = subprocess.run(
        ["docker", "ps", "--format", "{{.ID}} {{.Ports}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in out.splitlines():
        if f":{port}->" in line:
            return line.split()[0]
    raise RuntimeError(f"no container publishing {port}")


@pytest.fixture
def acl_namespace(valkey_server: tuple[str, int]) -> str:
    """Create the documented least-privilege user, scoped to one namespace."""
    ns = f"t{uuid.uuid4().hex[:12]}"
    _valkey_cli(
        valkey_server,
        "ACL",
        "SETUSER",
        ACL_USER,
        "on",
        f">{ACL_PASSWORD}",
        f"~ha:{ns}:*",
        "+@read",
        "+@write",
        "+@keyspace",
        # Required, and missing from ADD 03 §2.3 as originally written: PING
        # lives in @connection, and `connect()` pings deliberately to fail
        # fast. Without this the user authenticates and is then refused the
        # very first command.
        "+@connection",
        "-@dangerous",
    )
    return ns


async def test_the_backend_can_authenticate_as_a_named_acl_user(
    valkey_server: tuple[str, int], acl_namespace: str, socket_enabled: None
) -> None:
    """The gap this closes: without a username we authenticate as `default`."""
    host, port = valkey_server
    b = RedisBackend(
        namespace=acl_namespace,
        host=host,
        port=port,
        db=0,
        username=ACL_USER,
        password=ACL_PASSWORD,
        secret="s",
    )
    await b.connect()
    try:
        assert await b.health() is True
    finally:
        await b.close()


async def test_a_named_user_with_the_wrong_password_fails_closed(
    valkey_server: tuple[str, int], acl_namespace: str, socket_enabled: None
) -> None:
    """`connect()` raises by contract; setup turns that into ConfigEntryNotReady.

    Failing loudly here is right: a mistyped ACL password should stop setup and
    retry with backoff, not degrade quietly into a node that never syncs.
    """
    host, port = valkey_server
    b = RedisBackend(
        namespace=acl_namespace,
        host=host,
        port=port,
        db=0,
        username=ACL_USER,
        password="not-the-password",
        secret="s",
    )

    with pytest.raises(Exception, match="(?i)auth|password|WRONGPASS"):
        await b.connect()

    await b.close()


async def test_the_acl_user_is_confined_to_its_own_keyspace(
    valkey_server: tuple[str, int], acl_namespace: str, socket_enabled: None
) -> None:
    """H6 proper: the recipe must actually deny keys outside the namespace.

    A user that authenticates but can reach the whole keyspace is worse than no
    ACL, because it reads as protection.
    """
    import redis.asyncio as aioredis

    host, port = valkey_server
    client = aioredis.Redis(
        host=host,
        port=port,
        db=0,
        username=ACL_USER,
        password=ACL_PASSWORD,
        decode_responses=True,
    )
    try:
        # Inside the grant: allowed.
        await client.set(f"ha:{acl_namespace}:probe", "ok")
        assert await client.get(f"ha:{acl_namespace}:probe") == "ok"

        # Outside it: refused by the server, not by us.
        with pytest.raises(Exception, match="(?i)no permissions|NOPERM"):
            await client.get("ha:some-other-namespace:states")
    finally:
        await client.aclose()


def test_the_username_is_a_first_class_config_key() -> None:
    """It has to reach the backend from the config entry, not just the constructor."""
    assert CONF_REDIS_USERNAME == "redis_username"
    assert CONF_REDIS_PASSWORD == "redis_password"
