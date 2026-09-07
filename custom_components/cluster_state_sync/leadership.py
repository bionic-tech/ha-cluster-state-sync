"""Leadership resolution (AR-0017).

ADR-001 makes leadership the single source of truth for four gating layers:
the nftables ruleset, this integration's flush loop, recorder writes, and
automations. The integration is an unprivileged container, so it can only
enforce layer 2 — but layer 2 is the one that protects the shared snapshot,
and it is the one AR-0017 is about.

Three sources, matching ADR-001's wizard step 3:

* ``always``  — this node always writes. The pre-AR-0017 behaviour, and the
  right answer for a single-node install or a cold-standby pair where the
  follower's Home Assistant is not running at all.
* ``entity``  — follow a Home Assistant entity, typically an ``input_boolean``
  that Keepalived's ``notify_master``/``notify_backup`` scripts toggle.
* ``lease``   — take a TTL lease in Valkey itself. The strongest option and
  the split-brain guard ADR-001 leans on: VRRP alone can dual-master under a
  partition, and the lease is the tiebreak that keeps the second node off the
  shared hash.

**Everything here fails closed.** An unreadable signal, a missing entity, a
backend that will not answer — all resolve to "not leader". Assuming leadership
when you cannot establish it is precisely how you end up with two of them.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .backend import ClusterBackend
from .const import (
    LEADERSHIP_ALWAYS,
    LEADERSHIP_ENTITY,
    LEADERSHIP_LEASE,
)
from .hold import is_held

_LOGGER = logging.getLogger(__name__)

# States an entity may hold that mean "yes, this node is the leader".
_TRUTHY_STATES = frozenset({"on", "true", "yes", "master", "leader", "active"})


class LeadershipMonitor:
    """Answers one question: may this node write to the shared snapshot?"""

    def __init__(
        self,
        hass: HomeAssistant,
        backend: ClusterBackend,
        node_id: str,
        source: str,
        entity_id: str | None = None,
        config_dir: str | None = None,
    ) -> None:
        self._hass = hass
        self._backend = backend
        self._node_id = node_id
        self._source = source
        self._entity_id = entity_id
        self._last_known: bool | None = None
        # Where to look for the maintenance hold. None disables the check
        # entirely, which is what every existing test and the `always` and
        # `entity` sources want: the hold only has meaning for the lease.
        self._config_dir = config_dir

    async def async_is_leader(self) -> bool:
        """Resolve leadership, logging transitions but not steady state."""
        is_leader = await self._resolve()
        if is_leader != self._last_known:
            _LOGGER.info(
                "Leadership change: this node (%s) is now %s (source=%s)",
                self._node_id,
                "LEADER — publishing state" if is_leader else "FOLLOWER — not writing",
                self._source,
            )
            self._last_known = is_leader
        return is_leader

    async def _resolve(self) -> bool:
        if self._source == LEADERSHIP_ALWAYS:
            return True
        if self._source == LEADERSHIP_ENTITY:
            return self._from_entity()
        if self._source == LEADERSHIP_LEASE:
            return await self._from_lease()
        _LOGGER.error(
            "Unknown leadership source %r — treating this node as a follower",
            self._source,
        )
        return False

    def _from_entity(self) -> bool:
        if not self._entity_id:
            _LOGGER.error(
                "Leadership source is 'entity' but no entity is configured — "
                "treating this node as a follower"
            )
            return False
        state = self._hass.states.get(self._entity_id)
        if state is None:
            # Fails closed on purpose. A typo, or a flag Keepalived has not
            # created yet, must not read as "I am in charge".
            _LOGGER.warning(
                "Leadership entity %s does not exist — treating this node as a "
                "follower. It will not publish state until the entity appears.",
                self._entity_id,
            )
            return False
        return state.state.lower() in _TRUTHY_STATES

    async def _from_lease(self) -> bool:
        try:
            held = self._config_dir is not None and await self._hass.async_add_executor_job(
                is_held, self._config_dir
            )
            if held:
                # Maintenance hold: renew what we hold, never take what is
                # free. A standby that claims the lease here is the failover
                # the operator set the hold to prevent -- and during planned
                # work a free lease usually means the peer is mid-restart,
                # not dead. See hold.py.
                return await self._backend.renew_leadership(self._node_id)
            return await self._backend.acquire_leadership(self._node_id)
        except Exception:  # noqa: BLE001 — a backend that cannot answer is a no
            _LOGGER.warning(
                "Could not evaluate the cluster lease — treating this node as a follower",
                exc_info=True,
            )
            return False
