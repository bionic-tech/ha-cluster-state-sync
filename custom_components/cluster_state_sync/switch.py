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

from .backend import default_node_id
from .const import CONF_NODE_ID, DATA_BACKEND, DOMAIN, INTEGRATION_VERSION
from .handover import UI_HANDOVER_REASON, clear_request, is_requested, request_handover
from .hold import (
    UI_HOLD_REASON,
    clear_hold,
    hold_body,
    hold_origin,
    hold_text,
    is_held,
    reason_of,
    set_hold,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the maintenance hold switch."""
    runtime = entry.runtime_data
    cfg = {**entry.data, **entry.options}
    async_add_entities(
        [
            MaintenanceHoldSwitch(
                entry,
                hass.config.path(),
                runtime.get(DATA_BACKEND),
                cfg.get(CONF_NODE_ID) or default_node_id(),
            ),
            HandoverRequestSwitch(entry, hass.config.path()),
        ]
    )


class MaintenanceHoldSwitch(SwitchEntity):
    """On = failover suspended, across the whole cluster.

    State is read from the flag file and the shared key rather than
    remembered, because the host script and the peer both write them. An
    operator running `cluster-hold.sh off` at the console must not leave this
    switch showing `on` -- two controls over one flag that disagree are worse
    than one control.

    🚨 **This sets BOTH halves, and that is the entire point.** Until 0.6.0 it
    wrote only the local file, which in the cold model could never reach the
    peer -- the standby's Home Assistant is deliberately stopped, so this
    dashboard is the only one that exists, and a hold set here left the
    standby's promoter free to promote. It did (GOTCHAS 29). The local file
    stays because it keeps working when the backend does not; the shared key
    is what makes the hold mean what the switch has always claimed it meant.
    """

    _attr_has_entity_name = True
    _attr_name = "Maintenance hold"
    _attr_icon = "mdi:pause-octagon"
    # No entity_category: this is a control, not diagnostics. See module docstring.

    def __init__(
        self,
        entry: ConfigEntry,
        config_dir: str,
        backend: Any | None = None,
        node_id: str = "",
    ) -> None:
        self._entry = entry
        self._config_dir = config_dir
        self._backend = backend
        self._node_id = node_id
        self._attr_unique_id = f"{entry.entry_id}_maintenance_hold_switch"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Cluster State Sync",
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"reason": self._reason, "scope": self._scope, "set_by": self._origin}

    async def async_update(self) -> None:
        """Re-read both halves: the flag file, and the cluster-wide key.

        The file read goes to an executor -- it is blocking I/O and must not
        happen on the event loop. The backend read is already async.

        Cluster scope is reported in preference to local when both are set,
        because it is the one that binds the peer, and "who do I go to in
        order to clear this" is the question the attributes exist to answer.
        """
        local_text = await self.hass.async_add_executor_job(hold_text, self._config_dir)
        local = await self.hass.async_add_executor_job(is_held, self._config_dir)
        shared = await self._backend.read_cluster_hold() if self._backend is not None else None

        self._attr_is_on = local or shared is not None
        if shared is not None:
            self._scope = "cluster"
            self._reason = reason_of(shared)
            self._origin = hold_origin(shared) or "unknown"
        elif local:
            self._scope = "local"
            self._reason = reason_of(local_text)
            self._origin = hold_origin(local_text) or "unknown"
        else:
            self._scope = "none"
            self._reason = ""
            self._origin = ""

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Suspend failover across the cluster.

        Local file first, then the shared key. That order matters: if the
        second write fails, this node is still held, which is strictly safer
        than a node that believes it published a hold and holds nothing.
        """
        await self.hass.async_add_executor_job(
            set_hold, self._config_dir, UI_HOLD_REASON, self._node_id
        )
        published = False
        if self._backend is not None:
            published = await self._backend.publish_cluster_hold(
                hold_body(UI_HOLD_REASON, origin=self._node_id)
            )
        stale = await self._async_stale_bundles()
        if published and not stale:
            _LOGGER.warning(
                "Maintenance hold RAISED from the dashboard — failover is suspended on "
                "EVERY node until it is cleared. Home Assistant can now be restarted, or "
                "this machine rebooted, without the peer taking over."
            )
        elif published and stale:
            # 🚨 Published, and IGNORED where it matters. HACS upgrades the
            # integration and not the host bundle, so a promoter still on
            # 0.5.x never reads this key — it is not merely un-upgraded, it
            # will promote straight through the hold.
            #
            # Claiming EVERY node here would tell the operator the cluster is
            # held when it is not, which is precisely the accident of
            # GOTCHAS 29 recreated by its own fix. So say what is true.
            _LOGGER.error(
                "Maintenance hold published, but %s still running a host bundle from "
                "before 0.6.0 — those promoters do NOT read the cluster-wide hold and "
                "WILL promote through it. This node is held; they are not. Regenerate "
                "the bundle and re-run install.sh there, or set the hold on each of "
                "them by hand before rebooting anything.",
                ", ".join(sorted(stale)),
            )
        else:
            # 🚨 Never silent. An operator told "hold set" who then reboots the
            # leader gets the exact accident the hold exists to prevent, and
            # this is the only moment they can still be warned.
            _LOGGER.error(
                "Maintenance hold raised on THIS NODE ONLY — it could not be published to "
                "the cluster, so the peer is NOT held and may still promote. Set the hold "
                "on the peer as well (`cluster-hold.sh on`) before rebooting anything."
            )
        await self.async_update()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Resume failover across the cluster.

        Clears the shared key whoever raised it: the hold is a property of the
        cluster, not a claim by one node, so an operator at the one running
        dashboard must be able to release a hold the peer set.
        """
        await self.hass.async_add_executor_job(clear_hold, self._config_dir)
        cleared = True
        if self._backend is not None:
            cleared = await self._backend.clear_cluster_hold()
        if cleared:
            _LOGGER.warning(
                "Maintenance hold cleared from the dashboard — failover is live again."
            )
        else:
            _LOGGER.error(
                "Maintenance hold cleared on THIS NODE ONLY — the cluster-wide hold could "
                "not be removed, so the peer may still be holding. Clear it there too."
            )
        await self.async_update()
        self.async_write_ha_state()

    async def _async_stale_bundles(self) -> set[str]:
        """Nodes whose promoter predates 0.6.0 and cannot see a cluster hold.

        Detected from the heartbeat payload shape, which is the only evidence
        available: a promoter on 0.5.x writes a bare timestamp, one on 0.6.0
        writes JSON. Nothing else distinguishes them, and the difference
        decides whether the hold this switch just published binds anything.
        """
        if self._backend is None:
            return set()
        try:
            promoters = await self._backend.read_promoters()
        except Exception:  # noqa: BLE001 — a silent store is not a stale bundle
            return set()
        stale: set[str] = set()
        for node, entry in promoters.items():
            if entry.get("legacy_bundle"):
                stale.add(node)  # predates version stamping entirely
                continue
            stamped = entry.get("bundle_version")
            # Only an explicit DISAGREEMENT counts. A blank stamp is a bundle
            # from a version that did not stamp yet, already caught above; an
            # unknown integration version must never accuse a correct install.
            if stamped and INTEGRATION_VERSION and stamped != INTEGRATION_VERSION:
                stale.add(node)
        return stale

    _reason: str = ""
    _scope: str = "none"
    _origin: str = ""


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
