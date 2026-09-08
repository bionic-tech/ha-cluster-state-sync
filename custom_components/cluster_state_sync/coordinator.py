"""Diagnostic state for Cluster State Sync (AR-0019, AR-0032).

The v1 review's third theme was that this integration was "built to fail safe,
but blind while doing it" — the graceful-degradation design is sound, but with
no tests, no CI and no observability, AR-0001 and a dead backend both looked
exactly like a healthy green failover. Nobody would have been paged.

This module is the "nobody gets paged" half of that. `entities_tracked` in
particular is the number that makes AR-0001 self-evident: a snapshot carrying
three entities on a system tracking two hundred is not subtle once it is on
screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .backend import ClusterBackend
from .const import DOMAIN, HEALTH_POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)


@dataclass
class SyncStats:
    """Counters that outlive any single flush or restore."""

    restored_count: int = 0
    last_restore_at: datetime | None = None

    #: Entities dropped from the snapshot for exceeding `MAX_ATTRIBUTE_BYTES`,
    #: newest-first, capped so a pathological config cannot grow this without
    #: bound. A set rather than a count: "which ones" is the actionable half,
    #: and the same entity is skipped on every single flush.
    #:
    #: Why this exists. On a real 3,595-entity estate exactly one entity --
    #: `sensor.watchman_missing_entities`, 100,911 bytes -- was 12% of the
    #: whole payload and was silently dropped on every flush. The skip logged
    #: at WARNING and nobody read it, which is the shape this project keeps
    #: finding: a thing that never replicates, and no surface saying so
    #: (ADR-008).
    oversized: set[str] = field(default_factory=set)

    def record_restore(self, count: int) -> None:
        self.restored_count = count
        self.last_restore_at = datetime.now(tz=UTC)

    #: The last recorder snapshot attempt, or None if one has never run.
    #: Kept whole rather than as a timestamp: "it failed, and here is why" is
    #: the part an operator needs, and a bare age cannot carry it (ADR-008).
    last_recorder_snapshot: Any = None

    def record_recorder_snapshot(self, result: Any) -> None:
        """Remember the outcome of a snapshot attempt, success or not."""
        self.last_recorder_snapshot = result

    def record_oversized(self, entity_id: str) -> None:
        """Remember an entity that will never replicate until it slims down."""
        # Bounded on purpose: this is a diagnostic, not an inventory, and an
        # unbounded set fed by a per-flush loop is a slow leak.
        if len(self.oversized) < 200:
            self.oversized.add(entity_id)


class BackendHealthCoordinator(DataUpdateCoordinator[bool]):
    """Polls the backend so a dead one is visible rather than merely quiet.

    `health()` existed in v0.1 and was never called from anywhere, which is
    AR-0019: the integration could sit happily failing every write while
    reporting nothing at all.
    """

    def __init__(self, hass: HomeAssistant, backend: ClusterBackend) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_backend_health",
            update_interval=timedelta(seconds=HEALTH_POLL_INTERVAL),
        )
        self._backend = backend

    async def _async_update_data(self) -> bool:
        # Deliberately does not raise UpdateFailed on a down backend: "the
        # backend is down" is the reading we want to publish, not an error that
        # makes the entity unavailable and hides it.
        return await self._backend.health()


@dataclass(frozen=True)
class ClusterView:
    """What the shared store says, as distinct from what this node did.

    The existing diagnostics are all local: `entities_tracked` is what this node
    mirrors, and `last_snapshot_age` measures this node's own last successful
    flush. Both are the right questions on a leader and nearly useless on a
    standby, which by design never flushes at all — its `last_snapshot_age`
    climbs forever while the cluster's snapshot may be perfectly fresh.

    This is the other half: read from the shared store, identical on both nodes,
    and therefore the only thing that can answer "is the cluster healthy" rather
    than "am I healthy".
    """

    leader: str | None = None
    snapshot_source: str | None = None
    snapshot_at: datetime | None = None
    entry_count: int | None = None
    #: Every node that has announced itself inside the registry TTL, by id.
    #: Empty when the store cannot answer, which the entities render as
    #: unknown rather than as a cluster of zero nodes.
    members: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def clock_skew(self) -> float | None:
        """Widest disagreement between any two members' clocks, in seconds.

        Each member records its own offset against the store's clock, so the
        skew between two of them is the difference of their offsets -- the
        round trip largely cancels, appearing in both with the same sign.

        None below two members: skew is a property of a pair, and a single
        node reporting 0 would claim agreement it has not established.
        """
        offsets = [
            m["offset_s"]
            for m in self.members.values()
            if isinstance(m.get("offset_s"), (int, float))
        ]
        if len(offsets) < 2:
            return None
        return max(offsets) - min(offsets)

    @property
    def snapshot_age(self) -> float | None:
        """Seconds since anyone last wrote the shared snapshot.

        None when nothing has ever been written, for the same reason
        `last_snapshot_age` returns None: zero would claim perfect freshness,
        which is the exact opposite of the truth for an empty store.
        """
        if self.snapshot_at is None:
            return None
        return (datetime.now(tz=UTC) - self.snapshot_at).total_seconds()


class ClusterViewCoordinator(DataUpdateCoordinator[ClusterView]):
    """Polls the shared store for cluster-wide facts.

    Deliberately separate from `BackendHealthCoordinator` rather than folded
    into it. That one's data is a bare `bool` and the Backend binary sensor
    reads it as one; widening it to a dataclass would make every truthiness
    check pass unconditionally, so a dead backend would start reporting itself
    healthy. Two cheap reads are worth more than that risk.
    """

    def __init__(self, hass: HomeAssistant, backend: ClusterBackend, node_id: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_cluster_view",
            update_interval=timedelta(seconds=HEALTH_POLL_INTERVAL),
        )
        self._backend = backend
        self._node_id = node_id

    async def _async_update_data(self) -> ClusterView:
        # Same posture as the health coordinator: never raise UpdateFailed. A
        # backend that cannot answer publishes an empty view, which the entities
        # render as unknown -- a reading, not a blanked-out entity.
        # Announce this node before reading, so a single-node cluster still
        # sees itself and a member list is never emptier than the truth.
        await self._backend.register_node(self._node_id)
        leader, meta = await self._backend.read_cluster_view()
        members = await self._backend.read_members()
        return ClusterView(
            leader=leader,
            snapshot_source=meta.get("source_node") or None,
            snapshot_at=_parse_iso(meta.get("last_snapshot_at")),
            entry_count=meta.get("entry_count")
            if isinstance(meta.get("entry_count"), int)
            else None,
            members=members,
        )


def _parse_iso(value: object) -> datetime | None:
    """Parse the meta's ISO timestamp, or give up quietly.

    Anything unparseable is treated as "no timestamp" rather than raised: the
    peer writes this field and a malformed one should cost us a single reading,
    not the whole poll.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
