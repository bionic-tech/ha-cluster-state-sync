"""The maintenance hold, as something you can actually reach.

Restarting Home Assistant on the leader is a full failover: the D3 probe fails,
the promoter releases the lease, and `notify_backup.sh` stops the container.
`unless-stopped` never restarts an explicitly stopped container, so the node
stays down and the peer keeps the cluster -- without its radios, if it has
none. That has now happened on this fleet more than once, most recently to an
operator who simply pressed Restart in the UI.

The hold has existed the whole time and lived only in a host script the person
pressing Restart was not looking at. Same flag file, same semantics, reachable
from the place the mistake gets made.

Deliberately NOT a diagnostic entity: it is a control, it belongs on a
dashboard, and hiding it under the device's diagnostics section is how it stays
unfound.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .handover import UI_HANDOVER_REASON, clear_request, is_requested, request_handover
from .hold import UI_HOLD_REASON, clear_hold, hold_reason, is_held, set_hold

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the maintenance hold switch."""
    async_add_entities(
        [
            MaintenanceHoldSwitch(entry, hass.config.path()),
            HandoverRequestSwitch(entry, hass.config.path()),
        ]
    )


class MaintenanceHoldSwitch(SwitchEntity):
    """On = failover suspended on this node.

    State is read from the flag file rather than remembered, because the host
    script writes the same file. An operator running `cluster-hold.sh off` at
    the console must not leave this switch showing `on` -- two controls over
    one flag that disagree are worse than one control.
    """

    _attr_has_entity_name = True
    _attr_name = "Maintenance hold"
    _attr_icon = "mdi:pause-octagon"
    # No entity_category: this is a control, not diagnostics. See module docstring.

    def __init__(self, entry: ConfigEntry, config_dir: str) -> None:
        self._entry = entry
        self._config_dir = config_dir
        self._attr_unique_id = f"{entry.entry_id}_maintenance_hold_switch"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Cluster State Sync",
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"reason": self._reason}

    async def async_update(self) -> None:
        """Re-read the flag from disk, in an executor.

        `is_held` opens a file, which is blocking I/O and must not happen on
        the event loop.
        """
        self._attr_is_on = await self.hass.async_add_executor_job(is_held, self._config_dir)
        self._reason = await self.hass.async_add_executor_job(hold_reason, self._config_dir)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Suspend failover on this node."""
        await self.hass.async_add_executor_job(set_hold, self._config_dir, UI_HOLD_REASON)
        _LOGGER.warning(
            "Maintenance hold RAISED from the dashboard — failover is suspended on this "
            "node until it is cleared. Home Assistant can now be restarted without the "
            "peer taking over."
        )
        await self.async_update()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Resume failover on this node."""
        await self.hass.async_add_executor_job(clear_hold, self._config_dir)
        _LOGGER.warning("Maintenance hold cleared from the dashboard — failover is live again.")
        await self.async_update()
        self.async_write_ha_state()

    _reason: str = ""


class HandoverRequestSwitch(SwitchEntity):
    """Turning this on asks this node to give the cluster to its peer.

    The hold answers "do not fail over". This answers "fail over now", and
    until it existed the only ways to move the cluster deliberately were to
    stop Home Assistant on the leader -- which caused a real outage on
    2026-09-06, because a stopped leader is a leader whose container
    `notify_backup.sh` will not restart -- or to write force-master on the peer
    by hand at a console.

    Modelled as a switch rather than a button because the request is a state
    that exists until the promoter consumes it, typically within one ~11s tick.
    Watching it flip back off is how an operator knows the promoter saw it,
    rather than that the file was merely written.
    """

    _attr_has_entity_name = True
    _attr_name = "Hand over to peer"
    _attr_icon = "mdi:swap-horizontal-bold"
    # A control, like the hold. See MaintenanceHoldSwitch.

    def __init__(self, entry: ConfigEntry, config_dir: str) -> None:
        self._entry = entry
        self._config_dir = config_dir
        self._attr_unique_id = f"{entry.entry_id}_handover_request_switch"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Cluster State Sync",
        )

    async def async_update(self) -> None:
        self._attr_is_on = await self.hass.async_add_executor_job(is_requested, self._config_dir)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Ask the promoter to hand the cluster over."""
        await self.hass.async_add_executor_job(
            request_handover, self._config_dir, UI_HANDOVER_REASON
        )
        _LOGGER.warning(
            "HANDOVER REQUESTED from the dashboard. If this node holds the lease it "
            "will release it within one promoter tick, stop Home Assistant here, and "
            "the peer will promote. On a node that is not the leader this does "
            "nothing -- a follower has no lease to give away."
        )
        await self.async_update()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Withdraw a request the promoter has not yet acted on."""
        await self.hass.async_add_executor_job(clear_request, self._config_dir)
        _LOGGER.warning("Handover request withdrawn from the dashboard.")
        await self.async_update()
        self.async_write_ha_state()
