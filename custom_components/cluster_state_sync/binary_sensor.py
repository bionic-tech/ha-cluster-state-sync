"""Backend connectivity binary sensor for Cluster State Sync (AR-0019)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .backend import default_node_id
from .const import (
    CONF_NODE_ID,
    DATA_CLUSTER_VIEW,
    DATA_COORDINATOR,
    DATA_DEGRADED_MARKER,
    DATA_FILESET,
)
from .coordinator import BackendHealthCoordinator, ClusterViewCoordinator
from .entity import ClusterSyncDiagnosticEntity, ClusterViewEntity
from .fileset import FilesetPublisher
from .hold import hold_path, read_hold


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the backend connectivity and degraded-fileset sensors."""
    runtime = entry.runtime_data
    coordinator: BackendHealthCoordinator = runtime[DATA_COORDINATOR]
    degraded = runtime.get(DATA_DEGRADED_MARKER) is not None
    async_add_entities(
        [
            BackendConnectivitySensor(coordinator, entry, "backend"),
            FilesetDegradedBinarySensor(coordinator, entry, degraded, runtime.get(DATA_FILESET)),
            IsLeaderBinarySensor(runtime[DATA_CLUSTER_VIEW], entry),
            MaintenanceHoldBinarySensor(coordinator, entry, hass.config.path()),
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


class IsLeaderBinarySensor(ClusterViewEntity, BinarySensorEntity):
    """Does THIS node hold the lease right now.

    The one entity you can put on a wall and read without thinking. `Cluster
    leader` tells you a node id, which you then have to recognise; this answers
    the question directly for the node you happen to be looking at.

    Compares the lease holder against this node's own `node_id` -- the same
    value the integration presents when it takes the lease, resolved the same
    way, so the two cannot drift. If both nodes report this as on, the
    identities have collided and you have two leaders: the failure this project
    has already lost a day to.
    """

    _attr_translation_key = "is_leader"

    def __init__(self, coordinator: ClusterViewCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "is_leader")
        cfg = {**entry.data, **entry.options}
        self._node_id = cfg.get(CONF_NODE_ID) or default_node_id()

    @property
    def is_on(self) -> bool | None:
        """None, not False, when we do not know.

        A backend we cannot reach makes every node look like a follower, and a
        cluster where both nodes read "not the leader" is indistinguishable from
        one that has genuinely lost its leader. Unknown says so.
        """
        view = self.coordinator.data
        if view is None or view.leader is None:
            return None
        return view.leader == self._node_id

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"node_id": self._node_id}


class MaintenanceHoldBinarySensor(ClusterSyncDiagnosticEntity, BinarySensorEntity):
    """Is failover currently suspended for planned work (hold.py).

    This entity exists because the hold's failure mode is silence. A cluster
    under a forgotten hold looks exactly like a healthy one -- both nodes
    green, the lease renewing, the go-bag current -- right up until the moment
    it needed to fail over and did not. Everything else here reports whether
    the cluster is working; this reports whether it is *allowed* to.

    The flag is read in an executor and cached, never on the event loop. Home
    Assistant flags a `read_text` on the loop as a blocking call, with a link
    inviting a bug report, and it is right to: this is a file on whatever
    `/config` happens to be, which on this fleet is a spinning disk.
    """

    _attr_translation_key = "maintenance_hold"
    _attr_device_class = BinarySensorDeviceClass.SAFETY

    def __init__(
        self,
        coordinator: BackendHealthCoordinator,
        entry: ConfigEntry,
        config_dir: str,
    ) -> None:
        super().__init__(coordinator, entry, "maintenance_hold")
        self._config_dir = config_dir
        self._held = False
        self._reason = ""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_refresh_hold()

    def _handle_coordinator_update(self) -> None:
        """Re-read the flag on each backend poll.

        Scheduled rather than awaited because this hook is sync. The cached
        value is what the properties below serve, so a poll in flight simply
        means the previous answer stands for one more cycle -- acceptable for a
        flag a human sets and clears minutes apart.
        """
        self.hass.async_create_task(self._async_refresh_hold())
        super()._handle_coordinator_update()

    async def _async_refresh_hold(self) -> None:
        held, reason = await self.hass.async_add_executor_job(read_hold, self._config_dir)
        if (held, reason) != (self._held, self._reason):
            self._held, self._reason = held, reason
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """True means failover is suspended.

        `SAFETY` device class, so on renders as "Unsafe" -- which is exactly
        right: a held cluster is one outage away from an incident, and the
        wording should not be reassuring.
        """
        return self._held

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {
            "reason": (self._reason or "no reason given") if self._held else None,
            "flag_file": str(hold_path(self._config_dir)),
        }
