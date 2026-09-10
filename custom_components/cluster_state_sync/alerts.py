"""Reaching the person who is not looking at Home Assistant.

Everything this integration knew, until v0.4.2, was visible only to somebody
*looking*: eighteen diagnostic entities, a panel and the repairs page. For a
product whose whole premise is "the house keeps working while you are away",
observability that requires you to be at home is observability that stops one
step short of the point.

Three rules shape this module, and each is a scar:

**Edge-triggered, never level-triggered.** The repair sites raise-or-clear on
*every* pass, by design (a fixed standby must clear its own alarm without
anyone reloading anything). Notifying from those sites directly would push the
same alert every coordinator tick. This router tracks what is already active
and speaks only on the transition.

**A push failure must never touch the cluster.** A wrong `notify.` service
name, an expired push token, an integration that failed to load -- none of
these are reasons for state replication to stop. Every dispatch is wrapped and
degraded to a log line. The alerting is a passenger on this bus; it does not
get to steer.

**Silence is not evidence** -- AR-0040's lesson, and the reason
`async_clear` pushes rather than merely dismissing. An alarm that vanishes
without a word is indistinguishable from an alarm nobody sent. If we woke you,
we owe you the all-clear.
"""

from __future__ import annotations

import logging
import time

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback

from .const import (
    DOMAIN,
    NOTIFY_PROMOTED,
    NOTIFY_RECOVERED,
)

_LOGGER = logging.getLogger(__name__)

#: A condition that flaps must not become a condition that spams. The repair
#: card stays exact -- it is raised and cleared on every pass regardless. This
#: interval governs only the *interruption*, which is a scarcer resource than
#: the truth.
ALERT_MIN_INTERVAL = 300.0

#: Cap on pushes held across the boot window. Deliberately small, and the
#: OLDEST are kept: the promotion alert is raised first and is the one worth
#: keeping. A boot that queues more than this has something else wrong with it,
#: and the answer is not to hand somebody sixteen notifications at once.
MAX_PENDING_ALERTS = 16

#: How a promotion is told apart from an ordinary Home Assistant restart.
#:
#: The snapshot's `source_node` already records which node last wrote the
#: shared state, and it survives our restart because it lives in Valkey. So a
#: node that finds itself holding the lease while the newest snapshot bears
#: SOMEONE ELSE'S name has just taken over -- no new key, no extra round trip,
#: and no host-side helper.
#:
#: The host's `/run/cluster-sync/vrrp-state` would answer the same question and
#: is deliberately not used: it is on the host, unreachable from inside this
#: container, and reading it would need exactly the kind of host-side helper
#: this project has repeatedly refused to grow.


class AlertRouter:
    """Turns cluster conditions into interruptions, for chosen conditions only."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        conditions: tuple[str, ...] | list[str],
        services: tuple[str, ...] | list[str],
        node_id: str,
    ) -> None:
        self._hass = hass
        self._conditions = set(conditions)
        self._services = tuple(services)
        self._node_id = node_id
        self._active: set[str] = set()
        self._took_over_from: str | None = None
        #: 🚨 Held back until Home Assistant has finished starting.
        #:
        #: The alert that matters most -- 'this node just took over' -- is
        #: raised during OUR setup, and on a cold standby that is a Home
        #: Assistant which began booting seconds ago. The `notify.` service
        #: the operator chose belongs to another integration that may not have
        #: loaded yet, so dispatching immediately would find no such service,
        #: log a warning about a name that is perfectly correct, and drop the
        #: one notification the whole feature exists to send.
        #:
        #: Persistent notifications are NOT held: that component is core, is up
        #: before we are, and its whole job is to still be there when you look.
        self._pending: list[tuple[str, str]] = []
        self._started = hass.state is CoreState.running
        self._last_sent: dict[str, float] = {}
        #: Services already reported missing. Logged once, not once per tick:
        #: a typo in a service name should be findable, not deafening.
        self._warned: set[str] = set()

    @callback
    def async_arm(self) -> CALLBACK_TYPE:
        """Release held pushes once every other integration has loaded.

        Returns an unsubscribe callback, which setup registers: an entry
        unloaded mid-boot must not push afterwards from a listener nobody owns.

        If Home Assistant is already running -- the integration was added to a
        live instance rather than found at boot -- nothing was ever held, and
        this returns a callback that does nothing.
        """
        if self._started:
            return lambda: None

        fired = False

        async def _flush(_: Event) -> None:
            nonlocal fired
            fired = True
            self._started = True
            held, self._pending = self._pending, []
            for title, message in held:
                await self._push(title, message)

        unsub = self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _flush)

        @callback
        def _safe_unsub() -> None:
            """🚨 A one-time listener removes itself once it fires.

            Calling the returned unsubscribe afterwards makes Home Assistant
            log `Unable to remove unknown job listener` with a traceback, on
            every single unload after a normal boot. `_remove_start_listener`
            in `__init__.py` already carried a comment about exactly this and
            I reproduced it anyway.
            """
            if not fired:
                unsub()

        return _safe_unsub

    # -- edges ------------------------------------------------------------

    async def async_raise(self, condition: str, title: str, message: str) -> None:
        """Announce a condition that has just become true.

        A no-op when the condition was already active, so the raise-every-pass
        repair sites can call this unconditionally.
        """
        if condition in self._active:
            return
        self._active.add(condition)
        await self._dispatch(condition, title, message, sticky=True)

    async def async_clear(self, condition: str, title: str, message: str) -> None:
        """Announce that a condition has stopped being true."""
        if condition not in self._active:
            return
        self._active.discard(condition)
        # The card goes: a cleared condition must not leave something on the
        # repairs page demanding attention it no longer deserves.
        self._dismiss(condition)
        # The push stays. Whoever we woke is owed the all-clear, and they may
        # be nowhere near the repairs page to notice the card disappearing.
        await self._push_if_enabled(NOTIFY_RECOVERED, f"{condition}:clear", title, message)

    def forget(self, condition: str) -> None:
        """Drop a condition's active state without announcing anything.

        For an ACKNOWLEDGEMENT rather than a fix: the operator has dismissed
        the alarm, and the underlying thing may well still be true. Pushing an
        all-clear here would tell somebody their problem was solved when all
        that happened is that somebody pressed a button -- and a channel that
        says that once is a channel nobody reads afterwards.
        """
        self._active.discard(condition)

    async def async_event(self, condition: str, title: str, message: str) -> None:
        """Announce something that happened rather than something that is true.

        A promotion has no "cleared" state -- the house does not un-move -- so
        it is pushed once and leaves no card.

        Deliberately NOT rate-limited. The promoter's own 900-second hold-down
        (M3) already makes promotions arriving inside the flap window
        impossible, so the limit could never fire for a real cluster -- and if
        one somehow did arrive, "the house moved again" names a different node
        and is new information, not a repeat.
        """
        await self._push_if_enabled(condition, condition, title, message, rate_limit=False)

    # -- promotion --------------------------------------------------------

    async def async_check_promotion(
        self, *, leader: str | None, snapshot_source: str | None
    ) -> None:
        """Announce a genuine change of leader, and only a genuine one.

        The subtlety this exists for: on the cold-standby model a promoted node
        *starts* Home Assistant, so its first leadership resolution is always
        "leader" -- and so is the primary's, every single time it is restarted
        for maintenance. Naive edge detection cannot tell those apart and would
        cry wolf on every restart, which is how an operator learns to ignore
        the one alert that mattered.

        `snapshot_source` settles it. It names whoever last wrote the shared
        state, and it is not us until we write, so a node holding the lease
        over another node's snapshot is a node that has just taken over.
        """
        if leader != self._node_id:
            # Demoted, or never promoted. Re-arm, so a later takeover speaks.
            self._took_over_from = None
            return
        if not snapshot_source or snapshot_source == self._node_id:
            # A fresh cluster, or one whose newest state we already wrote.
            # Neither is a failover, and an alarm at install time is an alarm
            # nobody trusts afterwards.
            return
        if self._took_over_from == snapshot_source:
            return  # Already announced; the poll simply beat our first write.
        self._took_over_from = snapshot_source

        await self.async_event(
            NOTIFY_PROMOTED,
            "Home Assistant failed over",
            (
                f"The house is now running on {self._node_id}, which took over "
                f"from {snapshot_source}. Anything wired to the other host -- a "
                f"radio that does not follow -- is unavailable until it returns."
            ),
        )

    # -- dispatch ---------------------------------------------------------

    async def _push_if_enabled(
        self, gate: str, key: str, title: str, message: str, *, rate_limit: bool = True
    ) -> None:
        """Push if `gate` is a chosen condition, subject to `key`'s flap window.

        `gate` and `key` are separate on purpose. Recoveries are gated by the
        single `recovered` choice but rate-limited PER CONDITION, so a flapping
        statistics gap cannot suppress the all-clear for a degraded fileset.

        The two edges of one condition also get separate windows. Sharing one
        would mean a fault that clears inside five minutes never announces that
        it cleared -- which is precisely the case where the all-clear is most
        worth having, because somebody is still holding their phone.
        """
        if gate not in self._conditions:
            return
        if rate_limit:
            now = time.monotonic()
            last = self._last_sent.get(key)
            if last is not None and now - last < ALERT_MIN_INTERVAL:
                _LOGGER.debug("Suppressing a repeat %s alert inside the flap window", key)
                return
            self._last_sent[key] = now
        await self._push(title, message)

    async def _dispatch(self, condition: str, title: str, message: str, *, sticky: bool) -> None:
        if sticky:
            self._card(condition, title, message)
        await self._push_if_enabled(condition, f"{condition}:raise", title, message)

    def _card(self, condition: str, title: str, message: str) -> None:
        """A persistent notification, which every admin sees with no setup.

        This is the floor, not the ceiling: it is what makes zero configuration
        still tell somebody. It carries a stable id per condition so a repeat
        replaces rather than accumulates.
        """
        try:
            from homeassistant.components import persistent_notification as pn

            pn.async_create(
                self._hass, message, title=title, notification_id=f"{DOMAIN}_{condition}"
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not raise the persistent notification", exc_info=True)

    def _dismiss(self, condition: str) -> None:
        try:
            from homeassistant.components import persistent_notification as pn

            pn.async_dismiss(self._hass, f"{DOMAIN}_{condition}")
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not dismiss the persistent notification", exc_info=True)

    async def _push(self, title: str, message: str) -> None:
        """Call every configured `notify.` service, surviving each one's failure.

        Deliberately sequential and individually guarded: one dead service must
        not stop the others, because the one that still works is the one the
        operator is actually holding.
        """
        if not self._started:
            # Held, not dropped. See `_pending`.
            if len(self._pending) < MAX_PENDING_ALERTS:
                self._pending.append((title, message))
            else:
                _LOGGER.debug("Boot alert queue is full; dropping %r", title)
            return
        for service in self._services:
            name = service.split(".", 1)[1] if service.startswith("notify.") else service
            if not self._hass.services.has_service("notify", name):
                if name not in self._warned:
                    self._warned.add(name)
                    _LOGGER.warning(
                        "Alert service notify.%s does not exist -- alerts will not reach it. "
                        "Check the service name in the integration's options.",
                        name,
                    )
                continue
            try:
                await self._hass.services.async_call(
                    "notify", name, {"title": title, "message": message}, blocking=False
                )
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Alert via notify.%s failed", name, exc_info=True)
