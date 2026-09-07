"""Shared base for the diagnostic entities."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import StateMirror
from .const import DOMAIN
from .coordinator import BackendHealthCoordinator, ClusterViewCoordinator


class ClusterSyncDiagnosticEntity(CoordinatorEntity[BackendHealthCoordinator]):
    """Common wiring for every diagnostic entity this integration exposes.

    All of these are `EntityCategory.DIAGNOSTIC`: they describe the health of
    the replication itself, not anything in the house, so they belong in the
    device's diagnostics section rather than on a dashboard.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: BackendHealthCoordinator,
        entry: ConfigEntry,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Cluster State Sync",
            entry_type=None,
        )


class MirrorBackedEntity(ClusterSyncDiagnosticEntity):
    """A diagnostic entity whose value comes from the state mirror.

    The health coordinator polls once a minute, which is the right cadence for
    "is Valkey up" and far too slow for "how many entities am I tracking".
    These entities subscribe to the mirror instead, so they move when the thing
    they describe moves.
    """

    def __init__(
        self,
        coordinator: BackendHealthCoordinator,
        entry: ConfigEntry,
        key: str,
        mirror: StateMirror,
    ) -> None:
        super().__init__(coordinator, entry, key)
        self._mirror = mirror

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._mirror.add_listener(self.async_write_ha_state))


class ClusterViewEntity(CoordinatorEntity[ClusterViewCoordinator]):
    """A diagnostic entity whose value comes from the shared store.

    Separate from `ClusterSyncDiagnosticEntity` only because it rides a
    different coordinator. Everything else -- the device it attaches to, the
    diagnostic category, the unique-id convention -- is deliberately identical,
    so the two families sit together on one device rather than looking like two
    integrations.

    These are the entities that read the same on both nodes. That is the point
    of them: comparing a leader's view with a standby's is how you see a split
    brain, and you cannot compare two numbers that mean different things on each
    side.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: ClusterViewCoordinator,
        entry: ConfigEntry,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Cluster State Sync",
            entry_type=None,
        )
