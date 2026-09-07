"""Put this integration's device in an area of its own.

Most of what this integration creates is `EntityCategory.DIAGNOSTIC`, which
Home Assistant keeps off auto-generated dashboards. The two switches are
deliberately NOT diagnostic -- a control buried in a device's diagnostics
section is a control nobody finds, and that is exactly how the maintenance hold
went unused through two outages -- so they surface on the default dashboard,
loose and unexplained among the lamps.

An area fixes that properly. Assigning the DEVICE is what does the work:
entities inherit their device's area, so one assignment covers every sensor and
switch, now and whenever more are added.

**It never overrides a choice.** If the device already has an area, this does
nothing at all -- an operator who filed it under "Loft" or "Servers" meant it,
and an integration that quietly moved things back on every restart would be
worse than one that never helped.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

#: Named for what it holds rather than for this integration: an operator
#: reading a device list wants to know it is cluster infrastructure, not which
#: custom component created it.
AREA_NAME = "Cluster"
AREA_ICON = "mdi:server-network"


async def async_assign_area(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """Ensure the device sits in the Cluster area. Never raises.

    Returns the area id when this call assigned one, and None when it did not
    -- because the device already had an area, because the device does not
    exist yet, or because something went wrong. Tidying a device list must
    never be able to stop the cluster starting, so every failure is logged and
    swallowed.
    """
    try:
        devices = dr.async_get(hass)
        device = devices.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
        if device is None:
            # Platforms have not created it yet. Not an error: the caller runs
            # after `async_forward_entry_setups`, and a missing device simply
            # means there is nothing to file.
            return None
        if device.area_id is not None:
            # Deliberate: see the module docstring. Their choice wins.
            return None

        areas = ar.async_get(hass)
        area = areas.async_get_area_by_name(AREA_NAME)
        if area is None:
            area = areas.async_create(AREA_NAME, icon=AREA_ICON)

        devices.async_update_device(device.id, area_id=area.id)
        _LOGGER.debug("Filed the cluster device under the %r area", AREA_NAME)
        return area.id
    except Exception:  # noqa: BLE001 -- tidying must never break setup
        _LOGGER.debug("Could not assign the %r area", AREA_NAME, exc_info=True)
        return None
