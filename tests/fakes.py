"""Test doubles for the cluster_state_sync suite.

`FakeBackend` stands in for `RedisBackend` at the `ClusterBackend` boundary --
the one seam where mocking is genuinely unavoidable, since the alternative is a
live Valkey. Everything above it (the state mirror, the restore guards, the
lifecycle wiring) runs as real code against a real `hass`.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
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
        self.read_result: tuple[dict[str, SnapshotEntry], dict[str, Any]] | None = None
        self.fileset_manifest: bytes | None = None
        self.blobs: dict[str, bytes] = {}

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def write_snapshot(self, entries: dict[str, SnapshotEntry], source_node: str) -> bool:
        if self.fail_writes:
            return False
        self.writes.append(dict(entries))
        self.stored.update(entries)
        self.meta = {"source_node": source_node, "entry_count": len(self.stored)}
        return True

    async def read_snapshot(self) -> tuple[dict[str, SnapshotEntry], dict[str, Any]]:
        if self.read_result is not None:
            return self.read_result
        return dict(self.stored), dict(self.meta)

    async def health(self) -> bool:
        return self.connected

    async def acquire_leadership(self, node_id: str) -> bool:
        if self.lease_raises:
            raise ConnectionError("valkey is unreachable")
        if self.lease_holder is None:
            self.lease_holder = node_id
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

    # -- assertion helpers -------------------------------------------------

    @property
    def last_write(self) -> dict[str, SnapshotEntry]:
        """The entries passed to the most recent successful write."""
        assert self.writes, "no snapshot was ever written"
        return self.writes[-1]
