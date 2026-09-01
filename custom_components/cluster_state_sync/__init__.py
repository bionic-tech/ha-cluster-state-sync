"""Cluster State Sync — shared state mirror for active-passive Home Assistant.

This integration writes a periodic snapshot of selected entity states to a
shared backend (Redis/Valkey today; recorder/Postgres planned). On startup,
each HA instance reads the snapshot and seeds its in-memory state machine,
so a freshly-promoted standby node doesn't act on stale assumptions during
the failover window.

Design constraints — read these before changing anything:

* Uses only public HA APIs. No monkey-patching of core.
* Backend failures never raise into HA — they log and continue.
* Snapshot frequency is bounded; a busy mesh can't DoS the backend.
* On restore at boot, the peer's snapshot is the authority for the entities
  we track. It used to say the opposite — that local state wins when fresher —
  and that turned out to mean the restore did nothing at all, because Home
  Assistant stamps everything it replays from disk with the boot time. See
  AR-0040 in `_restore_from_snapshot`. Off the boot path, local state does
  still win, because there the timestamps mean something.
* Automation double-firing during failover is *not* solved here. That
  needs leader election, which is a v0.2 problem.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_START,
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
    Platform,
)
from homeassistant.core import (
    Context,
    CoreState,
    Event,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval

from .backend import ClusterBackend, RedisBackend, SnapshotEntry, default_node_id
from .const import (
    CLOCK_SKEW_TOLERANCE,
    CONF_CLUSTER_NAMESPACE,
    CONF_CLUSTER_SECRET,
    CONF_EXCLUDE_ENTITIES,
    CONF_FILESET_ENABLED,
    CONF_FILESET_EXCLUSIONS,
    CONF_FILESET_HOT_INTERVAL,
    CONF_FILESET_MAX_BYTES,
    CONF_GATE_AUTOMATIONS,
    CONF_GATE_RECORDER,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_ENTITIES,
    CONF_LEADERSHIP_ENTITY,
    CONF_LEADERSHIP_SOURCE,
    CONF_NODE_ID,
    CONF_REDIS_DB,
    CONF_REDIS_HOST,
    CONF_REDIS_PASSWORD,
    CONF_REDIS_PORT,
    CONF_REDIS_SENTINEL_HOSTS,
    CONF_REDIS_SENTINEL_SERVICE,
    CONF_REDIS_TLS_CA_CERTS,
    CONF_REDIS_USE_SENTINEL,
    CONF_REDIS_USE_TLS,
    CONF_REDIS_USERNAME,
    CONF_RESTORE_MAX_AGE,
    CONF_SNAPSHOT_INTERVAL,
    DATA_BACKEND,
    DATA_CONFIG,
    DATA_COORDINATOR,
    DATA_DEGRADED_MARKER,
    DATA_FILESET,
    DATA_GATE,
    DATA_LEADERSHIP,
    DATA_MIRROR,
    DATA_STATS,
    DATA_UNSUB,
    DEFAULT_CLUSTER_NAMESPACE,
    DEFAULT_FILESET_ENABLED,
    DEFAULT_FILESET_EXCLUSIONS,
    DEFAULT_FILESET_HOT_INTERVAL,
    DEFAULT_FILESET_MAX_BYTES,
    DEFAULT_INCLUDE_DOMAINS,
    DEFAULT_LEADERSHIP_SOURCE,
    DEFAULT_RESTORE_MAX_AGE,
    DEFAULT_SNAPSHOT_INTERVAL,
    DEGRADED_MARKER_NAME,
    DOMAIN,
    FINAL_FLUSH_TIMEOUT,
    MAX_ATTRIBUTE_BYTES,
    MAX_RESTORE_ENTRIES,
    SENSITIVE_DOMAINS,
    STALE_SNAPSHOT_FRACTION,
)
from .coordinator import BackendHealthCoordinator, SyncStats
from .fileset import FilesetPublisher
from .gating import ServiceGate
from .leadership import LeadershipMonitor
from .util import parse_sentinel_hosts

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Cluster State Sync from a config entry."""
    cfg = {**entry.data, **entry.options}
    namespace = cfg.get(CONF_CLUSTER_NAMESPACE, DEFAULT_CLUSTER_NAMESPACE)
    node_id = cfg.get(CONF_NODE_ID) or default_node_id()

    backend = RedisBackend(
        namespace=namespace,
        host=cfg.get(CONF_REDIS_HOST),
        port=cfg.get(CONF_REDIS_PORT, 6379),
        username=cfg.get(CONF_REDIS_USERNAME),
        password=cfg.get(CONF_REDIS_PASSWORD),
        db=cfg.get(CONF_REDIS_DB, 2),
        use_sentinel=cfg.get(CONF_REDIS_USE_SENTINEL, False),
        sentinel_hosts=parse_sentinel_hosts(cfg.get(CONF_REDIS_SENTINEL_HOSTS, "")),
        sentinel_service=cfg.get(CONF_REDIS_SENTINEL_SERVICE),
        use_tls=cfg.get(CONF_REDIS_USE_TLS, False),
        tls_ca_certs=cfg.get(CONF_REDIS_TLS_CA_CERTS) or None,
        secret=cfg.get(CONF_CLUSTER_SECRET) or None,
    )

    if not cfg.get(CONF_CLUSTER_SECRET):
        # AR-0005. Entries cannot be authenticated without a shared secret, and
        # the restore path hands whatever it reads straight to async_set. An
        # entry with no provenance is not a degraded snapshot, it is an
        # instruction from an unknown party — so we mirror outward but refuse
        # to take anything back in.
        _LOGGER.error(
            "No cluster secret is configured, so snapshot entries cannot be "
            "verified. This node will keep publishing its own state but will "
            "REFUSE TO RESTORE anything, because an unauthenticated entry is "
            "applied verbatim to the state machine. Reconfigure the "
            "integration to set a cluster secret shared with the peer node."
        )

    tracked_sensitive = SENSITIVE_DOMAINS.intersection(
        cfg.get(CONF_INCLUDE_DOMAINS) or DEFAULT_INCLUDE_DOMAINS
    )
    if tracked_sensitive and not cfg.get(CONF_REDIS_USE_TLS):
        # Review conflict C3, resolved as "warn loudly, don't silently upgrade".
        # Flipping TLS on by ourselves would break every deployment whose Valkey
        # has no TLS listener, turning a privacy warning into an outage.
        _LOGGER.warning(
            "Mirroring %s without TLS. These domains describe where people are "
            "and whether the house is armed, and they are crossing the network "
            "in the clear to a shared backend. Enable TLS on the integration, "
            "or stop tracking these domains.",
            ", ".join(sorted(tracked_sensitive)),
        )

    try:
        await backend.connect()
    except Exception as err:
        # ConfigEntryNotReady triggers HA's automatic retry-with-backoff. If
        # Redis is briefly down on boot, the integration will come up later.
        raise ConfigEntryNotReady(f"Cannot reach state-sync backend: {err}") from err

    runtime: dict[str, Any] = {
        DATA_BACKEND: backend,
        DATA_CONFIG: cfg,
        DATA_UNSUB: [],
        DATA_MIRROR: None,
        DATA_STATS: SyncStats(),
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    # 1. Seed local state from the snapshot BEFORE automations start.
    #    HA fires EVENT_HOMEASSISTANT_START after integrations have loaded
    #    but before the automation engine, which is exactly the window we want.
    if hass.state is CoreState.running:
        # AR-0012: added to a live HA, not at boot. Restoring here would seed
        # dozens of entities into a running system and every automation
        # watching them would fire at once — an automation storm caused by the
        # thing that exists to make failover uneventful. The snapshot will be
        # applied at the next restart, which is the only moment it is safe.
        _LOGGER.info(
            "Added while Home Assistant is already running — skipping restore. "
            "The shared snapshot will be applied on the next restart."
        )
    else:
        restored_already = False

        async def _on_start(_event: Event) -> None:
            nonlocal restored_already
            restored_already = True
            await _restore_from_snapshot(
                hass, backend, cfg, node_id, runtime[DATA_STATS], at_boot=True
            )

        unsub_start = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _on_start)

        @callback
        def _remove_start_listener() -> None:
            # A one-time listener removes itself once it fires; calling the
            # returned unsub afterwards makes HA log an "unknown job listener"
            # error. Only unsubscribe if the event never arrived.
            if not restored_already:
                unsub_start()

        runtime[DATA_UNSUB].append(_remove_start_listener)

    # 2. Build the authoritative mirror and seed it from current state.
    #    Seeding is AR-0002: without it, an entity that never fires a
    #    state-changed event after boot would never be mirrored at all.
    mirror = StateMirror(hass, backend, cfg, node_id)
    mirror.seed_from_current_states()
    runtime[DATA_MIRROR] = mirror

    @callback
    def _on_state_changed(event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        if new_state is None:
            # Entity removed; skip. Could optionally tombstone in v0.2.
            return
        if not _should_track(new_state.entity_id, cfg):
            return
        # No lock needed — the asyncio event loop is single-threaded, so this
        # callback runs to completion atomically.
        mirror.record(new_state)

    runtime[DATA_UNSUB].append(hass.bus.async_listen(EVENT_STATE_CHANGED, _on_state_changed))

    # 3. Periodic flush, driven by HA's clock rather than a bare asyncio.sleep
    #    so it cancels cleanly on shutdown and is drivable in tests.
    interval = cfg.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL)

    # AR-0017: leadership gates the write. Phase 1's merge-instead-of-erase
    # write stays underneath as the interim guard, but merging two nodes' views
    # is damage control — the follower's state is by definition the stale one,
    # so the real answer is that it does not write at all.
    leadership = LeadershipMonitor(
        hass,
        backend,
        node_id,
        source=cfg.get(CONF_LEADERSHIP_SOURCE, DEFAULT_LEADERSHIP_SOURCE),
        entity_id=cfg.get(CONF_LEADERSHIP_ENTITY),
    )
    runtime[DATA_LEADERSHIP] = leadership

    # ADR-001 layers 3 and 4, sharing layer 2's signal and its cadence. Both
    # off unless asked for — see gating.py for why they must not inherit the
    # flush's fail-closed posture.
    gate = ServiceGate(
        hass,
        gate_recorder=bool(cfg.get(CONF_GATE_RECORDER)),
        gate_automations=bool(cfg.get(CONF_GATE_AUTOMATIONS)),
    )
    runtime[DATA_GATE] = gate

    # AR-0039: apply the gate once as soon as leadership can be evaluated,
    # instead of waiting for the first flush interval.
    #
    # The restore is wired to EVENT_HOMEASSISTANT_START precisely because that
    # is the window after integrations load and before the automation engine
    # attaches its triggers. Layer 4 decides whether those automations should
    # be running at all, and it was only ever consulted from the flush timer —
    # so on a follower the order was: restore a batch of peer states, start
    # every automation, let them act on what was just restored, and only then
    # turn them off. The two halves disagreed about when "before automations"
    # is. The interval is configurable up to 300 seconds, so the window was not
    # bounded by the 5-second default either.
    #
    # Registered after the restore listener, so it runs after it: listeners on
    # the same event fire in registration order.
    async def _apply_gate(_event: Event | None = None) -> None:
        await gate.async_apply(await leadership.async_is_leader())

    if gate.enabled:
        if hass.state is CoreState.running:
            # Added to a live system. Unlike the restore (AR-0012), acting now
            # is the point: the operator asked for followers to be quiet, and
            # the automations they mean are already running.
            hass.async_create_task(_apply_gate())
        else:
            gated_already = False

            async def _on_start_gate(event: Event) -> None:
                nonlocal gated_already
                gated_already = True
                await _apply_gate(event)

            unsub_gate = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _on_start_gate)

            @callback
            def _remove_gate_listener() -> None:
                # A one-time listener removes itself once it fires; calling the
                # returned unsub afterwards makes HA log an "unknown job
                # listener" error.
                if not gated_already:
                    unsub_gate()

            runtime[DATA_UNSUB].append(_remove_gate_listener)

    async def _scheduled_flush(_now: datetime) -> None:
        is_leader = await leadership.async_is_leader()
        # Reconcile before returning: a follower needs gating applied precisely
        # when it is NOT flushing, so this cannot sit after the early return.
        await gate.async_apply(is_leader)
        if not is_leader:
            return
        await mirror.async_flush()

    runtime[DATA_UNSUB].append(
        async_track_time_interval(
            hass,
            _scheduled_flush,
            timedelta(seconds=interval),
            name=f"{DOMAIN}_snapshot_flush",
            cancel_on_shutdown=True,
        )
    )

    # 3b. Fileset replication (design 2026-08-29). Off unless configured: it
    #     reads every credential in the config directory, so the operator opts
    #     in. Leader-only for the same reason the flush is — a follower
    #     publishing would overwrite the leader's identity with its own.
    publisher: FilesetPublisher | None = None
    if cfg.get(CONF_FILESET_ENABLED, DEFAULT_FILESET_ENABLED):
        secret = cfg.get(CONF_CLUSTER_SECRET)
        if not secret:
            # Without a secret there is no key, and an unsealed `.storage` in
            # shared Valkey is not a degraded mode worth offering.
            _LOGGER.error(
                "Fileset replication is enabled but no cluster secret is set; "
                "not publishing. Reconfigure the integration to mint one."
            )
        else:
            publisher = FilesetPublisher(
                backend,
                config_dir=hass.config.path(),
                node_id=node_id,
                secret=secret,
                exclusions=cfg.get(CONF_FILESET_EXCLUSIONS, DEFAULT_FILESET_EXCLUSIONS),
                max_bytes=cfg.get(CONF_FILESET_MAX_BYTES, DEFAULT_FILESET_MAX_BYTES),
            )

            async def _scheduled_fileset(_now: datetime) -> None:
                if not await leadership.async_is_leader():
                    return
                try:
                    await publisher.async_publish()
                except Exception:  # noqa: BLE001 — a publish must never kill the loop
                    _LOGGER.exception("Fileset publish failed; will retry next interval")

            runtime[DATA_UNSUB].append(
                async_track_time_interval(
                    hass,
                    _scheduled_fileset,
                    timedelta(
                        seconds=cfg.get(CONF_FILESET_HOT_INTERVAL, DEFAULT_FILESET_HOT_INTERVAL)
                    ),
                    name=f"{DOMAIN}_fileset_publish",
                    cancel_on_shutdown=True,
                )
            )

    runtime[DATA_FILESET] = publisher

    # 3c. The degraded-fileset alarm. Decision D4 has the standby promote on a
    #     stale or missing go-bag rather than refuse to start, which makes
    #     this the only safety net: every HTTP health check on a node in that
    #     state is green anyway. `cluster-fileset-swap.sh` writes this marker
    #     on the host, before the container starts, on every degraded path,
    #     and removes it on a clean swap — so its mere presence is the signal,
    #     and it does not go away on its own.
    #
    #     AR-0040 is why this cannot be a log line: a defect of exactly this
    #     shape — a promotion that quietly did not do its job — lived in this
    #     project for its entire life, and its only symptom was an INFO line
    #     nobody read. This goes to the repairs panel instead.
    marker_path = hass.config.path(DEGRADED_MARKER_NAME)

    def _read_degraded_marker() -> dict[str, Any] | None:
        try:
            with open(marker_path, encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            # The normal case on a healthy node, and the only one that is not
            # worth a word.
            return None
        except (OSError, ValueError):
            # A marker that exists but cannot be read is NOT the same as no
            # marker, and returning None silently makes them identical. This is
            # the only safety net on decision D4's "promote anyway": the swap
            # wrote this file because something went wrong, and a truncated
            # write or a permission problem would otherwise turn the alarm off
            # and leave the node looking clean. Say so, because the alternative
            # is a degraded promotion nobody ever hears about.
            _LOGGER.warning(
                "Could not read the degraded-fileset marker at %s; treating this boot as "
                "clean. If the host-side swap wrote one, its alarm has been lost -- check "
                "the swap log on the host.",
                marker_path,
                exc_info=True,
            )
            return None

    degraded_marker = await hass.async_add_executor_job(_read_degraded_marker)
    runtime[DATA_DEGRADED_MARKER] = degraded_marker
    if degraded_marker:
        ir.async_create_issue(
            hass,
            DOMAIN,
            "fileset_degraded",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="fileset_degraded",
            translation_placeholders={"reason": str(degraded_marker.get("reason", "unknown"))},
        )
    else:
        # The issue registry is storage-backed and survives restarts, so
        # creating the issue is only half the job. `strings.json` tells the
        # operator that reloading the integration once a fresh fileset is
        # staged clears it — without this, that is a false promise, and a
        # fully-recovered node would carry a stale ERROR forever. Deleting an
        # issue that does not exist is a documented no-op, so this is safe to
        # run on every setup, degraded or not.
        ir.async_delete_issue(hass, DOMAIN, "fileset_degraded")

    # 4. On stop, write a final snapshot — the gift the dying node leaves the
    #    standby. AR-0016: this is the *only* shutdown flush path. v0.1 had two
    #    (a CancelledError handler in the loop and this listener), which could
    #    both fire and race over the same live dict.
    async def _on_stop(_event: Event) -> None:
        if await leadership.async_is_leader():
            await _final_flush(mirror)
        # Hand the lease back on the way out so the peer promotes immediately
        # rather than waiting out the TTL.
        await _release_leadership(backend, node_id)

    runtime[DATA_UNSUB].append(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop))

    # 5. Diagnostics (AR-0019/AR-0032). Set up last so the entities find a
    #    fully-populated runtime dict.
    coordinator = BackendHealthCoordinator(hass, backend)
    await coordinator.async_config_entry_first_refresh()
    runtime[DATA_COORDINATOR] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info(
        "Cluster State Sync ready — node=%s namespace=%s interval=%ds tracking=%d entities",
        node_id,
        namespace,
        interval,
        mirror.tracked_count,
    )
    return True


async def _release_leadership(backend: ClusterBackend, node_id: str) -> None:
    """Best-effort lease handback. Never raises."""
    release = getattr(backend, "release_leadership", None)
    if release is None:
        return
    try:
        await asyncio.wait_for(release(node_id), timeout=FINAL_FLUSH_TIMEOUT)
    except Exception:  # noqa: BLE001 — teardown must not be blocked by Redis
        _LOGGER.debug("Could not release the cluster lease", exc_info=True)


async def _final_flush(mirror: StateMirror) -> None:
    """Best-effort, time-bounded last write. Never raises (AR-0016)."""
    try:
        await asyncio.wait_for(mirror.async_flush(), timeout=FINAL_FLUSH_TIMEOUT)
    except Exception:  # noqa: BLE001 — teardown must not be blocked by Redis
        _LOGGER.debug("Final flush failed", exc_info=True)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down on entry removal."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    runtime = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not runtime:
        return True

    # Stop the timer and the event listener before flushing, so nothing new
    # lands in the mirror while the last write is in flight.
    for unsub in runtime.get(DATA_UNSUB, []):
        unsub()

    # Undo the gating before anything else. If this node was a follower with
    # layers 3 or 4 enabled, its recorder and automations are switched off, and
    # after this unload nothing remains that would turn them back on. Removing
    # an integration must not be how a house loses its automations.
    gate: ServiceGate | None = runtime.get(DATA_GATE)
    if gate is not None:
        await gate.async_restore()

    mirror: StateMirror | None = runtime.get(DATA_MIRROR)
    if mirror is not None:
        await _final_flush(mirror)

    backend = runtime.get(DATA_BACKEND)
    if backend is not None:
        # Hand the lease back here too, not only on Home Assistant shutdown.
        # Unload covers reloads and reconfiguration, and a lease left held by a
        # node that is no longer running the integration costs the peer the
        # full TTL before it can promote — failover budget burned for nothing,
        # on the one kind of shutdown that was entirely orderly.
        cfg = runtime.get(DATA_CONFIG) or {}
        node_id = cfg.get(CONF_NODE_ID) or default_node_id()
        await _release_leadership(backend, node_id)
        await backend.close()
    return True


# ---------------------------------------------------------------------------
# Authoritative state mirror
# ---------------------------------------------------------------------------


class StateMirror:
    """Full in-memory mirror of every tracked entity, and the flush cycle.

    This replaces v0.1's `pending` change-buffer, which was the subject of the
    v1 review's headline finding. The old buffer held only the entities that
    changed since the last flush, and the flush wiped the shared hash before
    rewriting it from that buffer — so the "snapshot" a promoted standby read
    back was whatever happened to move in the preceding five seconds, and a
    quiet entity was never in it at all (AR-0001, AR-0002).

    Here the map is authoritative: seeded from current state at setup, updated
    on every tracked change, and written *whole* on every flush. That also
    dissolves AR-0015 — the map is bounded by the number of tracked entities,
    not by how fast they change, so no amount of churn can grow it.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        backend: ClusterBackend,
        cfg: dict[str, Any],
        node_id: str,
    ) -> None:
        self._hass = hass
        self._backend = backend
        self._cfg = cfg
        self._node_id = node_id
        self._tracked: dict[str, SnapshotEntry] = {}
        # Monotonic revision rather than a dirty *flag*: changes that arrive
        # while a flush is awaiting the backend bump the revision past the one
        # being written, so they are never mistaken for already-persisted.
        self._revision = 0
        self._flushed_revision = 0
        self._last_successful_flush: datetime | None = None
        self._listeners: list[Callable[[], None]] = []

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to mirror changes; returns an unsubscribe callable.

        Push rather than poll, matching the integration's `local_push` iot
        class. Listeners fire when the tracked set changes size or a flush
        succeeds — not on every individual state change, which at a few hundred
        entities would be a needless entity write per event.
        """
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    @property
    def last_successful_flush(self) -> datetime | None:
        """When the snapshot last actually reached the backend.

        Drives the `last_snapshot_age` diagnostic. Stays None until a write
        genuinely succeeds, so "never written" reads as unknown rather than as
        a reassuring zero.
        """
        return self._last_successful_flush

    @property
    def tracked_count(self) -> int:
        """Number of entities currently mirrored."""
        return len(self._tracked)

    @property
    def has_unflushed_changes(self) -> bool:
        return self._revision != self._flushed_revision

    @callback
    def seed_from_current_states(self) -> None:
        """Capture every already-present tracked entity (AR-0002)."""
        for state in self._hass.states.async_all():
            if _should_track(state.entity_id, self._cfg):
                self._tracked[state.entity_id] = _entry_from_state(state, self._node_id)
        if self._tracked:
            self._revision += 1
        _LOGGER.debug("Seeded mirror with %d entities", len(self._tracked))

    @callback
    def record(self, state: State) -> None:
        """Fold one state change into the authoritative map."""
        is_new = state.entity_id not in self._tracked
        self._tracked[state.entity_id] = _entry_from_state(state, self._node_id)
        self._revision += 1
        if is_new:
            self._notify()

    async def async_flush(self) -> bool:
        """Write the full map to the backend. Returns True on a successful write.

        Skips entirely when nothing has changed since the last successful
        flush, so an idle cluster does not rewrite an identical snapshot every
        interval.
        """
        if not self.has_unflushed_changes or not self._tracked:
            return False

        revision = self._revision
        payload = dict(self._tracked)

        if await self._backend.write_snapshot(payload, self._node_id):
            # Only the revision we actually wrote is marked clean; anything
            # recorded during the await stays pending for the next interval.
            self._flushed_revision = revision
            self._last_successful_flush = datetime.now(tz=UTC)
            self._notify()
            return True

        # AR-0011: leave the revision dirty so the next interval retries.
        # A backend outage must degrade the snapshot's freshness, never
        # silently discard the changes that happened during it.
        _LOGGER.debug("Snapshot write failed; %d entities still pending", len(payload))
        return False


def _entry_from_state(state: State, node_id: str) -> SnapshotEntry:
    """Convert an HA state object into a storable snapshot entry."""
    return SnapshotEntry(
        entity_id=state.entity_id,
        state=state.state,
        attributes=dict(state.attributes),
        last_changed=state.last_changed.isoformat(),
        last_updated=state.last_updated.isoformat(),
        source_node=node_id,
    )


# ---------------------------------------------------------------------------
# Restore logic
# ---------------------------------------------------------------------------


async def _restore_from_snapshot(
    hass: HomeAssistant,
    backend: ClusterBackend,
    cfg: dict[str, Any],
    node_id: str,
    stats: SyncStats,
    *,
    at_boot: bool,
) -> None:
    """Seed the local state machine from the shared snapshot.

    We only restore entities that:
      * match the include filter (no point restoring things we don't track),
      * have a snapshot age within the max-age window (don't restore ancient),
      * fit within the size bounds (AR-0009),
      * were not written by this node itself,
      * carry a valid signature (AR-0005).

    AR-0040 — what is deliberately *not* on that list any more, and why.

    It used to also require that the entry be newer than the local state, to
    "avoid clobbering fresh data". On two real Home Assistant containers that
    guard skipped every single entry of an eighteen-second-old snapshot:

        Snapshot age at restore: 18s (max 1800s)
        Restored 0 entities from snapshot (skipped: 2 local-newer, ...)

    Every domain in the default allowlist is a RestoreEntity. At boot Home
    Assistant replays each one's previous value from this node's own disk and
    stamps `last_updated` with **the boot time**, not the time the value last
    actually changed. The restore runs at `EVENT_HOMEASSISTANT_START`, so by
    then *all* local state is stamped in this run — the comparison had no
    information in it, and resolved against the snapshot every time.

    It then compounded: the standby restored nothing, took the lease, and
    flushed its own cold state over the peer's good snapshot. The integration
    destroyed the snapshot it had failed to use.

    The replacement is a decision rather than a computation, because at boot
    the timestamps genuinely cannot tell a replayed value from a freshly polled
    one. On a promoting standby the peer's snapshot is the authority for the
    entities we track — that is the premise of the whole integration — and it
    stays bounded by max-age, the signature, the size cap, the future-stamp
    refusal and the include filter. A device-backed entity that really did poll
    fresher state corrects itself on its next poll; a helper never does, and
    helpers are most of what this tracks.

    The comparison is kept behind `at_boot` for a caller that does not exist
    yet: a manual restore invoked against a running system, where local
    timestamps *are* evidence because they were set by real events rather
    than by startup. `at_boot` rather than a timestamp boundary on purpose —
    there is no instant we can compare against, because Home Assistant
    replays entity state as each integration sets up, which may be before or
    after this one. The thing we actually know is which path we are on.

    Every entry here is attacker-controllable until AR-0005 lands: anything
    able to write to the shared hash can put an arbitrary state in front of
    `async_set`. These guards bound the damage; they do not establish
    authenticity. That is the signature's job, not the timestamp's.
    """
    if not cfg.get(CONF_CLUSTER_SECRET):
        # AR-0005. Refusing here rather than restoring unverified entries is
        # the whole point: a snapshot we cannot authenticate is not a weaker
        # snapshot, it is input from an unknown writer that we would otherwise
        # apply verbatim to the state machine. Starting cold is a worse
        # failover; obeying a forged alarm state is a worse outcome.
        _LOGGER.error(
            "Refusing to restore: no cluster secret is configured, so snapshot "
            "entries cannot be authenticated. Starting cold instead."
        )
        return

    entries, meta = await backend.read_snapshot()
    if not entries:
        _LOGGER.info("No snapshot available — starting cold")
        return

    if len(entries) > MAX_RESTORE_ENTRIES:
        # Applying an unbounded hash blocks the event loop during precisely the
        # window a failover needs it responsive.
        _LOGGER.warning(
            "Snapshot holds %d entries, above the %d cap — restoring the first "
            "%d only. A legitimate snapshot should not be this large; check "
            "whether the namespace is shared or the hash has been tampered with.",
            len(entries),
            MAX_RESTORE_ENTRIES,
            MAX_RESTORE_ENTRIES,
        )
        entries = dict(list(entries.items())[:MAX_RESTORE_ENTRIES])

    max_age = cfg.get(CONF_RESTORE_MAX_AGE, DEFAULT_RESTORE_MAX_AGE)
    now = datetime.now(tz=UTC)
    cutoff = now - timedelta(seconds=max_age)
    # One context shared by every state this restore seeds, so the whole
    # operation is attributable in the logbook and distinguishable by
    # automations from a genuine device report (AR-0027).
    context = Context()
    restored = 0
    skipped_local_newer = 0
    skipped_too_old = 0
    skipped_own_node = 0
    skipped_oversized = 0
    skipped_unparseable = 0
    skipped_refused = 0
    skipped_future = 0
    clamped_future = 0

    for entity_id, entry in entries.items():
        # Don't restore states this node itself wrote — it'll already
        # have them in memory from a previous lifetime, or its integration
        # will. Only restore from the peer.
        if entry.source_node == node_id:
            skipped_own_node += 1
            continue

        entry_updated = _parse_entry_timestamp(entry.last_updated)
        if entry_updated is None:
            _LOGGER.debug("Snapshot entry for %s has bad timestamp", entity_id)
            skipped_unparseable += 1
            continue

        if entry_updated > now + timedelta(seconds=CLOCK_SKEW_TOLERANCE):
            # AR-0035. The review prescribed clamping future stamps to "now",
            # but clamping alone does not achieve what it was asked to: a stamp
            # clamped to now is still newer than every pre-existing local
            # state, so the poisoned value keeps winning the freshness guard.
            # (A regression test asserting the security property rather than
            # the mechanism is what surfaced this.)
            #
            # So: honest clock skew is tolerated and clamped below; a stamp
            # implausibly far ahead is refused outright. No legitimate writer
            # produces one, and the timestamp is not evidence of authenticity
            # in any case — that is the AR-0005 signature's job.
            skipped_future += 1
            continue

        if entry_updated > now:
            # Within tolerance: the peer's clock is a little ahead. Clamp so
            # the comparison below stays meaningful.
            clamped_future += 1
            entry_updated = now

        if entry_updated < cutoff:
            skipped_too_old += 1
            continue

        if not _should_track(entity_id, cfg):
            continue

        if not _attributes_within_budget(entity_id, entry.attributes):
            skipped_oversized += 1
            continue

        existing = hass.states.get(entity_id)
        if existing is not None and existing.last_updated > entry_updated and not at_boot:
            skipped_local_newer += 1
            continue

        # async_set is the documented public API for seeding state.
        #
        # AR-0037: and it is the one step of this loop that runs arbitrary Home
        # Assistant code, so it is the one step that can refuse. It validates
        # the entity_id it is handed and raises on a malformed one — the field
        # names come from a shared hash, so "no legitimate node writes that" is
        # a statement about the writer, not a guarantee about the reader.
        #
        # Every other guard above already isolates one bad entry (AR-0013).
        # Leaving this call outside a try meant the last step could still
        # abandon every entry after the offending one and skip the summary that
        # would have said so: a promotion that comes up silently cold, which is
        # the exact failure AR-0013 was raised to prevent.
        try:
            hass.states.async_set(
                entity_id=entity_id,
                new_state=entry.state,
                attributes=entry.attributes,
                force_update=False,
                context=context,
            )
        except Exception:  # noqa: BLE001 — one refused entry, not a failed failover
            skipped_refused += 1
            _LOGGER.debug("State machine refused entry %s", entity_id, exc_info=True)
            continue
        restored += 1

    stats.record_restore(restored)

    # AR-0040: "a snapshot was there and none of it was applied" is the exact
    # signature of the bug the node-ops rehearsal found, and it was logged at
    # INFO — indistinguishable at a glance from a healthy restore. It is the
    # one outcome that means this integration did nothing during the only event
    # it exists for, so it is a warning with the reason breakdown attached.
    if entries and restored == 0:
        _LOGGER.warning(
            "Restored NOTHING from a snapshot that held %d entries. This node "
            "has promoted with no state from its peer. Skipped: %d local-newer, "
            "%d too-old, %d own-node, %d oversized, %d unparseable, %d "
            "implausibly-future, %d refused.",
            len(entries),
            skipped_local_newer,
            skipped_too_old,
            skipped_own_node,
            skipped_oversized,
            skipped_unparseable,
            skipped_future,
            skipped_refused,
        )

    # AR-0020: make staleness explicit rather than implicit. Entries past
    # max_age were always skipped, but nothing said how old the surviving ones
    # were — so a promotion restoring 3-second-old state and one restoring
    # 29-minute-old state logged identically, and an operator could not tell a
    # healthy failover from a barely-legal one.
    snapshot_age = _snapshot_age_seconds(meta, now)
    age_text = f"{snapshot_age:.0f}s" if snapshot_age is not None else "unknown"
    _LOGGER.info("Snapshot age at restore: %s (max %ds)", age_text, max_age)
    if snapshot_age is not None and snapshot_age > max_age * STALE_SNAPSHOT_FRACTION:
        _LOGGER.warning(
            "Restored from a STALE snapshot — %.0fs old, against a %ds limit. "
            "The peer stopped writing well before this node took over; treat "
            "the restored state as suspect and check why its flush loop "
            "stopped.",
            snapshot_age,
            max_age,
        )

    _LOGGER.info(
        "Restored %d entities from snapshot (skipped: %d local-newer, "
        "%d too-old, %d own-node, %d oversized, %d unparseable, %d "
        "implausibly-future, %d refused) — source meta=%s",
        restored,
        skipped_local_newer,
        skipped_too_old,
        skipped_own_node,
        skipped_oversized,
        skipped_unparseable,
        skipped_future,
        skipped_refused,
        meta,
    )
    if skipped_refused:
        # Not a debug line: this is state the operator believes was restored
        # and was not, during the one event the integration exists for.
        _LOGGER.warning(
            "%d snapshot entries were refused by the state machine and were "
            "skipped. The rest of the snapshot was restored normally. Enable "
            "debug logging for this integration to see which entities.",
            skipped_refused,
        )
    if skipped_future:
        _LOGGER.warning(
            "%d snapshot entries were stamped implausibly far in the future and "
            "were refused. Nothing legitimate writes those — treat this as a "
            "sign the shared hash has been tampered with.",
            skipped_future,
        )
    if clamped_future:
        # Worth a warning rather than a debug line: either the peer's clock is
        # wrong or somebody is writing entries by hand.
        _LOGGER.warning(
            "%d snapshot entries were stamped in the future and were clamped to "
            "now. Check clock sync between nodes, or whether the shared hash is "
            "being written by something other than a cluster node.",
            clamped_future,
        )


def _snapshot_age_seconds(meta: dict[str, Any], now: datetime) -> float | None:
    """Age of the snapshot as a whole, from its metadata. None if unreadable."""
    written = _parse_entry_timestamp(str(meta.get("last_snapshot_at") or ""))
    if written is None:
        return None
    return max(0.0, (now - written).total_seconds())


def _parse_entry_timestamp(raw: str) -> datetime | None:
    """Parse a stored ISO-8601 timestamp into a tz-aware UTC datetime.

    Returns None if it cannot be read, so the caller can skip that one entry.

    AR-0014: a naive timestamp compared against a tz-aware cutoff raises
    TypeError, which — inside the restore loop — took the whole restore down
    with it. Anything naive is assumed UTC, which is what this integration
    writes anyway.
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _attributes_within_budget(entity_id: str, attributes: dict[str, Any]) -> bool:
    """Reject entries whose attributes exceed the per-entry budget (AR-0009)."""
    try:
        size = len(json.dumps(attributes, default=str).encode())
    except (TypeError, ValueError):
        _LOGGER.debug("Attributes for %s are not serialisable — skipping", entity_id)
        return False
    if size > MAX_ATTRIBUTE_BYTES:
        _LOGGER.warning(
            "Snapshot entry for %s carries %d bytes of attributes, above the "
            "%d byte cap — skipping it",
            entity_id,
            size,
            MAX_ATTRIBUTE_BYTES,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _should_track(entity_id: str, cfg: dict[str, Any]) -> bool:
    """Decide whether an entity is in scope for snapshot/restore.

    AR-0038: the leadership entity is refused first, ahead of every list.

    `input_boolean` is in the default domain allowlist, and the documented way
    to drive `leadership_source: entity` is an `input_boolean` that Keepalived
    toggles — so by default the leader published its own "I am the leader" flag
    into the shared snapshot, and the standby restored it at boot, before the
    first leadership evaluation, and concluded that it was the leader as well.
    The integration handed the follower the false belief itself, and the
    follower then un-gated its automations and started writing to the shared
    hash: precisely the split brain AR-0017 exists to prevent.

    Ahead of the include/exclude precedence rather than inside it, because this
    is not a default that happens to be wrong. The signal is a statement about
    *this node*; no configuration makes replicating it correct, so an operator
    listing it in `include_entities` is expressing a mistake rather than an
    intention.
    """
    leadership_entity = cfg.get(CONF_LEADERSHIP_ENTITY)
    if leadership_entity and entity_id == leadership_entity:
        return False

    excludes: list[str] = cfg.get(CONF_EXCLUDE_ENTITIES) or []
    if entity_id in excludes:
        return False

    explicit_includes: list[str] = cfg.get(CONF_INCLUDE_ENTITIES) or []
    if entity_id in explicit_includes:
        return True

    domain = entity_id.split(".", 1)[0]
    domains: list[str] = cfg.get(CONF_INCLUDE_DOMAINS) or DEFAULT_INCLUDE_DOMAINS
    return domain in domains
