"""ADR-001 layers 3 and 4 — recorder writes and automations.

Layer 2 (the snapshot flush) already consumes the leadership signal. These two
consume the same signal, deliberately without copying its posture.

**Why these are opt-in.** `LeadershipMonitor.async_is_leader` fails closed: a
backend it cannot reach answers "not leader". For the flush that is correct —
declining to write costs one interval of freshness, and assuming leadership is
how two writers happen. For automations it is not remotely equivalent. A
thirty-second Valkey blip would turn off every automation in the house, and the
mechanism intended to prevent a messy failover would become the outage.

So both default to off. Turning them on is a statement that you have a warm
standby, or a shared recorder, and have accepted that trade.

**Why it acts only on transitions.** This is called from the same interval as
the flush, every few seconds. Re-issuing `automation.turn_off` on a timer would
fight anyone using the UI and fill the log with a change that is not one.
"""

from __future__ import annotations

import logging

from homeassistant.const import ENTITY_MATCH_ALL
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

AUTOMATION_DOMAIN = "automation"
RECORDER_DOMAIN = "recorder"


class ServiceGate:
    """Bring recorder and automations into line with this node's role.

    Holds the last state it successfully reached, so a call that failed is
    retried on the next pass rather than recorded as done. A gate that believes
    it disabled something it did not is worse than no gate: it reports success
    and leaves a follower running automations.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        gate_recorder: bool = False,
        gate_automations: bool = False,
    ) -> None:
        self._hass = hass
        self._gate_recorder = gate_recorder
        self._gate_automations = gate_automations
        # None means "this gate has not switched anything off". It is not a
        # synonym for "leader": a leader pass while None must do NOTHING.
        #
        # Turning everything on because we happen to be leader would trample
        # intent — an operator who deliberately disabled one automation in the
        # UI would find it re-enabled by a failover component that had no
        # business having an opinion about it. The gate undoes its own actions
        # and nothing else.
        self._recorder_state: bool | None = None
        self._automation_state: bool | None = None

    @property
    def enabled(self) -> bool:
        return self._gate_recorder or self._gate_automations

    async def async_apply(self, is_leader: bool) -> None:
        """Reconcile both layers with the current role. Never raises.

        Asymmetric on purpose. Becoming a follower switches things off;
        becoming a leader switches them on **only if this gate switched them
        off in the first place**. A leader that was never gated does nothing at
        all, which is what keeps the integration out of the operator's way.
        """
        if self._gate_recorder:
            await self._reconcile(
                is_leader,
                "_recorder_state",
                RECORDER_DOMAIN,
                "enable",
                "disable",
                target_all=False,
            )
        if self._gate_automations:
            await self._reconcile(
                is_leader,
                "_automation_state",
                AUTOMATION_DOMAIN,
                "turn_on",
                "turn_off",
            )

    async def _reconcile(
        self,
        is_leader: bool,
        attr: str,
        domain: str,
        on_service: str,
        off_service: str,
        *,
        target_all: bool = True,
    ) -> None:
        gated_off = getattr(self, attr) is False
        if not is_leader and not gated_off:
            if await self._call(domain, off_service, target_all=target_all):
                setattr(self, attr, False)
        elif is_leader and gated_off:
            if await self._call(domain, on_service, target_all=target_all):
                setattr(self, attr, None)

    async def async_restore(self) -> None:
        """Undo anything this gate turned off. Called on unload.

        Without this, removing the integration while the node is a follower
        leaves the recorder and every automation switched off, and the only
        thing that would turn them back on has just been deleted. Uninstalling
        something must not be how a house loses its automations.

        Only what was actually disabled is restored: a leader that unloads has
        nothing to undo and must not issue a spurious `turn_on`.
        """
        await self._reconcile(
            True,
            "_recorder_state",
            RECORDER_DOMAIN,
            "enable",
            "disable",
            target_all=False,
        )
        await self._reconcile(
            True,
            "_automation_state",
            AUTOMATION_DOMAIN,
            "turn_on",
            "turn_off",
        )

    async def _call(self, domain: str, service: str, *, target_all: bool = True) -> bool:
        """Call one service. Returns whether it succeeded.

        Swallows everything: this runs inside the snapshot flush loop, and a
        recorder that refuses to disable is a degraded gate, whereas a raised
        exception is a stopped flush loop.
        """
        data = {"entity_id": ENTITY_MATCH_ALL} if target_all else {}
        try:
            await self._hass.services.async_call(domain, service, data, blocking=True)
        except Exception:  # noqa: BLE001 — must not take the flush loop down
            _LOGGER.warning(
                "Could not %s.%s while gating this node; will retry",
                domain,
                service,
                exc_info=True,
            )
            return False
        if service in ("turn_off", "disable"):
            # Deliberately a warning, and deliberately carrying the recovery.
            # This state persists across a Home Assistant restart, and it
            # outlives the integration: if the entry is removed while a node is
            # gated off — or if setup later fails before the gate is built,
            # because the backend is unreachable — nothing remains that would
            # undo it. Someone reading the log at that point needs the fix in
            # front of them, not a hunt through the docs.
            _LOGGER.warning(
                "Cluster gating: this node is a FOLLOWER, so %s.%s was called. "
                "This persists across restarts. To undo it by hand: Developer "
                "Tools -> Actions -> %s.%s with entity_id: all",
                domain,
                service,
                domain,
                "turn_on" if domain == AUTOMATION_DOMAIN else "enable",
            )
        else:
            _LOGGER.info("Cluster gating: called %s.%s", domain, service)
        return True
