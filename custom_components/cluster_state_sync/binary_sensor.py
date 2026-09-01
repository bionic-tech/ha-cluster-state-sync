"""Backend connectivity binary sensor for Cluster State Sync (AR-0019)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_COORDINATOR, DATA_DEGRADED_MARKER, DATA_FILESET, DOMAIN
from .coordinator import BackendHealthCoordinator
from .entity import ClusterSyncDiagnosticEntity
from .fileset import FilesetPublisher


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the backend connectivity and degraded-fileset sensors."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    coordinator: BackendHealthCoordinator = runtime[DATA_COORDINATOR]
    degraded = runtime.get(DATA_DEGRADED_MARKER) is not None
    async_add_entities(
        [
            BackendConnectivitySensor(coordinator, entry, "backend"),
            FilesetDegradedBinarySensor(coordinator, entry, degraded, runtime.get(DATA_FILESET)),
        ]
    )


class BackendConnectivitySensor(ClusterSyncDiagnosticEntity, BinarySensorEntity):
    """Whether the shared backend is reachable.

    AR-0019: `RedisBackend.health()` shipped in v0.1 and was never called by
    anything. The integration could sit failing every single write — by design,
    silently, because backend errors are swallowed so they cannot crash HA —
    and nothing anywhere said so. Swallowing the error is right; not reporting
    it is not.
    """

    _attr_translation_key = "backend"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data)

    @property
    def available(self) -> bool:
        # Always available: "the backend is down" is the reading, not a reason
        # to make the entity itself unavailable and hide the problem.
        return True


class FilesetDegradedBinarySensor(ClusterSyncDiagnosticEntity, BinarySensorEntity):
    """Whether this node's config fileset is degraded — inbound or outbound.

    Two independent sources, because there are two ways to be degraded and an
    operator has exactly one place to look:

    **The go-bag this node came up on** (the degraded marker). Decision D4: on
    a stale or missing go-bag the standby promotes anyway rather than refuse to
    start. Every HTTP health check on a node in that state comes back green —
    HA boots, `:8123` answers, the UI loads. This is a boot-time fact: the
    host-side swap script writes the marker before the container starts, and a
    clean swap removes it, so nothing changes the answer again until the next
    restart. `async_setup_entry` reads it once and hands the result down,
    rather than re-opening the file from a synchronous property, which would be
    blocking I/O on the event loop.

    **The go-bag this node is publishing** (the publisher's last refusal). Over
    the size cap, `FilesetPublisher` publishes nothing at all and returns
    `skipped_reason="too_large"`. Before this it did so in silence: no log, no
    issue, no entity. The leader looked healthy while the *standby's* go-bag
    quietly stopped ageing forward, and the first anyone heard of it was a
    `stale` marker at the next promotion — the moment it is least useful. A
    leader refusing to publish and a follower that came up on a bad go-bag are
    the same problem seen from the two ends of the same wire, so they are one
    entity with an attribute that says which.

    AR-0040 is why neither can be a log line: a defect of exactly this shape —
    something that quietly did not do its job — lived in this project for its
    entire life, and its only symptom was an INFO line nobody read.

    This is `CoordinatorEntity`-backed like every other diagnostic here, but
    its data has nothing to do with whether `BackendHealthCoordinator`'s own
    poll succeeded — inheriting `available` from it would let a coordinator
    failure for any reason hide the one entity whose entire job is to stay
    visible when something has already gone wrong. `BackendConnectivitySensor`
    above makes the same trade for the same reason.
    """

    _attr_translation_key = "fileset_degraded"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: BackendHealthCoordinator,
        entry: ConfigEntry,
        degraded: bool,
        publisher: FilesetPublisher | None = None,
    ) -> None:
        super().__init__(coordinator, entry, "fileset_degraded")
        self._degraded = degraded
        self._publisher = publisher

    @property
    def _publish_skipped_reason(self) -> str | None:
        result = self._publisher.last_result if self._publisher else None
        return result.skipped_reason if result else None

    @property
    def is_on(self) -> bool:
        return self._degraded or self._publish_skipped_reason is not None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Which of the two it is. `is_on` alone would send the operator to the
        host-side swap log for a problem that is entirely on this node's own
        publish path, or the other way round."""
        return {
            "boot_marker": self._degraded,
            "publish_skipped_reason": self._publish_skipped_reason,
        }

    @property
    def available(self) -> bool:
        return True
