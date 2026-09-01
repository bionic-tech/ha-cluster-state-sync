"""Backend tests against a **real** Valkey — READINESS T4.

Every other backend test in this suite asserts against a hand-written double.
`LeaseClient` "records eval() calls and returns a canned result";
`ScriptClient` "records eval() calls and returns a canned [states, meta] pair".
Both assert that we passed the right arguments. Neither runs the Lua.

There are three Lua scripts in `backend.py` and until this module none of them
had ever been executed by a Redis:

* ``_READ_SCRIPT``   — the atomic snapshot read (AR-0018)
* ``_LEASE_SCRIPT``  — take-or-renew, the split-brain guard (ADR-003)
* the inline release script in ``release_leadership``

Those are the highest-consequence lines in the project — the lease is what
stops two leaders writing at once — and they were the least tested. A double
that returns `1` when we hoped for `1` proves nothing about whether Valkey
agrees.

Engine: `valkey/valkey:8.1`, matching what the fleet runs. See the
`valkey_server` fixture in conftest.py; if no real server is available these
tests **skip loudly** rather than passing quietly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
import uuid

import pytest

from custom_components.cluster_state_sync import backend as backend_module
from custom_components.cluster_state_sync.backend import RedisBackend, SnapshotEntry

SECRET = "a-test-cluster-secret"


def _entry(
    entity_id: str, state: str = "on", *, node: str = "node-a", **attrs: object
) -> SnapshotEntry:
    stamp = datetime.now(tz=UTC).isoformat()
    return SnapshotEntry(
        entity_id=entity_id,
        state=state,
        attributes=dict(attrs) or {"friendly_name": "Test"},
        last_changed=stamp,
        last_updated=stamp,
        source_node=node,
    )


@pytest.fixture
async def valkey_backend(
    valkey_server: tuple[str, int], socket_enabled: None
) -> AsyncGenerator[RedisBackend]:
    """A connected backend on a namespace unique to this test.

    Unique namespaces mean the tests neither collide nor need teardown between
    them, which keeps each one readable in isolation.
    """
    host, port = valkey_server
    b = RedisBackend(
        namespace=f"t{uuid.uuid4().hex[:12]}",
        host=host,
        port=port,
        db=0,
        secret=SECRET,
    )
    await b.connect()
    try:
        yield b
    finally:
        await b.close()


# ---------------------------------------------------------------------------
# _LEASE_SCRIPT — the split-brain guard
# ---------------------------------------------------------------------------


async def test_an_unheld_lease_is_granted(valkey_backend: RedisBackend) -> None:
    assert await valkey_backend.acquire_leadership("node-a") is True


async def test_the_holder_can_renew_its_own_lease(valkey_backend: RedisBackend) -> None:
    """Renewal is the common path — a healthy leader re-takes every interval."""
    assert await valkey_backend.acquire_leadership("node-a") is True

    assert await valkey_backend.acquire_leadership("node-a") is True


async def test_a_second_node_is_refused_while_the_lease_is_held(
    valkey_backend: RedisBackend,
) -> None:
    """The whole reason the lease exists. If Valkey disagrees, we get two leaders."""
    await valkey_backend.acquire_leadership("node-a")

    assert await valkey_backend.acquire_leadership("node-b") is False


async def test_a_refused_node_does_not_extend_the_holders_lease(
    valkey_backend: RedisBackend,
) -> None:
    """A losing bid must be inert.

    If the `elseif` matched too loosely, a follower hammering the lease every
    interval would keep a dead leader's key alive forever and the standby would
    never promote.
    """
    monkey_ttl = 2
    original = backend_module.LEASE_TTL_SECONDS
    backend_module.LEASE_TTL_SECONDS = monkey_ttl
    try:
        await valkey_backend.acquire_leadership("node-a")
        for _ in range(6):
            await asyncio.sleep(0.4)
            await valkey_backend.acquire_leadership("node-b")
        # node-a never renewed; node-b was refused throughout. The lease must
        # have lapsed on its own TTL despite node-b's traffic.
        assert await valkey_backend.acquire_leadership("node-b") is True
    finally:
        backend_module.LEASE_TTL_SECONDS = original


async def test_the_lease_expires_so_a_dead_leader_does_not_block_promotion(
    valkey_backend: RedisBackend,
) -> None:
    """Timing behaviour, which only a real server can demonstrate."""
    original = backend_module.LEASE_TTL_SECONDS
    backend_module.LEASE_TTL_SECONDS = 1
    try:
        assert await valkey_backend.acquire_leadership("node-a") is True
        assert await valkey_backend.acquire_leadership("node-b") is False

        await asyncio.sleep(1.5)

        assert await valkey_backend.acquire_leadership("node-b") is True
    finally:
        backend_module.LEASE_TTL_SECONDS = original


# ---------------------------------------------------------------------------
# release_leadership — the inline script
# ---------------------------------------------------------------------------


async def test_release_hands_the_lease_straight_to_the_peer(
    valkey_backend: RedisBackend,
) -> None:
    """Handback is what saves the standby waiting out the full TTL."""
    await valkey_backend.acquire_leadership("node-a")

    await valkey_backend.release_leadership("node-a")

    assert await valkey_backend.acquire_leadership("node-b") is True


async def test_release_never_deletes_a_lease_the_peer_has_taken(
    valkey_backend: RedisBackend,
) -> None:
    """A late shutdown must not evict the node that has since promoted.

    This is the compare-and-delete in the inline script. Get it wrong and an
    orderly shutdown of the *old* leader unseats the new one.
    """
    original = backend_module.LEASE_TTL_SECONDS
    backend_module.LEASE_TTL_SECONDS = 1
    try:
        await valkey_backend.acquire_leadership("node-a")
        await asyncio.sleep(1.5)
        assert await valkey_backend.acquire_leadership("node-b") is True

        await valkey_backend.release_leadership("node-a")

        # node-b must still hold it, so node-a cannot simply take it back.
        assert await valkey_backend.acquire_leadership("node-a") is False
    finally:
        backend_module.LEASE_TTL_SECONDS = original


# ---------------------------------------------------------------------------
# _READ_SCRIPT — the atomic snapshot read
# ---------------------------------------------------------------------------


async def test_a_snapshot_round_trips_through_a_real_valkey(
    valkey_backend: RedisBackend,
) -> None:
    """Write with the real pipeline, read back with the real Lua."""
    written = {
        "light.kitchen": _entry("light.kitchen", "on", friendly_name="Kitchen"),
        "sensor.temperature": _entry(
            "sensor.temperature", "21.5", unit_of_measurement="°C"
        ),
    }

    assert await valkey_backend.write_snapshot(written, "node-a") is True
    entries, meta = await valkey_backend.read_snapshot()

    assert set(entries) == {"light.kitchen", "sensor.temperature"}
    assert entries["light.kitchen"].state == "on"
    # Non-ASCII survives the JSON -> Valkey -> Lua -> JSON round trip.
    assert entries["sensor.temperature"].attributes["unit_of_measurement"] == "°C"
    # The meta key is written inside the same MULTI as the states, and the Lua
    # returns both in one call; assert its whole shape, since this is the first
    # time a real server has produced it.
    assert meta["source_node"] == "node-a"
    assert meta["entry_count"] == 2
    assert meta["schema_version"] == 1
    assert "last_snapshot_at" in meta


async def test_reading_an_empty_namespace_yields_nothing_rather_than_raising(
    valkey_backend: RedisBackend,
) -> None:
    """HGETALL on a missing key returns an empty table and GET returns nil.

    The unflattening code has to survive both, and a double that returns
    ``[[], None]`` is asserting our own guess about the shape.
    """
    entries, meta = await valkey_backend.read_snapshot()

    assert entries == {}
    assert meta == {}


async def test_two_nodes_merge_rather_than_erasing_each_other(
    valkey_backend: RedisBackend,
) -> None:
    """ADR-004's no-DEL decision, demonstrated against the real engine."""
    await valkey_backend.write_snapshot({"light.a": _entry("light.a", "on")}, "node-a")
    await valkey_backend.write_snapshot(
        {"light.b": _entry("light.b", "off", node="node-b")}, "node-b"
    )

    entries, _ = await valkey_backend.read_snapshot()

    assert set(entries) == {"light.a", "light.b"}


async def test_a_tampered_entry_is_refused_by_a_real_read(
    valkey_backend: RedisBackend,
) -> None:
    """Signature verification, end to end through Valkey rather than in-process."""
    await valkey_backend.write_snapshot({"light.a": _entry("light.a", "on")}, "node-a")

    forged = RedisBackend(
        namespace=valkey_backend._namespace,  # noqa: SLF001 — same keyspace on purpose
        host=valkey_backend._host,  # noqa: SLF001
        port=valkey_backend._port,  # noqa: SLF001
        db=0,
        secret="a-different-secret",
    )
    await forged.connect()
    try:
        entries, _ = await forged.read_snapshot()
    finally:
        await forged.close()

    assert entries == {}


async def test_health_is_true_against_a_live_server(
    valkey_backend: RedisBackend,
) -> None:
    assert await valkey_backend.health() is True
