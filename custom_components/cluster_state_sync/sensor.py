"""Diagnostic sensors for Cluster State Sync (AR-0032)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import StateMirror
from .const import (
    CLOCK_SKEW_CRITICAL_SECONDS,
    CLOCK_SKEW_WARN_SECONDS,
    DATA_CLUSTER_VIEW,
    DATA_COORDINATOR,
    DATA_DEGRADED_MARKER,
    DATA_FILESET,
    DATA_MIRROR,
    DATA_STATS,
)
from .coordinator import BackendHealthCoordinator, ClusterViewCoordinator, SyncStats
from .entity import ClusterSyncDiagnosticEntity, ClusterViewEntity, MirrorBackedEntity
from .fileset import REPLICATED_DIRS, REPLICATED_FILES, FilesetPublisher
from .includes import scan as scan_includes


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the diagnostic sensors."""
    runtime = entry.runtime_data
    coordinator: BackendHealthCoordinator = runtime[DATA_COORDINATOR]
    mirror: StateMirror = runtime[DATA_MIRROR]
    stats: SyncStats = runtime[DATA_STATS]
    publisher: FilesetPublisher | None = runtime.get(DATA_FILESET)
    degraded_marker: dict[str, Any] | None = runtime.get(DATA_DEGRADED_MARKER)
    cluster_view: ClusterViewCoordinator = runtime[DATA_CLUSTER_VIEW]

    async_add_entities(
        [
            EntitiesTrackedSensor(coordinator, entry, mirror),
            LastSnapshotAgeSensor(coordinator, entry, mirror),
            EntitiesRestoredSensor(coordinator, entry, stats),
            FilesetAgeSensor(coordinator, entry, degraded_marker, publisher),
            ClusterLeaderSensor(cluster_view, entry),
            SharedSnapshotAgeSensor(cluster_view, entry),
            ClusterMembersSensor(cluster_view, entry),
            ClockSkewSensor(cluster_view, entry),
            UnreplicatedReferencesSensor(coordinator, entry, hass.config.path()),
        ]
    )


class EntitiesTrackedSensor(MirrorBackedEntity, SensorEntity):
    """How many entities this node is mirroring.

    This is the sensor that would have caught AR-0001. The snapshot was a
    five-second delta, so the number actually reaching the backend was a
    handful while the tracked set was hundreds — obvious at a glance, invisible
    without one.
    """

    _attr_translation_key = "entities_tracked"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "entities"

    def __init__(
        self,
        coordinator: BackendHealthCoordinator,
        entry: ConfigEntry,
        mirror: StateMirror,
    ) -> None:
        super().__init__(coordinator, entry, "entities_tracked", mirror)

    @property
    def native_value(self) -> int:
        return self._mirror.tracked_count


class LastSnapshotAgeSensor(MirrorBackedEntity, SensorEntity):
    """Seconds since the last snapshot was successfully written.

    A stalled flush loop or an unreachable backend both show up here as a
    number that climbs and never resets — the signal the review wanted, since a
    silently-failing sync is indistinguishable from a healthy one otherwise.
    """

    _attr_translation_key = "last_snapshot_age"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        coordinator: BackendHealthCoordinator,
        entry: ConfigEntry,
        mirror: StateMirror,
    ) -> None:
        super().__init__(coordinator, entry, "last_snapshot_age", mirror)

    @property
    def native_value(self) -> float | None:
        """Age in seconds, or None if nothing has ever been written.

        Returning None rather than 0 matters: zero would claim the snapshot is
        perfectly fresh, which is the exact opposite of the truth for a node
        that has never managed a single write.
        """
        last = self._mirror.last_successful_flush
        if last is None:
            return None
        return (datetime.now(tz=UTC) - last).total_seconds()


class EntitiesRestoredSensor(ClusterSyncDiagnosticEntity, SensorEntity):
    """How many entities the last restore actually seeded.

    A promotion that restored zero is the failure this whole integration exists
    to prevent, and it is otherwise completely silent.
    """

    _attr_translation_key = "entities_restored"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "entities"

    def __init__(
        self,
        coordinator: BackendHealthCoordinator,
        entry: ConfigEntry,
        stats: SyncStats,
    ) -> None:
        super().__init__(coordinator, entry, "entities_restored")
        self._stats = stats

    @property
    def native_value(self) -> int:
        return self._stats.restored_count

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        last = self._stats.last_restore_at
        return {"last_restore_at": last.isoformat() if last else None}


class FilesetAgeSensor(ClusterSyncDiagnosticEntity, SensorEntity):
    """Seconds since the config fileset now in place was last known fresh.

    Decision D4 has the standby promote on a stale or missing go-bag rather
    than refuse to start, which makes the degraded marker (see
    `FilesetDegradedBinarySensor`) and this sensor the only safety net.
    AR-0040 already showed what happens to a signal like that when it is left
    as a log line: it lived unread for the whole life of the project.

    Two sources, read once at setup and never re-opened from a property,
    because the two nodes in a pair learn freshness differently:

    * A degraded marker records the age the host-side swap measured *at the
      moment it wrote the file*, before this container even started. Real
      time keeps passing after that, so this keeps climbing from that
      baseline rather than freezing at the boot-time value — freezing would
      repeat, for the fileset, exactly the reassuring-but-wrong number
      `LastSnapshotAgeSensor` exists to avoid for the state hash.
    * With no marker, a node running a `FilesetPublisher` reports elapsed
      time since `last_success_at` — the same "real timestamp that keeps
      climbing" shape as `LastSnapshotAgeSensor`, and for the same reason: a
      publish loop that silently stops after one success is this project's
      own history, and a value frozen at a reassuring zero would hide exactly
      that.

    Absent both, there is genuinely nothing to report, and this is unknown
    rather than zero, for the same reason `last_snapshot_age` is unknown
    before the first flush: zero would claim freshness that was never earned.
    """

    _attr_translation_key = "fileset_age"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        coordinator: BackendHealthCoordinator,
        entry: ConfigEntry,
        degraded_marker: dict[str, Any] | None,
        publisher: FilesetPublisher | None,
    ) -> None:
        super().__init__(coordinator, entry, "fileset_age")
        self._marker = degraded_marker
        self._publisher = publisher

    @property
    def native_value(self) -> float | None:
        if self._marker is not None:
            age = self._marker.get("age_s")
            if age is None:
                # e.g. "no_staged_fileset" — nothing existed to measure the
                # age of, so there is no honest number to report.
                return None
            return float(age) + self._seconds_since_marker_written()

        if self._publisher is not None and self._publisher.last_success_at is not None:
            return (datetime.now(tz=UTC) - self._publisher.last_success_at).total_seconds()

        return None

    def _seconds_since_marker_written(self) -> float:
        """How long ago the marker itself was written, so the reported age
        keeps climbing instead of freezing at the value captured at boot."""
        written = self._marker.get("at") if self._marker else None
        if not isinstance(written, str):
            return 0.0
        try:
            parsed = datetime.fromisoformat(written)
        except ValueError:
            return 0.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (datetime.now(tz=UTC) - parsed).total_seconds())


class ClusterLeaderSensor(ClusterViewEntity, SensorEntity):
    """Which node currently holds the lease.

    Reads the same on both nodes, which is the point: open it on each and they
    must agree. Two nodes each naming themselves is a split brain, and it is far
    easier to see here than by comparing log lines at 2am.

    A value of `None` is genuinely ambiguous and worth knowing about. Read it
    together with the Backend binary sensor: Backend off means "we could not
    ask", Backend on means "we asked and nobody holds it" -- which is normal for
    a few seconds after a leader releases, and a real problem if it persists.
    """

    _attr_translation_key = "cluster_leader"

    def __init__(self, coordinator: ClusterViewCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "cluster_leader")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.leader if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        view = self.coordinator.data
        if view is None:
            return {}
        # Which node wrote the snapshot, as opposed to which one leads now.
        # They differ for exactly as long as it takes a new leader to flush, and
        # a lasting difference means the leader is not writing.
        return {"snapshot_source": view.snapshot_source, "entry_count": view.entry_count}


class SharedSnapshotAgeSensor(ClusterViewEntity, SensorEntity):
    """How old the snapshot in the shared store is, whoever wrote it.

    Not the same question as `Last snapshot age`, which measures this node's own
    last successful flush. On a standby that never flushes by design, that one
    climbs forever and says nothing about whether the cluster is healthy. This
    one is read from the store, so it reads identically on both nodes and is the
    number that actually answers "how much would we lose if we promoted now".
    """

    _attr_translation_key = "shared_snapshot_age"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: ClusterViewCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "shared_snapshot_age")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.snapshot_age if self.coordinator.data else None


class ClusterMembersSensor(ClusterViewEntity, SensorEntity):
    """How many nodes are in this cluster right now.

    Nothing counted them before. Leadership was always answerable — one key,
    one holder — but "who else is here" had no answer at all, so a node that
    should not be in this namespace joined silently and began pulling the
    cluster's `.storage`. The most plausible version is not malicious: a
    restored backup on a test box, still pointed at `prod`.

    A registry does not prevent that. It makes it visible, which is the part
    that was missing — and a hard cap on members would not have caught it
    either, because the stray node is not the one running the wizard.

    Unknown, not zero, when the store cannot answer: a cluster of no nodes is
    not a reading anyone should be shown.
    """

    _attr_translation_key = "cluster_members"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "nodes"

    def __init__(self, coordinator: ClusterViewCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "cluster_members")

    @property
    def native_value(self) -> int | None:
        view = self.coordinator.data
        if view is None or not view.members:
            return None
        return len(view.members)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        view = self.coordinator.data
        if view is None:
            return {}
        return {
            "node_ids": sorted(view.members),
            "clock_offsets_s": {
                node: m.get("offset_s") for node, m in sorted(view.members.items())
            },
        }


class ClockSkewSensor(ClusterViewEntity, SensorEntity):
    """Widest disagreement between any two nodes' clocks.

    Not a diagnostic nicety: skew silently disables the restore, in both
    directions, and the symptom is this project's founding incident — "restored
    0 entities" and a polite log line.

    `_restore_from_snapshot` compares a **peer-written** `last_updated` against
    **local** `now` three times. A peer more than `CLOCK_SKEW_TOLERANCE` ahead
    has every entry refused as `skipped_future`; a peer more than
    `restore_max_age` behind has every entry skipped as too old. Neither
    corrupts anything. Both produce a restore that does nothing while every
    other entity here reads green.

    The lease is immune, because Valkey expires it server-side. So leadership
    stays correct while the thing leadership exists to protect quietly stops
    working, and that asymmetry is the whole argument for measuring this.

    Measured against the store's clock rather than the peer's — see
    `RedisBackend.register_node` for why that cancels most of the round trip.
    """

    _attr_translation_key = "clock_skew"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: ClusterViewCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "clock_skew")

    @property
    def native_value(self) -> float | None:
        view = self.coordinator.data
        return view.clock_skew if view is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        skew = self.native_value
        if skew is None:
            return {"status": "unknown", "warn_above_s": CLOCK_SKEW_WARN_SECONDS}
        if skew >= CLOCK_SKEW_CRITICAL_SECONDS:
            status = "critical — the restore is refusing the peer's entries"
        elif skew >= CLOCK_SKEW_WARN_SECONDS:
            status = "warning — drifting toward the restore's cliff"
        else:
            status = "ok"
        return {
            "status": status,
            "warn_above_s": CLOCK_SKEW_WARN_SECONDS,
            "breaks_restore_above_s": CLOCK_SKEW_CRITICAL_SECONDS,
        }


class UnreplicatedReferencesSensor(ClusterSyncDiagnosticEntity, SensorEntity):
    """How many files the configuration needs that the go-bag will not carry.

    This exists because the failure it measures is invisible. On 2026-09-04 a
    promotion succeeded in every observable way -- lease taken in 12 seconds,
    `.storage` swapped, the right identity, 20 refresh tokens, the mobile app
    connected -- and Home Assistant came up in recovery mode, because twelve
    referenced files had never been replicated. Nothing anywhere said so until
    the house was the test.

    Zero is the healthy reading. Anything above it is the number of files a
    promoted standby would be missing, and the attributes name them.

    Counts only what a parser can see. `python_scripts/`, `custom_templates/`,
    ZHA's `zigbee.db` and anything an integration opens by path at runtime are
    invisible to it -- so zero here means "nothing *referenced* is missing",
    not "a promotion is guaranteed complete".
    """

    _attr_translation_key = "unreplicated_references"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "files"

    def __init__(
        self,
        coordinator: BackendHealthCoordinator,
        entry: ConfigEntry,
        config_dir: str,
    ) -> None:
        super().__init__(coordinator, entry, "unreplicated_references")
        self._config_dir = config_dir
        self._count: int | None = None
        self._detail: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_rescan()

    def _handle_coordinator_update(self) -> None:
        self.hass.async_create_task(self._async_rescan())
        super()._handle_coordinator_update()

    async def _async_rescan(self) -> None:
        """Walk the configuration in an executor -- it reads many files."""
        try:
            result = await self.hass.async_add_executor_job(scan_includes, self._config_dir)
        except Exception:  # noqa: BLE001 - a diagnostic must never break setup
            return
        gap = sorted(p for p in result.paths if not _is_replicated(p))
        detail: dict[str, Any] = {"missing_from_go_bag": gap}
        if result.outside:
            detail["outside_config_dir"] = [t for _, t in result.outside]
        if result.missing:
            detail["referenced_but_absent"] = [t for _, t in result.missing]
        if result.unreadable:
            detail["unreadable"] = [f for f, _ in result.unreadable]
        if (len(gap), detail) != (self._count, self._detail):
            self._count, self._detail = len(gap), detail
            self.async_write_ha_state()

    @property
    def native_value(self) -> int | None:
        return self._count

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._detail


def _is_replicated(rel: str) -> bool:
    """Would the go-bag already carry this path?"""
    return rel.split("/")[0] in REPLICATED_DIRS or rel in REPLICATED_FILES
