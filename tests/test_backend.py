"""Backend-level tests: Redis command shape and entry serialisation.

`test_snapshot.py` asserts that the *caller* hands the backend full state.
This file asserts the other half of AR-0001: that the backend then writes it
with commands that merge rather than replace, so two nodes flushing
concurrently cannot erase each other.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from custom_components.cluster_state_sync.backend import RedisBackend, SnapshotEntry
from custom_components.cluster_state_sync.const import (
    LEASE_TTL_SECONDS,
    SCHEMA_VERSION,
    leader_key,
    meta_key,
    states_key,
)

NAMESPACE = "testns"


class RecordingPipeline:
    """Captures the command sequence a flush issues."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str):
        def record(*args: Any, **kwargs: Any) -> None:
            self.commands.append((name, args, kwargs))

        return record

    async def execute(self) -> list[Any]:
        return []

    @property
    def command_names(self) -> list[str]:
        return [name for name, _, _ in self.commands]


class RecordingClient:
    def __init__(self) -> None:
        self.pipe = RecordingPipeline()

    def pipeline(self, transaction: bool = True) -> RecordingPipeline:
        return self.pipe


@pytest.fixture
def backend() -> RedisBackend:
    be = RedisBackend(namespace=NAMESPACE, host="valkey.invalid", port=6379)
    be._client = RecordingClient()  # noqa: SLF001 — injecting at the client seam
    return be


def make_entry(entity_id: str = "input_boolean.quiet", **overrides: Any) -> SnapshotEntry:
    defaults = {
        "entity_id": entity_id,
        "state": "on",
        "attributes": {"friendly_name": "Quiet"},
        "last_changed": "2026-08-05T10:00:00+00:00",
        "last_updated": "2026-08-05T10:00:00+00:00",
        "source_node": "node-a",
    }
    return SnapshotEntry(**{**defaults, **overrides})


async def test_ar_0001_write_does_not_delete_the_hash(backend: RedisBackend) -> None:
    """The flush must merge, never replace.

    Production change that would make this fail: restoring the
    `pipe.delete(states_key(...))` that preceded the `HSET` in v0.1.

    This is what makes the write safe while both nodes still run the flush
    loop: with the DEL, each node's flush erased the peer's entries and the
    hash oscillated between two partial views (AR-0001 + AR-0017).
    """
    assert await backend.write_snapshot({"input_boolean.quiet": make_entry()}, "node-a")

    names = backend._client.pipe.command_names  # noqa: SLF001
    assert "delete" not in names, "flush must not wipe the shared hash"
    assert "hset" in names


async def test_write_targets_the_namespaced_keys(backend: RedisBackend) -> None:
    """States and meta go to the namespaced keys, keeping clusters isolated."""
    await backend.write_snapshot({"input_boolean.quiet": make_entry()}, "node-a")

    commands = backend._client.pipe.commands  # noqa: SLF001
    hset = next(c for c in commands if c[0] == "hset")
    set_cmd = next(c for c in commands if c[0] == "set")

    assert hset[1][0] == states_key(NAMESPACE)
    assert set_cmd[1][0] == meta_key(NAMESPACE)


async def test_meta_records_schema_version_and_entry_count(
    backend: RedisBackend,
) -> None:
    """AR-0026 — a reader must be able to tell what wrote the snapshot."""
    entries = {f"input_boolean.b{i}": make_entry(f"input_boolean.b{i}") for i in range(3)}
    await backend.write_snapshot(entries, "node-a")

    set_cmd = next(c for c in backend._client.pipe.commands if c[0] == "set")  # noqa: SLF001
    meta = json.loads(set_cmd[1][1])

    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["entry_count"] == 3
    assert meta["source_node"] == "node-a"


async def test_empty_write_is_a_no_op(backend: RedisBackend) -> None:
    """Nothing to say means nothing on the wire."""
    assert await backend.write_snapshot({}, "node-a") is False
    assert backend._client.pipe.commands == []  # noqa: SLF001


async def test_backend_failure_is_swallowed_not_raised(backend: RedisBackend) -> None:
    """The core design promise: Redis being down degrades, never crashes HA.

    Production change that would make this fail: letting the exception escape
    `write_snapshot` instead of logging and returning False.
    """

    class ExplodingClient:
        def pipeline(self, transaction: bool = True):
            raise ConnectionError("valkey is gone")

    backend._client = ExplodingClient()  # noqa: SLF001
    assert await backend.write_snapshot({"a.b": make_entry("a.b")}, "node-a") is False


# -- AR-0013: one malformed stored value must not zero the restore --------


def read_backend(states: dict[str, str], meta: str | None = None) -> RedisBackend:
    """Backend whose read script returns a canned result.

    Returns the states as a flat array, which is what Lua actually hands back
    for a hash -- if the production code stopped unflattening it, these tests
    would notice.
    """
    flat: list[str] = []
    for k, v in states.items():
        flat += [k, v]

    class Client:
        async def eval(self, *_args: Any) -> list[Any]:
            return [flat, meta]

    be = RedisBackend(namespace=NAMESPACE, host="valkey.invalid", port=6379)
    be._client = Client()  # noqa: SLF001
    return be


async def test_ar_0013_one_malformed_entry_does_not_zero_the_read() -> None:
    """AR-0013 — a single unreadable value must cost one entry, not all of them.

    Production change that would make this fail: parsing the hash back as a
    single dict comprehension. One KeyError propagates out of `read_snapshot`,
    the broad except returns ({}, {}), and the promoted standby comes up
    completely cold -- a total failover failure caused by one bad field.
    """
    good = make_entry("input_boolean.good").to_json()
    backend = read_backend(
        {
            "input_boolean.good": good,
            "input_boolean.truncated": '{"s": "on"',  # invalid JSON
            "input_boolean.missing_field": '{"s": "on"}',  # no lc/lu
        }
    )

    entries, _meta = await backend.read_snapshot()

    assert set(entries) == {"input_boolean.good"}
    assert entries["input_boolean.good"].state == "on"


async def test_unreadable_meta_does_not_discard_good_entries() -> None:
    """Meta is diagnostic; corrupt meta must not cost us the states."""
    backend = read_backend(
        {"input_boolean.good": make_entry("input_boolean.good").to_json()},
        meta="{not json",
    )

    entries, meta = await backend.read_snapshot()

    assert set(entries) == {"input_boolean.good"}
    assert meta == {}


# -- SnapshotEntry serialisation ------------------------------------------


def test_entry_round_trips_through_json() -> None:
    entry = make_entry()
    restored = SnapshotEntry.from_json(entry.entity_id, entry.to_json())
    assert restored == entry


def test_serialised_entry_carries_the_schema_version() -> None:
    """AR-0026 — every entry is self-describing."""
    assert json.loads(make_entry().to_json())["v"] == SCHEMA_VERSION


def test_entry_without_a_version_is_read_as_v1() -> None:
    """Entries written before versioning must still restore.

    A standby promoted during a rolling upgrade will read whatever the old
    node last wrote; refusing it would turn an upgrade into a cold start.
    """
    legacy = json.dumps(
        {
            "s": "on",
            "a": {},
            "lc": "2026-08-05T10:00:00+00:00",
            "lu": "2026-08-05T10:00:00+00:00",
            "n": "node-a",
        }
    )
    entry = SnapshotEntry.from_json("input_boolean.quiet", legacy)
    assert entry.state == "on"
    assert entry.source_node == "node-a"


def test_entry_from_a_newer_schema_is_rejected() -> None:
    """Refuse to guess at a format a future node defined.

    Production change that would make this fail: dropping the version check in
    `from_json`. Silently misreading a v2 entry is worse than skipping it --
    the caller treats a rejected entry as one bad entry (AR-0013), not as a
    reason to abandon the restore.
    """
    future = json.dumps(
        {
            "v": SCHEMA_VERSION + 1,
            "s": "on",
            "a": {},
            "lc": "2026-08-05T10:00:00+00:00",
            "lu": "2026-08-05T10:00:00+00:00",
            "n": "node-b",
        }
    )
    with pytest.raises(ValueError, match="newer node"):
        SnapshotEntry.from_json("input_boolean.quiet", future)


# -- AR-0017: the Valkey leadership lease ----------------------------------


class LeaseClient:
    """Records eval() calls and returns a canned result."""

    def __init__(self, result: int = 1) -> None:
        self.result = result
        self.calls: list[tuple[Any, ...]] = []

    async def eval(self, *args: Any) -> int:
        self.calls.append(args)
        return self.result


def lease_backend(result: int = 1) -> RedisBackend:
    be = RedisBackend(namespace=NAMESPACE, host="valkey.invalid", port=6379)
    be._client = LeaseClient(result)  # noqa: SLF001
    return be


async def test_ar_0017_lease_is_taken_atomically_in_one_script() -> None:
    """AR-0017 — take-or-renew must be one atomic decision inside Valkey.

    Production change that would make this fail: replacing the script with a
    GET-then-SET in Python. Both nodes could then read "unheld" in the same
    instant and both conclude they are leader -- reintroducing exactly the
    dual-writer race the lease exists to close.
    """
    backend = lease_backend(result=1)

    assert await backend.acquire_leadership("node-a") is True

    (call,) = backend._client.calls  # noqa: SLF001
    script, numkeys, key, node_id, ttl_ms = call
    assert numkeys == 1
    assert key == leader_key(NAMESPACE)
    assert node_id == "node-a"
    assert int(ttl_ms) == LEASE_TTL_SECONDS * 1000
    # The renew branch must be conditional on already holding it, otherwise a
    # follower could steal leadership from a live leader.
    assert "ARGV[1]" in script
    assert "PEXPIRE" in script


async def test_lease_not_held_returns_false() -> None:
    """A lease held by the peer means this node is a follower."""
    backend = lease_backend(result=0)
    assert await backend.acquire_leadership("node-a") is False


async def test_lease_failure_is_not_a_yes() -> None:
    """A backend that cannot answer must never be read as leadership.

    Production change that would make this fail: letting the exception escape,
    or returning True on error. Assuming leadership when it cannot be
    established is how two leaders happen.
    """

    class Exploding:
        async def eval(self, *args: Any) -> int:
            raise ConnectionError("valkey is gone")

    backend = lease_backend()
    backend._client = Exploding()  # noqa: SLF001
    assert await backend.acquire_leadership("node-a") is False


async def test_release_only_deletes_a_lease_we_hold() -> None:
    """Handback must never delete a lease the peer has since taken."""
    backend = lease_backend()
    await backend.release_leadership("node-a")

    (call,) = backend._client.calls  # noqa: SLF001
    script = call[0]
    assert "GET" in script and "DEL" in script
    assert "ARGV[1]" in script


# -- AR-0018: consistent read of states + meta -----------------------------


class ScriptClient:
    """Records eval() calls and returns a canned [states, meta] pair."""

    def __init__(self, states: dict[str, str], meta: str | None) -> None:
        self.result = [states, meta]
        self.calls: list[tuple[Any, ...]] = []

    async def eval(self, *args: Any) -> list[Any]:
        self.calls.append(args)
        return self.result


async def test_ar_0018_states_and_meta_are_read_in_one_atomic_operation() -> None:
    """AR-0018 — the reader must not see states from one flush and meta from another.

    Production change that would make this fail: going back to a
    non-transactional pipeline of HGETALL + GET.

    The two keys are written inside a MULTI, so a pipelined reader can land
    between the peer's HSET and its SET and come away with a snapshot whose
    meta describes a different write. `last_snapshot_at` then understates the
    age of the states actually held -- and that timestamp is what the operator
    reads to decide whether a failover is safe.
    """
    good = make_entry("input_boolean.good").to_json()
    backend = RedisBackend(namespace=NAMESPACE, host="valkey.invalid", port=6379)
    backend._client = ScriptClient(  # noqa: SLF001
        {"input_boolean.good": good}, json.dumps({"entry_count": 1})
    )

    entries, meta = await backend.read_snapshot()

    assert set(entries) == {"input_boolean.good"}
    assert meta["entry_count"] == 1

    (call,) = backend._client.calls  # noqa: SLF001
    script, numkeys = call[0], call[1]
    assert numkeys == 2, "both keys must be read by one script invocation"
    assert call[2] == states_key(NAMESPACE)
    assert call[3] == meta_key(NAMESPACE)
    assert "HGETALL" in script.upper()


async def test_read_still_degrades_gracefully_when_the_script_fails() -> None:
    """A backend failure must still return empty, never raise into HA."""

    class Exploding:
        async def eval(self, *args: Any) -> list[Any]:
            raise ConnectionError("valkey is gone")

    backend = RedisBackend(namespace=NAMESPACE, host="valkey.invalid", port=6379)
    backend._client = Exploding()  # noqa: SLF001

    assert await backend.read_snapshot() == ({}, {})


# -- Task 3 review fix: fileset blob/manifest storage ----------------------
#
# Two gaps a review found in the first cut: nothing asserted the
# not-connected behaviour of the four fileset methods, and the round-trip
# test in test_fileset_valkey.py only checks final stored values -- a
# reversed implementation (manifest before blobs) would pass it just as
# happily. AR-0040 is the precedent for both: a restore that silently did
# nothing for its entire life while the suite stayed green.


async def test_write_fileset_raises_when_not_connected() -> None:
    """`write_fileset` has no honest empty return that could mean "did not
    happen" without being indistinguishable from "happened and stored
    nothing" -- so unlike its three siblings below, it raises rather than
    swallowing. A caller (Task 5's publisher) that treated a swallowed
    failure as success would report a fileset generation that was never
    actually stored."""
    backend = RedisBackend(namespace=NAMESPACE, host="valkey.invalid", port=6379)

    with pytest.raises(RuntimeError, match="not connected"):
        await backend.write_fileset(b"manifest", {"a": b"1"})


async def test_read_fileset_manifest_returns_none_when_not_connected() -> None:
    backend = RedisBackend(namespace=NAMESPACE, host="valkey.invalid", port=6379)
    assert await backend.read_fileset_manifest() is None


async def test_read_blobs_returns_empty_when_not_connected() -> None:
    backend = RedisBackend(namespace=NAMESPACE, host="valkey.invalid", port=6379)
    assert await backend.read_blobs(["a", "b"]) == {}


async def test_prune_blobs_returns_zero_when_not_connected() -> None:
    backend = RedisBackend(namespace=NAMESPACE, host="valkey.invalid", port=6379)
    assert await backend.prune_blobs({"keep"}) == 0


async def test_read_fileset_manifest_degrades_on_backend_failure() -> None:
    """The class contract applies here too: only `write_fileset` raises."""

    class Exploding:
        async def get(self, *_args: Any) -> None:
            raise ConnectionError("valkey is gone")

    backend = RedisBackend(namespace=NAMESPACE, host="valkey.invalid", port=6379)
    backend._client = Exploding()  # noqa: SLF001

    assert await backend.read_fileset_manifest() is None


async def test_read_blobs_degrades_on_backend_failure() -> None:
    class Exploding:
        async def mget(self, *_args: Any) -> None:
            raise ConnectionError("valkey is gone")

    backend = RedisBackend(namespace=NAMESPACE, host="valkey.invalid", port=6379)
    backend._client = Exploding()  # noqa: SLF001

    assert await backend.read_blobs(["a"]) == {}


async def test_prune_blobs_degrades_on_backend_failure() -> None:
    class Exploding:
        def scan_iter(self, *_args: Any, **_kwargs: Any) -> Any:
            raise ConnectionError("valkey is gone")

    backend = RedisBackend(namespace=NAMESPACE, host="valkey.invalid", port=6379)
    backend._client = Exploding()  # noqa: SLF001

    assert await backend.prune_blobs({"keep"}) == 0


class OrderingClient:
    """Records the sequence of pipeline execution versus the manifest `set`.

    `test_blobs_land_before_the_manifest_moves` in test_fileset_valkey.py
    writes two generations to completion and only asserts final state --
    swapping the manifest `set` ahead of the blob pipeline's `execute()`
    would pass that test just as happily, since both writes finish before any
    assertion runs. This double records the actual order of operations so a
    reversed implementation fails the test meant to catch it, rather than the
    guarantee being established only by reading the code.
    """

    def __init__(self) -> None:
        self.order: list[str] = []

    def pipeline(self, transaction: bool = True) -> OrderingPipeline:
        return OrderingPipeline(self.order)

    async def set(self, key: str, value: str) -> None:
        self.order.append("manifest_set")


class OrderingPipeline:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    def set(self, key: str, value: str) -> None:
        pass  # the blob writes themselves aren't what's under test here

    async def execute(self) -> list[Any]:
        self._order.append("blob_pipeline_execute")
        return []


async def test_blob_pipeline_executes_before_the_manifest_set() -> None:
    """The ordering contract, asserted on the actual sequence of Redis calls.

    Production change that would make this fail: swapping the manifest `set`
    ahead of the blob pipeline's `execute()`. A manifest referencing a blob
    that has not landed is a torn read a follower cannot recover from.
    """
    client = OrderingClient()
    backend = RedisBackend(namespace=NAMESPACE, host="valkey.invalid", port=6379)
    backend._client = client  # noqa: SLF001

    await backend.write_fileset(b"manifest-bytes", {"a": b"1", "b": b"2"})

    assert client.order == ["blob_pipeline_execute", "manifest_set"]


async def test_manifest_still_moves_when_there_are_no_blobs_to_write() -> None:
    """An empty blob map must not skip the manifest write -- there is no
    pipeline to execute, but the manifest still has to move."""
    client = OrderingClient()
    backend = RedisBackend(namespace=NAMESPACE, host="valkey.invalid", port=6379)
    backend._client = client  # noqa: SLF001

    await backend.write_fileset(b"manifest-bytes", {})

    assert client.order == ["manifest_set"]
