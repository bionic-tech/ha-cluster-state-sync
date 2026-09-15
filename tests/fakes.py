"""Test doubles for the cluster_state_sync suite.

`FakeBackend` stands in for `RedisBackend` at the `ClusterBackend` boundary --
the one seam where mocking is genuinely unavoidable, since the alternative is a
live Valkey. Everything above it (the state mirror, the restore guards, the
lifecycle wiring) runs as real code against a real `hass`.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from custom_components.cluster_state_sync.backend import ClusterBackend, SnapshotEntry


class FakeBackend(ClusterBackend):
    """In-memory ClusterBackend that records every write for assertion.

    Models the *observable contract* of the real backend, not its Redis
    mechanics: `write_snapshot` merges into the stored map the way an `HSET`
    without a preceding `DEL` does. A backend that wipes the hash first is
    tested separately, against a fake Redis client, in test_backend.py.
    """

    def __init__(self) -> None:
        self.stored: dict[str, SnapshotEntry] = {}
        self.meta: dict[str, Any] = {}
        self.writes: list[dict[str, SnapshotEntry]] = []
        self.connected = False
        self.fail_writes = False
        self.lease_holder: str | None = None
        self.lease_raises = False
        # Cluster registry (node_key). `offset_s` is this fake node's clock
        # offset against the store; tests that care about skew set it
        # per-node, and the default of 0.0 means "agrees with the store".
        self.offset_s: float | None = 0.0
        self.members: dict[str, dict] = {}
        self.read_result: tuple[dict[str, SnapshotEntry], dict[str, Any]] | None = None
        self.fileset_manifest: bytes | None = None
        self.blobs: dict[str, bytes] = {}
        # The statistics window, and the line the follower writes back.
        # `fail_writes` covers this too: the real `write_statistics`
        # raises rather than returning quietly, and a fake that swallowed
        # the failure would exercise the publisher's error path as a
        # no-op -- the exact way an omitted field turns a guard into a
        # test that cannot fail.
        self.statistics: bytes | None = None
        # SEALED bytes, as the real backend returns them -- a fake that
        # handed back a parsed dict would exercise the leader's unsealing
        # as a no-op, which is the shape that turns a guard into a test
        # that cannot fail.
        self.statistics_status: bytes | None = None
        self.promoter_nodes: set[str] = set()

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def write_snapshot(self, entries: dict[str, SnapshotEntry], source_node: str) -> bool:
        if self.fail_writes:
            return False
        self.writes.append(dict(entries))
        self.stored.update(entries)
        # `last_snapshot_at` too, exactly as the real backend writes it. Its
        # absence here made the fake unfaithful in the one way that
        # mattered: the restore gates the whole snapshot on this
        # timestamp, so a fake that omitted it exercised the gate as a
        # no-op and every test passed regardless of the rule.
        self.meta = {
            "source_node": source_node,
            "entry_count": len(self.stored),
            "last_snapshot_at": datetime.now(tz=UTC).isoformat(),
        }
        return True

    async def read_snapshot(self) -> tuple[dict[str, SnapshotEntry], dict[str, Any]]:
        if self.read_result is not None:
            return self.read_result
        return dict(self.stored), dict(self.meta)

    async def read_cluster_view(self) -> tuple[str | None, dict[str, Any]]:
        # Mirrors the real backend's degradation: a disconnected one publishes
        # "nothing known", not stale values it can no longer vouch for.
        if not self.connected:
            return None, {}
        return self.lease_holder, dict(self.meta)

    async def health(self) -> bool:
        return self.connected

    async def acquire_leadership(self, node_id: str) -> bool:
        if self.lease_raises:
            raise ConnectionError("valkey is unreachable")
        if self.lease_holder is None:
            self.lease_holder = node_id
        return self.lease_holder == node_id

    async def register_node(self, node_id: str) -> float | None:
        self.members[node_id] = {"node_id": node_id, "offset_s": self.offset_s}
        return self.offset_s

    async def read_members(self) -> dict[str, dict]:
        return dict(self.members)

    async def renew_leadership(self, node_id: str) -> bool:
        """Renew only -- never take a free lease.

        The asymmetry with `acquire_leadership` above is the whole point of the
        maintenance hold, so the fake has to model it faithfully: a fake that
        claimed a free lease here would let a test pass while the real hold
        failed open, which is the class of unfaithful stub this project has
        already been bitten by.
        """
        if self.lease_raises:
            raise ConnectionError("valkey is unreachable")
        return self.lease_holder == node_id

    async def release_leadership(self, node_id: str) -> None:
        if self.lease_holder == node_id:
            self.lease_holder = None

    async def write_fileset(self, manifest: bytes, blobs: Mapping[str, bytes]) -> None:
        self.blobs.update(blobs)
        self.fileset_manifest = manifest

    async def read_fileset_manifest(self) -> bytes | None:
        return self.fileset_manifest

    async def read_blobs(self, refs: Sequence[str]) -> dict[str, bytes]:
        return {ref: self.blobs[ref] for ref in refs if ref in self.blobs}

    async def prune_blobs(self, keep: Collection[str]) -> int:
        doomed = [ref for ref in self.blobs if ref not in keep]
        for ref in doomed:
            del self.blobs[ref]
        return len(doomed)

    async def write_statistics(self, sealed: bytes) -> None:
        if self.fail_writes:
            raise RuntimeError("fake backend: statistics write failed")
        self.statistics = sealed

    async def read_statistics_status(self) -> bytes | None:
        return self.statistics_status

    async def read_promoter_nodes(self) -> set[str]:
        return set(self.promoter_nodes)

    # -- assertion helpers -------------------------------------------------

    @property
    def last_write(self) -> dict[str, SnapshotEntry]:
        """The entries passed to the most recent successful write."""
        assert self.writes, "no snapshot was ever written"
        return self.writes[-1]


def render_schema_for_frontend(schema, custom_serializer):
    """Serialise a config-flow schema the way Home Assistant serialises it.

    🚨 Home Assistant changed the library underneath this between 2026.6.4 and
    2026.9.2, and changed nothing a symbol check can see.

    Until 2026.6.x it used `voluptuous_serialize.convert`. 2026.9.x replaced
    `voluptuous` wholesale with **probatio**, which installs itself under the
    name `voluptuous` at Home Assistant import time (`install_as_voluptuous`) —
    so `import voluptuous` in this process is `probatio._vol_shim`, and
    `cv.custom_serializer` now returns probatio's `_Unsupported` sentinel.

    The old `convert()` does not recognise that sentinel. It does not raise: it
    RETURNS it, so the caller gets a non-iterable object where a list of fields
    used to be, and the failure surfaces as `TypeError: '_Unsupported' object is
    not iterable` several lines away from the cause.

    These tests exist to catch a wizard step whose schema cannot be rendered,
    which the operator meets as an error where a form should be. That is worth
    keeping working across the version this repository pins and the version it
    is moving to, so this tries the new path first and falls back.
    """
    try:
        from probatio.codecs import to_field_list
    except ImportError:
        import voluptuous_serialize

        return voluptuous_serialize.convert(schema, custom_serializer=custom_serializer)
    return to_field_list(schema, custom_serializer=custom_serializer)
