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
    DATA_COORDINATOR,
    DATA_DEGRADED_MARKER,
    DATA_FILESET,
    DATA_MIRROR,
    DATA_STATS,
    DOMAIN,
)
from .coordinator import BackendHealthCoordinator, SyncStats
from .entity import ClusterSyncDiagnosticEntity, MirrorBackedEntity
from .fileset import FilesetPublisher


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the diagnostic sensors."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    coordinator: BackendHealthCoordinator = runtime[DATA_COORDINATOR]
    mirror: StateMirror = runtime[DATA_MIRROR]
    stats: SyncStats = runtime[DATA_STATS]
    publisher: FilesetPublisher | None = runtime.get(DATA_FILESET)
    degraded_marker: dict[str, Any] | None = runtime.get(DATA_DEGRADED_MARKER)

    async_add_entities(
        [
            EntitiesTrackedSensor(coordinator, entry, mirror),
            LastSnapshotAgeSensor(coordinator, entry, mirror),
            EntitiesRestoredSensor(coordinator, entry, stats),
            FilesetAgeSensor(coordinator, entry, degraded_marker, publisher),
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
