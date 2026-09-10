"""What actually crosses, computed once and answered consistently everywhere.

Three things need this answer and they must never disagree: the filter that
decides what to publish, the diagnostic sensors that report the scope, and the
panel that renders it. Before this module the filter was the only one that
knew, and it knew by evaluating a precedence chain inline -- so the honest
answer to "what is replicated?" was "read `_should_track` and work it out".

**Two channels, and conflating them is the usual confusion.** This module
describes the *state snapshot*: live values, allowlisted by domain. It says
nothing about the *fileset*, which carries `.storage` wholesale (including
`core.entity_registry` and `core.device_registry`, so an entity's settings
cross even when its value does not). An operator asking "is my thermostat
replicated?" is usually asking about both, and the answer differs.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_EXCLUDE_DEVICES,
    CONF_EXCLUDE_ENTITIES,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    CONF_LEADERSHIP_ENTITY,
    DEFAULT_INCLUDE_DOMAINS,
)

_LOGGER = logging.getLogger(__name__)


def resolve_device_entities(hass: HomeAssistant, device_ids: list[str]) -> frozenset[str]:
    """Every entity id belonging to the named devices.

    Disabled entities are included deliberately. A disabled entity has no state
    to publish today, but it can be re-enabled at any time and the operator's
    instruction was about the *device* -- silently narrowing that to "the
    entities it happens to expose right now" is the kind of helpfulness that
    produces a replicated entity nobody asked for.

    A device id that no longer exists is skipped rather than raised on: devices
    are removed by integrations without warning, and a stale exclusion is an
    exclusion that has simply stopped mattering, not a broken configuration.
    """
    if not device_ids:
        return frozenset()
    registry = er.async_get(hass)
    resolved: set[str] = set()
    for device_id in device_ids:
        for entry in er.async_entries_for_device(
            registry, device_id, include_disabled_entities=True
        ):
            resolved.add(entry.entity_id)
    return frozenset(resolved)


def device_labels(hass: HomeAssistant, device_ids: list[str]) -> list[dict[str, Any]]:
    """Name the excluded devices, for a human reading the panel.

    A raw device id tells an operator nothing -- it is a 32-character hex
    string. The name is what they chose in the UI, so it is what they will
    recognise; the id travels alongside because the name is not unique and
    renaming must not make the exclusion unrecognisable.
    """
    registry = dr.async_get(hass)
    out: list[dict[str, Any]] = []
    for device_id in device_ids:
        device = registry.async_get(device_id)
        out.append(
            {
                "id": device_id,
                # `name_by_user` is what the operator typed, and it wins.
                "name": (device.name_by_user or device.name) if device else None,
                # A device that has gone away is worth showing rather than
                # hiding: the exclusion is still in the config and still
                # counts, and an operator tidying up should be able to see it.
                "present": device is not None,
            }
        )
    return out


def replication_scope(
    hass: HomeAssistant, cfg: dict[str, Any], excluded_ids: frozenset[str] | None = None
) -> dict[str, Any]:
    """The whole picture, in the order the filter actually applies it.

    Returned as plain data so a sensor can publish it as attributes and the
    panel can render it without recomputing anything -- which is what keeps
    the three consumers from drifting apart.
    """
    domains = list(cfg.get(CONF_INCLUDE_DOMAINS) or DEFAULT_INCLUDE_DOMAINS)
    exclude_entities = list(cfg.get(CONF_EXCLUDE_ENTITIES) or [])
    exclude_devices = list(cfg.get(CONF_EXCLUDE_DEVICES) or [])
    include_entities = list(cfg.get(CONF_INCLUDE_ENTITIES) or [])
    if excluded_ids is None:
        excluded_ids = resolve_device_entities(hass, exclude_devices)

    present = {state.domain for state in hass.states.async_all()}
    leadership_entity = cfg.get(CONF_LEADERSHIP_ENTITY)

    return {
        "replicated_domains": sorted(domains),
        # Only domains this instance ACTUALLY HAS. A list of every domain in
        # Home Assistant would be noise; the useful question is "what do I run
        # that is not crossing?".
        "not_replicated_domains": sorted(present - set(domains)),
        "also_replicated_entities": sorted(include_entities),
        "excluded_entities": sorted(exclude_entities),
        "excluded_devices": device_labels(hass, exclude_devices),
        "entities_excluded_by_device": sorted(excluded_ids),
        # AR-0038. Refused ahead of every list and not configurable, so it is
        # reported separately rather than folded into the exclusions an
        # operator chose.
        "leadership_entity_refused": leadership_entity or None,
    }
