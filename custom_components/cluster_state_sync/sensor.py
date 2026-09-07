"""Diagnostic sensors for Cluster State Sync (AR-0032)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import fnmatch
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
from homeassistant.util import dt as dt_util

from . import StateMirror
from .const import (
    CLOCK_SKEW_CRITICAL_SECONDS,
    CLOCK_SKEW_WARN_SECONDS,
    CONF_RADIO_WATCH,
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


def watch_patterns(data: Mapping[str, Any]) -> list[str]:
    """The radio-liveness globs, with the blanks removed.

    The wizard field accepts free text, so a stray empty entry is ordinary. An
    empty glob matches nothing, and a watch list of nothing but blanks would
    create a sensor that reports `unknown` forever while looking configured --
    so blanks are dropped here, and a list that is only blanks turns the
    feature off rather than half-on.
    """
    return [p.strip() for p in (data.get(CONF_RADIO_WATCH) or []) if p and p.strip()]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the diagnostic sensors."""
    runtime = entry.runtime_data
    radio_watch = watch_patterns(entry.data)
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
            # Only when the operator has said what a radio looks like
            # here. An entity that watches nothing is worse than absent:
            # it reads as a working check.
            *([RadioSilenceSensor(coordinator, entry, radio_watch)] if radio_watch else []),
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


class RadioSilenceSensor(ClusterSyncDiagnosticEntity, SensorEntity):
    """Seconds since anything the operator called a radio was last heard from.

    The blind spot this closes: the D3 probe asks Home Assistant whether it is
    alive, and Home Assistant is perfectly capable of being alive while every
    radio behind it is dead. On this fleet a Zigbee daemon livelocked -- 78%
    CPU, no log output for 32 minutes, its healthcheck still reporting
    `healthy` -- and the only trace anywhere was one reconnect line. No
    failover fires for that, correctly, and nothing marked the cluster
    degraded either.

    So: the age of the freshest update across the watched set. Rising steadily
    means nothing has been heard.

    **Silence is not a fault on its own**, and this sensor does not pretend
    otherwise. A quiet house at 4am legitimately produces no RF for a long
    while. What it gives an operator is a number they can put a threshold on
    themselves, knowing their own traffic -- and a value that has stopped
    moving when it always used to is the signal worth chasing.

    Unknown, not zero, when nothing matches the configured globs: zero would
    read as "heard something just now", which is the opposite of the truth.

    🚨 **An `unknown` entity is not evidence of reception, and counting one is
    the bug this sensor shipped with.** Home Assistant stamps `last_reported`
    on an entity whose state is `unknown` exactly as it does on a real reading,
    so the first version reported a confident 60 s, 120 s, 191 s on this fleet
    while all 37 watched entities sat at `unknown` and the RFXtrx receivers had
    heard nothing for **thirty hours**. The number was Home Assistant's own
    state writes keeping time with themselves, and it read exactly like a
    healthy radio.

    So entities without a usable value are excluded, and the three cases an
    operator has to tell apart are separated in the `status` attribute:

    | `status`     | means                                                 |
    |--------------|-------------------------------------------------------|
    | `ok`         | at least one radio has reported; the number is real   |
    | `no_matches` | the globs match nothing -- a configuration problem    |
    | `no_reports` | entities matched, none has ever reported -- **deaf**  |

    `no_reports` is the loudest of the three and still reads `unknown` rather
    than a large number, because "never" has no age. Alert on the attribute,
    not only on the value.

    🚨 **The watch set must be ONE radio's signals.** Freshest-wins is right
    within a radio and wrong across radios: on the fleet this was built for,
    `sensor.*_rssi` would have swept in a WLED and two Sonoff Wi-Fi RSSI
    sensors alongside 39 RFXtrx ones, and a chatty Wi-Fi chip would have held
    the number at zero through a completely dead RFXtrx. Watching more looks
    safer and is the opposite. One radio per watch list.
    """

    _attr_translation_key = "radio_silence"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "s"

    def __init__(
        self,
        coordinator: BackendHealthCoordinator,
        entry: ConfigEntry,
        patterns: list[str],
    ) -> None:
        super().__init__(coordinator, entry, "radio_silence")
        self._patterns = patterns

    #: States that mean "this entity has no reading", not "this radio is
    #: quiet". Home Assistant stamps `last_reported` on these exactly as it
    #: does on a real value, so they must be excluded by state rather than
    #: trusted by timestamp.
    _NOT_A_READING = ("unknown", "unavailable")

    def _watched(self) -> tuple[int, list[float]]:
        """(entities matched, timestamps of those carrying a usable reading)."""
        matched = 0
        stamps: list[float] = []
        for state in self.hass.states.async_all():
            if not any(fnmatch.fnmatch(state.entity_id, p) for p in self._patterns):
                continue
            matched += 1
            if state.state in self._NOT_A_READING:
                continue
            # `last_reported`, not `last_changed`. The question is "did we
            # hear a packet", and a radio re-reporting the same RSSI is a
            # packet received -- `last_changed` does not move for it, so a
            # radio in continuous, healthy reception can look silent. On this
            # fleet the two happen to agree today (checked: 0 of 37 diverge,
            # 2026-09-07), which is exactly why it needed checking rather than
            # assuming. `getattr` because the field arrived in HA 2024.11.
            stamps.append((getattr(state, "last_reported", None) or state.last_changed).timestamp())
        return matched, stamps

    @property
    def native_value(self) -> int | None:
        if not self._patterns:
            return None
        _, stamps = self._watched()
        if not stamps:
            # Either the globs match nothing, or they match entities that have
            # never reported. Both are `unknown`: the value is an age, and
            # neither case has one. `status` tells them apart.
            return None
        return max(0, int(dt_util.utcnow().timestamp() - max(stamps)))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        matched, stamps = self._watched()
        if stamps:
            status = "ok"
        elif matched:
            status = "no_reports"
        else:
            status = "no_matches"
        return {
            "watching": self._patterns,
            "entities_matched": matched,
            "entities_reporting": len(stamps),
            "status": status,
        }
